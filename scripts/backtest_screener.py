# -*- coding: utf-8 -*-
"""
backtest_screener.py — 自研回测驱动的选股筛选器（用户 2026-08-01 决策）

背景：卡方 xlsx 绩效数据只作参考不作依据（内核不透明，无法验证筛选因子）。
决策：在没有经过回测数据验证的选股筛选器前，不增加监控标的。
本模块 = 用 v9 策略 + tickflow 1m 离线数据，对任意标的批量回测，验证
        胜率 / 盈亏比 / 年化 / 回撤，达标才可进入监控池。

模式：
  --run <csv1,csv2,...>    对指定 1m CSV 跑完整 v9 回测（按日配对 round-trip）
  --dir <path>             对目录下全部 *_1m.csv 批量回测
  --report <date>          生成 HTML 报告（复用 build_review_html 风格）
  --add-watchlist <sym>    把达标标的建议加入 watchlist（默认只输出建议，不直接改）

达标线（用户确认，与 v9 历史目标一致）：
  - 胜率 ≥ 60%
  - 盈亏比 ≥ 1.6
  - 样本量 ≥ 20 笔（否则标注"样本不足"，不判达标）
  - 年化 / 回撤作为辅助参考

用法示例：
  python scripts/backtest_screener.py --run backtest/backtest_data/688820.SH_1m.csv
  python scripts/backtest_screener.py --dir backtest/backtest_data
  python scripts/backtest_screener.py --run a.csv,b.csv --report 2026-08-01
"""
import argparse
import csv
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 成本模型常量（万一佣金不免五 + 滑点；ETF/LOF 无印花税）——用户费率 2026-08-01
from core.exit_manager import DEFAULT_COST, DEFAULT_COST_NO_STAMP, DEFAULT_COST_BSE  # noqa: E402

# 生产同源：MACD_GATE_MODE=floor（floor模式=MACD背离 OR 引力地板，生产默认）
# 必须在 import miji_alpha 前设置（其模块级变量在 import 时读取环境变量）
os.environ.setdefault('MACD_GATE_MODE', 'floor')

# 达标线（用户确认 2026-08-01）
MIN_WIN_RATE = 60.0      # 净胜率 ≥ 60%（已扣双边成本 ~0.12%）
MIN_PL_RATIO = 1.6       # 盈亏比 ≥ 1.6（基于净收益）
MIN_SAMPLE = 20          # 最少样本量（否则标注样本不足）
OUT_JSON = os.path.join(BASE, 'data', 'backtest_screener_results.json')

# 生产出场配置（与 monitor 一致：仅移动止损，act0.4/trail0.6，S信号自然出场）
PROD_CONFIG = dict(
    use_stop=False, use_time=False,
    use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
    s_signal_exit=True,
)


def load_1m_csv(path):
    """读取 1m/5m CSV → 兼容结构。
    支持两种格式：
      - tickflow 格式: symbol/name/timestamp/trade_date/trade_time/open/high/low/close/volume/amount
      - 新浪格式:      symbol/name/trade_time/open/high/low/close/volume/amount (trade_time 含日期)
    返回 DataFrame。"""
    import pandas as pd
    df = pd.read_csv(path)
    # 新浪格式: 无 trade_date 列，从 trade_time 拆分
    if 'trade_date' not in df.columns and 'trade_time' in df.columns:
        df['trade_date'] = df['trade_time'].astype(str).str[:10]
    df['trade_date'] = df['trade_date'].astype(str)
    return df


def group_by_day(df):
    """按交易日分组。返回 list[(date, sub_df)]，sub_df 按时间升序。"""
    out = []
    for d, g in df.groupby('trade_date', sort=True):
        if 'timestamp' in g.columns:
            g = g.sort_values('timestamp')
        elif 'trade_time' in g.columns:
            g = g.sort_values('trade_time')
        out.append((d, g))
    return out


def day_prev_close(df, date):
    """计算某交易日的前收盘价。取该日之前最近一个交易日的收盘价。"""
    dates = sorted(df['trade_date'].unique())
    if date not in dates:
        return None
    idx = dates.index(date)
    if idx == 0:
        return None  # 首日无昨收
    prev = dates[idx - 1]
    sub = df[df['trade_date'] == prev]
    return float(sub['close'].iloc[-1])


def backtest_symbol(csv_path, config=None, engine='miji', macd_min_hist_diff=0.0, atr_min_pct=None,
                    mpr_enable=False, mpr_periods=None,
                    vwap_dev_ceil=None, atr_min_pct_s=None):
    """对单个 1m CSV 跑完整 v9 回测（逐日 detect → simulate_day 配对）。

    engine:
      'miji'  : 生产同源信号引擎 miji_alpha.detect_miji_signals
                （gravity + vol_div + MACD背离 + floor门控 + 高波动守卫）
      'v9'    : 基础版 indicators.detect_signals（无 floor 门控）

    macd_min_hist_diff: MACD 背离强度阈值（0=不过滤=原行为；0.15=报告建议值，P0 接入）。
    atr_min_pct: ATR 波动率下限门槛 %（None=关闭；0.20~0.30 候选，P1 验证）。
    mpr_enable/mpr_periods: 多周期 MACD 方向过滤（P3-1，默认关）；支持 'B'/'S'/'both'。
    vwap_dev_ceil: S 侧 VWAP 偏离上限 %（None=关闭；P3-2 S 信号专项）。
    atr_min_pct_s: S 侧 ATR 波动率下限门槛 %（None=关闭；与 B 侧 atr_min_pct 对称）。

    返回 dict：{symbol, days, trips, metrics, pass_verdict}。"""
    if engine == 'miji':
        from core.miji_alpha import compute_miji_indicators, detect_miji_signals
    else:
        from core.indicators import compute_indicators as compute_miji_indicators
        from core.indicators import detect_signals as detect_miji_signals
    from core.exit_manager import (simulate_day, aggregate_metrics, make_config,
                                   DEFAULT_COST, DEFAULT_COST_NO_STAMP, DEFAULT_COST_BSE,
                                   cost_for_symbol)
    cfg = config or PROD_CONFIG
    mcfg = make_config(**cfg)

    df = load_1m_csv(csv_path)
    if 'symbol' in df.columns:
        symbol = str(df['symbol'].iloc[0])
    else:
        base = os.path.basename(csv_path).replace('_1m.csv', '').replace('_5m.csv', '')
        symbol = base
    # 成本模型按标的类型自动选择（2026-08-01 用户费率）
    #   个股 → 万一佣金+印花税；ETF/LOF(1xx/5xx) → 万一佣金无印花税；
    #   北交所(4xx/8xx) → 千分之0.575 佣金无印花税
    cost = cost_for_symbol(symbol)
    days = group_by_day(df)

    all_trips = []
    day_count = 0
    skipped_no_pc = 0
    for date, sub in days:
        pc = day_prev_close(df, date)
        if pc is None or pc <= 0:
            skipped_no_pc += 1
            continue
        o = sub['open'].values.astype(float)
        h = sub['high'].values.astype(float)
        lo = sub['low'].values.astype(float)
        c = sub['close'].values.astype(float)
        v = sub['volume'].values.astype(float)
        data = compute_miji_indicators(o, h, lo, c, v, pc)
        sigs = detect_miji_signals(data, pc, macd_min_hist_diff=macd_min_hist_diff,
                                   atr_min_pct=atr_min_pct,
                                   mpr_enable=mpr_enable, mpr_periods=mpr_periods,
                                   vwap_dev_ceil=vwap_dev_ceil, atr_min_pct_s=atr_min_pct_s)
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data.get('trend'), 'n': data['n']}
        trips = simulate_day(sigs, prices, mcfg, cost=cost)
        all_trips.extend(trips)
        day_count += 1

    metrics = aggregate_metrics(all_trips)
    n = metrics['total']
    # 达标判定（样本量≥20 且 胜率/盈亏比达标）
    verdict = {
        'pass': n >= MIN_SAMPLE and metrics['win_rate'] >= MIN_WIN_RATE
                and metrics['pl_ratio'] >= MIN_PL_RATIO,
        'sample_ok': n >= MIN_SAMPLE,
        'reason': '',
    }
    if n < MIN_SAMPLE:
        verdict['reason'] = f'样本不足（{n}笔 < {MIN_SAMPLE}），不作达标判定'
    elif metrics['win_rate'] < MIN_WIN_RATE:
        verdict['reason'] = f'胜率{metrics["win_rate"]}% < {MIN_WIN_RATE}%'
    elif metrics['pl_ratio'] < MIN_PL_RATIO:
        verdict['reason'] = f'盈亏比{metrics["pl_ratio"]} < {MIN_PL_RATIO}'
    else:
        verdict['reason'] = '达标'

    return {
        'symbol': symbol,
        'csv': os.path.basename(csv_path),
        'days': day_count,
        'skipped_no_pc': skipped_no_pc,
        'trips': all_trips,
        'metrics': metrics,
        'verdict': verdict,
        'config': cfg,
        'engine': engine,
    }


def run_batch(paths, verbose=True, engine='miji', macd_min_hist_diff=0.0, atr_min_pct=None):
    """批量回测，返回 {symbol: result}。"""
    results = {}
    for p in paths:
        if not os.path.exists(p):
            print(f'  ⚠️ 跳过不存在的文件: {p}')
            continue
        try:
            r = backtest_symbol(p, engine=engine, macd_min_hist_diff=macd_min_hist_diff,
                                atr_min_pct=atr_min_pct)
        except Exception as e:
            print(f'  ❌ {os.path.basename(p)} 回测失败: {e}')
            results[os.path.basename(p)] = {'symbol': os.path.basename(p), 'error': str(e)}
            continue
        results[r['symbol']] = r
        if verbose:
            m = r['metrics']; v = r['verdict']
            mark = '✅' if v['pass'] else ('⚠️' if not v['sample_ok'] else '❌')
            print(f'  {mark} {r["symbol"]:12s} 天数{r["days"]:>3} 笔数{m["total"]:>3} '
                  f'胜率{m["win_rate"]:>5.1f}% 盈亏比{m["pl_ratio"]:>5.2f} '
                  f'年化{m["ann_ret_pct"]:>8.2f}% 回撤{m["max_drawdown_pct"]:>6.2f}% '
                  f'{v["reason"]}')
    return results


def save_results(results, out_path=None):
    """把指标（不含 trips，减少体积）落盘。"""
    out_path = out_path or OUT_JSON
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'min_win_rate': MIN_WIN_RATE,
        'min_pl_ratio': MIN_PL_RATIO,
        'min_sample': MIN_SAMPLE,
        'config': PROD_CONFIG,
        'cost_model': {
            'commission_rate': '万一(0.0001)不免五（沪深/ETF/可转债/债券/港股通）；北交所千分之0.575',
            'stamp_duty': '卖出万5.641（仅沪深个股；ETF/LOF/可转债/债券现券/港股通/北交所无）',
            'slippage_bps': 2.0,
            'stock_bilateral_pct': round(DEFAULT_COST[0] + DEFAULT_COST[1], 4),
            'fund_bilateral_pct': round(DEFAULT_COST_NO_STAMP[0] + DEFAULT_COST_NO_STAMP[1], 4),
            'bse_bilateral_pct': round(DEFAULT_COST_BSE[0] + DEFAULT_COST_BSE[1], 4),
        },
        'results': {
            sym: {
                'symbol': r.get('symbol'),
                'csv': r.get('csv'),
                'days': r.get('days'),
                'error': r.get('error'),
                'metrics': r.get('metrics'),
                'verdict': r.get('verdict'),
                'config': r.get('config'),
            } for sym, r in results.items()
        }
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 结果已写入 {out_path}')
    return out_path


def main():
    ap = argparse.ArgumentParser(description='自研回测驱动的选股筛选器（v9 + tickflow 1m）')
    ap.add_argument('--run', help='对指定 1m CSV 回测，逗号分隔多个')
    ap.add_argument('--dir', help='对目录下全部 *_1m.csv 批量回测')
    ap.add_argument('--report', help='生成 HTML 报告日期，如 2026-08-01（默认输出 output/backtest_screener_report.html）')
    ap.add_argument('--engine', choices=['miji', 'v9'], default='miji',
                    help='信号引擎: miji=生产同源(默认), v9=基础版')
    ap.add_argument('--mhd', type=float, default=0.0,
                    help='MACD 背离强度阈值 min_hist_diff（0=不过滤=旧行为，0.15=报告建议，P0 生产已接入）')
    ap.add_argument('--atr', type=float, default=None,
                    help='ATR 波动率下限门槛 %%（None=关闭；0.20/0.25/0.30 候选，P1 验证）')
    args = ap.parse_args()

    if args.dir:
        import glob
        paths = sorted(glob.glob(os.path.join(args.dir, '*_1m.csv'))) + \
                sorted(glob.glob(os.path.join(args.dir, '*_5m.csv')))
        print(f'🎯 批量回测目录 {args.dir}: {len(paths)} 个标的')
    elif args.run:
        paths = [p.strip() for p in args.run.split(',') if p.strip()]
        print(f'🎯 回测 {len(paths)} 个标的')
    else:
        ap.print_help()
        return

    results = run_batch(paths, verbose=True, engine=args.engine, macd_min_hist_diff=args.mhd,
                        atr_min_pct=args.atr)
    save_results(results)

    # 汇总
    passed = [r for r in results.values() if isinstance(r, dict) and r.get('verdict') and r['verdict']['pass']]
    print(f'\n✅ 达标 {len(passed)}/{len(results)}:')
    for r in passed:
        m = r['metrics']
        print(f'  {r["symbol"]:12s} 胜率{m["win_rate"]}% 盈亏比{m["pl_ratio"]} 年化{m["ann_ret_pct"]}%')

    if args.report:
        _write_html(results, args.report)


def _write_html(results, date_str):
    """生成 HTML 报告（深色主题，内嵌数据）。"""
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    passed = [r for r in results.values() if isinstance(r, dict) and r.get('verdict') and r['verdict']['pass']]
    sample_warn = [r for r in results.values() if isinstance(r, dict) and r.get('verdict') and not r['verdict']['sample_ok']]
    rows_html = ''
    for sym, r in sorted(results.items()):
        if 'error' in r and r.get('error'):
            rows_html += f'<tr><td>{esc(sym)}</td><td colspan="6">❌ {esc(r["error"])}</td></tr>'
            continue
        m = r['metrics']; v = r['verdict']
        mark = '✅' if v['pass'] else ('⚠️' if not v['sample_ok'] else '❌')
        rows_html += (
            f'<tr><td>{mark} {esc(sym)}</td>'
            f'<td>{m["total"]}</td><td>{m["win_rate"]}%</td><td>{m["pl_ratio"]}</td>'
            f'<td>{m["ann_ret_pct"]}%</td><td>{m["max_drawdown_pct"]}%</td>'
            f'<td>{esc(v["reason"])}</td></tr>'
        )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>tpoint 回测筛选器 {date_str}</title>
<style>
body{{background:#11151c;color:#d5dae2;font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1100px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:#9ec9ff;font-size:15px;margin-top:28px}}
.card{{background:#1a2029;border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #2a3140;font-size:13px}}
th{{color:#8a93a6;font-weight:500}}
.ok{{color:#7ee2a8}} .bad{{color:#ff8b8b}} .warn{{color:#f5c26b}}
.sum{{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}}
.sum div{{background:#232b38;border-radius:8px;padding:12px 18px}}
.sum b{{font-size:22px;display:block;color:#fff}}
</style></head><body>
<h1>tpoint 回测筛选器 · {date_str}</h1>
<div class="card">
  <h2>达标线（用户确认）</h2>
  <p>胜率 ≥ {MIN_WIN_RATE}% 且 盈亏比 ≥ {MIN_PL_RATIO}，样本 ≥ {MIN_SAMPLE} 笔；年化/回撤仅作参考。</p>
  <p>出场配置：仅移动止损（act {PROD_CONFIG["trail_activate_pct"]}% / trail {PROD_CONFIG["trail_pct"]}%），S信号自然出场。</p>
  <div class="sum">
    <div><b>{len(results)}</b>回测标的</div>
    <div><b>{len(passed)}</b>达标</div>
    <div><b>{len(sample_warn)}</b>样本不足</div>
  </div>
</div>
<div class="card">
  <h2>逐标的回测结果</h2>
  <table>
    <tr><th>标的</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>年化</th><th>最大回撤</th><th>判定</th></tr>
    {rows_html}
  </table>
</div>
</body></html>"""
    out_path = os.path.join(BASE, 'output', f'backtest_screener_report_{date_str}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out_path}')


if __name__ == '__main__':
    main()
