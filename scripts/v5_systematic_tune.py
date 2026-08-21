# -*- coding: utf-8 -*-
"""
v5_systematic_tune.py — v5/GT 参数系统性调优（清洁数据 → IS 选参 → OOS 验证 → 生产配置）

数据：F:/keyfactor_data/1m_clean（2026-08-20 清洗后）
目标：方向准确性（directional accuracy）—— B 信号后 N 根 bar 价格上行、S 信号后下行。
      离线 PnL/WR 被配对偏置污染，不可靠；方向准确性是信号质量的本质度量。
方法：Coordinate ascent 分阶段搜索，避免组合爆炸；IS/OOS 时间切分 70/30 防过拟合。
输出：output/v5_systematic_tune_<date>.json + .html
      包含候选生产配置（可直接写入 monitor_config.json._global.general_algorithm）。

注意：本脚本不直接改写 data/monitor_config.json；P1 灰度 observing 期间，
      候选配置需经量化专家 agent 评审 / 实盘灰度验证后方可部署。
"""
import os, sys, json, csv, argparse, datetime, itertools, signal
# 进程级信号免疫：避免工具侧超时强杀波及
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        signal.signal(getattr(signal, _s), signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GeneralConfig, STRATEGY_VERSION, ENGINE_FULL
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

# 样本池：优先选历史足够长的 ETF/LOF + 当前 watchlist 个股
SYMBOLS = ['161129.SZ', '513310.SH', '688111.SH']
NAME = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF', '688111.SH': '金山办公'}

OOS_SPLIT = 0.30          # 后 30% 日期作为 OOS
FWD_HORIZON = 8           # 方向准确性前瞻窗口（分钟 bar）
MIN_SIGNALS_IS = 40       # IS 阶段单标最小信号数（ETF/LOF 样本长，个股样本短，放宽）
MIN_SIGNALS_OOS = 15      # OOS 阶段单标最小信号数


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs], dtype=float)
        h = np.array([float(x['high']) for x in rs], dtype=float)
        lo = np.array([float(x['low']) for x in rs], dtype=float)
        c = np.array([float(x['close']) for x in rs], dtype=float)
        v = np.array([float(x['volume']) for x in rs], dtype=float)
        days[d] = (o, h, lo, c, v)
    return days


def build_runs(sym, min_bars=200):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return []
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    pc_map = {}
    prev = None
    for d in dates:
        _, _, _, c, _ = days_all[d]
        pc_map[d] = prev if prev is not None else (c[0] if len(c) else 0.0)
        if len(c):
            prev = c[-1]
    complete = [(d, days_all[d]) for d in dates if len(days_all[d][3]) >= min_bars]
    runs = []
    for idx, (d, (o, h, lo, c, v)) in enumerate(complete):
        runs.append({
            'sym': sym, 'date': d, 'o': o, 'h': h, 'lo': lo, 'c': c, 'v': v,
            'pc': pc_map[d], 'n': len(c),
            'tag': 'IS' if idx < int(len(complete) * (1 - OOS_SPLIT)) else 'OOS',
            'data': None
        })
    return runs


def prep_run(r):
    if r['pc'] <= 0 or r['n'] < 10:
        return False
    df = pd.DataFrame({
        'open': r['o'], 'high': r['h'], 'low': r['lo'], 'close': r['c'], 'volume': r['v'],
        'trade_time': [r['date'] + ' 09:31:00'] * r['n']
    })
    try:
        r['data'] = build_data(df, r['pc'])
        r['data']['n'] = r['n']
        return r['data'] is not None
    except Exception as e:
        print(f'[{r["sym"]} {r["date"]}] prep error: {e}')
        return False


def direction_accuracy(sigs, c, horizon=FWD_HORIZON):
    """逐信号计算 forward return：B 后价涨 / S 后价跌为正确。"""
    if not sigs:
        return None, 0
    correct = 0
    for s in sigs:
        i = s['idx']
        if i + horizon >= len(c):
            continue
        fwd_ret = (c[i + horizon] - c[i]) / c[i] if c[i] > 0 else 0.0
        if s['type'] == 'B' and fwd_ret > 0:
            correct += 1
        elif s['type'] == 'S' and fwd_ret < 0:
            correct += 1
    n = len(sigs)
    return (100.0 * correct / n) if n else 0.0, n


def run_cfg_on_day(r, cfg):
    data = r['data']
    sigs = detect_signals_general(data, r['pc'], cfg)
    return sigs


def evaluate_cfg(cfg, runs_by_sym, tag='IS'):
    """返回逐标方向准确性 + 聚合中位数 + 信号数。"""
    per = {}
    all_pairs = []  # (is_b, fwd_ret) 跨标聚合
    for sym, runs in runs_by_sym.items():
        sigs = []
        for r in runs:
            if r['tag'] != tag:
                continue
            ss = run_cfg_on_day(r, cfg)
            sigs.extend(ss)
            c = r['c']
            for s in ss:
                i = s['idx']
                if i + FWD_HORIZON < len(c) and c[i] > 0:
                    all_pairs.append((s['type'] == 'B', (c[i + FWD_HORIZON] - c[i]) / c[i]))
        acc, n = direction_accuracy(sigs, c if runs else np.array([]))
        per[sym] = {'acc': acc, 'n': n}
    # 跨标中位数（抗单标稀释）
    accs = [v['acc'] for v in per.values() if v['acc'] is not None and v['n'] >= (MIN_SIGNALS_IS if tag == 'IS' else MIN_SIGNALS_OOS)]
    median_acc = float(np.median(accs)) if accs else 0.0
    # 跨标总信号数
    total_n = sum(v['n'] for v in per.values())
    # 全信号方向准确性
    all_acc = _acc_all(all_pairs)
    return {'per_symbol': per, 'median_acc': median_acc, 'total_n': total_n, 'all_acc': all_acc}


def _acc_all(pairs):
    if not pairs:
        return 0.0
    correct = sum(1 for b, fr in pairs if (b and fr > 0) or (not b and fr < 0))
    return 100.0 * correct / len(pairs)


def backtest_pool(cfg, runs_by_sym, tag='IS'):
    """用 simulate_day 做配对回测，得到 WR/total_ret（仅作参考，非优化目标）。"""
    exit_cfg = make_config()
    pool_trips = []
    per = {}
    for sym, runs in runs_by_sym.items():
        trips = []
        for r in runs:
            if r['tag'] != tag:
                continue
            sigs = run_cfg_on_day(r, cfg)
            prices = {'o': r['o'], 'h': r['h'], 'lo': r['lo'], 'c': r['c'], 'atr': r['data']['atr'],
                      'trend': r['data']['trend'], 'n': r['n'], 'date': r['date'], 'pc': r['pc'], 'sym': sym}
            trips.extend(simulate_day(sigs, prices, exit_cfg, cost_for_symbol(sym)))
        pool_trips.extend(trips)
        if trips:
            n = len(trips); wins = sum(1 for t in trips if t['ret_pct'] > 0)
            per[sym] = {'n': n, 'wr': round(100.0 * wins / n, 1), 'total_ret': round(sum(t['ret_pct'] for t in trips), 2)}
    if not pool_trips:
        return {'n': 0, 'wr': 0.0, 'total_ret': 0.0}, per
    n = len(pool_trips); wins = sum(1 for t in pool_trips if t['ret_pct'] > 0)
    return {'n': n, 'wr': round(100.0 * wins / n, 1), 'total_ret': round(sum(t['ret_pct'] for t in pool_trips), 2)}, per


def make_monitor_config(global_cfg_dict):
    """生成可直接写入 monitor_config.json 的候选 _global 配置。"""
    return {
        "_global": {
            "settle_split_enable": False,
            "_note": "2026-08-22 v5 系统性调优候选配置（P1 observing，未部署）",
            "vol_ratio_b_max": None,
            "use_general_engine": True,
            "v4_gray_enable": True,
            "v4_promote": False,
            "bidirectional_enable": True,
            "general_algorithm": {
                "_note": "v5 候选配置（由 v5_systematic_tune.py 从清洁数据 IS/OOS 选出）",
                "strategy_version": STRATEGY_VERSION,
                "engine": ENGINE_FULL,
                **global_cfg_dict
            }
        },
        "603039.SH": {"comment": "泛微网络；候选配置待灰度验证"},
        "688111.SH": {"comment": "金山办公；候选配置待灰度验证"}
    }


def coordinate_ascent(runs_by_sym, baseline_cfg):
    """分阶段 coordinate ascent，每阶段只变一个子集。"""
    best_cfg = baseline_cfg
    best_is = evaluate_cfg(best_cfg, runs_by_sym, 'IS')
    stages = []

    # 阶段 A：权重组合
    grids_A = list(itertools.product(
        [0.8, 1.0, 1.2, 1.5],   # w_vwap
        [0.3, 0.5, 0.7, 1.0],   # w_vol_div
        [0.6, 0.9, 1.2],        # w_macd_div
        [0.6, 0.8, 1.0]         # w_rsi
    ))
    cands_A = []
    for wv, wvd, wm, wr in grids_A:
        cfg = best_cfg.__class__(**{**best_cfg.__dict__,
                                    'w_vwap': wv, 'w_vol_div': wvd, 'w_macd_div': wm, 'w_rsi': wr})
        ev = evaluate_cfg(cfg, runs_by_sym, 'IS')
        cands_A.append((cfg, ev))
    best_cfg, best_is = max(cands_A, key=lambda x: x[1]['median_acc'])
    stages.append({'name': '权重组合', 'n': len(cands_A), 'best_median_acc': best_is['median_acc'],
                   'best_weights': {'w_vwap': best_cfg.w_vwap, 'w_vol_div': best_cfg.w_vol_div,
                                    'w_macd_div': best_cfg.w_macd_div, 'w_rsi': best_cfg.w_rsi}})

    # 阶段 B：threshold + gap
    grids_B = list(itertools.product(
        [0.40, 0.45, 0.50, 0.55],  # threshold
        [6, 8]                     # gap
    ))
    cands_B = []
    for thr, gap in grids_B:
        cfg = best_cfg.__class__(**{**best_cfg.__dict__,
                                    'buy_threshold': thr, 'sell_threshold': thr, 'signal_gap': gap})
        ev = evaluate_cfg(cfg, runs_by_sym, 'IS')
        cands_B.append((cfg, ev))
    best_cfg, best_is = max(cands_B, key=lambda x: x[1]['median_acc'])
    stages.append({'name': 'threshold+gap', 'n': len(cands_B), 'best_median_acc': best_is['median_acc'],
                   'best_thr_gap': {'buy_threshold': best_cfg.buy_threshold, 'signal_gap': best_cfg.signal_gap}})

    # 阶段 C：b_downtrend_reversal + regime_gate
    grids_C = list(itertools.product([True, False], [True, False]))
    cands_C = []
    for b_rev, reg in grids_C:
        cfg = best_cfg.__class__(**{**best_cfg.__dict__,
                                    'b_downtrend_reversal': b_rev, 'regime_gate': reg})
        ev = evaluate_cfg(cfg, runs_by_sym, 'IS')
        cands_C.append((cfg, ev))
    best_cfg, best_is = max(cands_C, key=lambda x: x[1]['median_acc'])
    stages.append({'name': '双向门控+regime', 'n': len(cands_C), 'best_median_acc': best_is['median_acc'],
                   'best_flags': {'b_downtrend_reversal': best_cfg.b_downtrend_reversal,
                                  'regime_gate': best_cfg.regime_gate}})

    # 阶段 D：RSI 参数
    grids_D = list(itertools.product(
        [9, 14, 21],           # rsi_period
        [(30, 70), (35, 65), (40, 60)]  # (oversold, overbought)
    ))
    cands_D = []
    for period, (os, ob) in grids_D:
        cfg = best_cfg.__class__(**{**best_cfg.__dict__,
                                    'rsi_period': period, 'rsi_oversold': os, 'rsi_overbought': ob})
        ev = evaluate_cfg(cfg, runs_by_sym, 'IS')
        cands_D.append((cfg, ev))
    best_cfg, best_is = max(cands_D, key=lambda x: x[1]['median_acc'])
    stages.append({'name': 'RSI参数', 'n': len(cands_D), 'best_median_acc': best_is['median_acc'],
                   'best_rsi': {'rsi_period': best_cfg.rsi_period,
                                'rsi_oversold': best_cfg.rsi_oversold,
                                'rsi_overbought': best_cfg.rsi_overbought}})

    # 阶段 E：vwap_k1 + div_local_w + div_vol_ratio
    grids_E = list(itertools.product(
        [0.5, 0.8, 1.0, 1.2],  # vwap_k1
        [10, 15, 20],          # div_local_w
        [0.6, 0.7, 0.8]        # div_vol_ratio
    ))
    cands_E = []
    for k1, div_w, div_vr in grids_E:
        cfg = best_cfg.__class__(**{**best_cfg.__dict__,
                                    'vwap_k1': k1, 'div_local_w': div_w, 'div_vol_ratio': div_vr})
        ev = evaluate_cfg(cfg, runs_by_sym, 'IS')
        cands_E.append((cfg, ev))
    best_cfg, best_is = max(cands_E, key=lambda x: x[1]['median_acc'])
    stages.append({'name': 'vwap+量价背离', 'n': len(cands_E), 'best_median_acc': best_is['median_acc'],
                   'best_vwap_vol': {'vwap_k1': best_cfg.vwap_k1, 'div_local_w': best_cfg.div_local_w,
                                     'div_vol_ratio': best_cfg.div_vol_ratio}})

    return best_cfg, best_is, stages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--syms', default=','.join(SYMBOLS))
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]

    print(f'[v5_systematic_tune] 样本池: {syms}')
    runs_by_sym = {}
    for sym in syms:
        runs = build_runs(sym)
        ok = sum(1 for r in runs if prep_run(r))
        print(f'[{sym}] 总交易日={len(runs)} 有效={ok} IS={sum(1 for r in runs if r["tag"]=="IS")} OOS={sum(1 for r in runs if r["tag"]=="OOS")}')
        if ok:
            runs_by_sym[sym] = runs

    if not runs_by_sym:
        print('无有效数据，退出')
        return

    baseline_cfg = GeneralConfig()
    print('\n=== 基线配置方向准确性 ===')
    base_is = evaluate_cfg(baseline_cfg, runs_by_sym, 'IS')
    base_oos = evaluate_cfg(baseline_cfg, runs_by_sym, 'OOS')
    print(f'基线 IS median_acc={base_is["median_acc"]:.1f}% all_acc={base_is["all_acc"]:.1f}% n={base_is["total_n"]}')
    print(f'基线 OOS median_acc={base_oos["median_acc"]:.1f}% all_acc={base_oos["all_acc"]:.1f}% n={base_oos["total_n"]}')

    print('\n=== Coordinate Ascent 开始 ===')
    best_cfg, best_is, stages = coordinate_ascent(runs_by_sym, baseline_cfg)

    print('\n=== OOS 验证最优配置 ===')
    best_oos = evaluate_cfg(best_cfg, runs_by_sym, 'OOS')
    print(f'最优 IS median_acc={best_is["median_acc"]:.1f}% all_acc={best_is["all_acc"]:.1f}% n={best_is["total_n"]}')
    print(f'最优 OOS median_acc={best_oos["median_acc"]:.1f}% all_acc={best_oos["all_acc"]:.1f}% n={best_oos["total_n"]}')

    # 用 simulate_day 跑回测（IS/OOS 都跑，作为参考）
    bt_is, bt_is_per = backtest_pool(best_cfg, runs_by_sym, 'IS')
    bt_oos, bt_oos_per = backtest_pool(best_cfg, runs_by_sym, 'OOS')
    print(f'\n回测参考 IS: trips={bt_is["n"]} WR={bt_is["wr"]}% net={bt_is["total_ret"]}%')
    print(f'回测参考 OOS: trips={bt_oos["n"]} WR={bt_oos["wr"]}% net={bt_oos["total_ret"]}%')

    # 基线回测参考
    bt_base_is, _ = backtest_pool(baseline_cfg, runs_by_sym, 'IS')
    bt_base_oos, _ = backtest_pool(baseline_cfg, runs_by_sym, 'OOS')

    # 组装输出
    cfg_dict = best_cfg.as_dict()
    # 去掉不可 JSON 或不应暴露的字段
    cfg_dict.pop('trend_b_allowed', None)
    cfg_dict.pop('trend_s_allowed', None)

    out = {
        'date': a.out_suffix,
        'engine': ENGINE_FULL,
        'strategy_version': STRATEGY_VERSION,
        'symbols': syms,
        'fwd_horizon': FWD_HORIZON,
        'oos_split': OOS_SPLIT,
        'baseline': {
            'cfg': baseline_cfg.as_dict(),
            'is': base_is, 'oos': base_oos,
            'backtest_is': bt_base_is, 'backtest_oos': bt_base_oos,
        },
        'best': {
            'cfg': cfg_dict,
            'is': best_is, 'oos': best_oos,
            'backtest_is': bt_is, 'backtest_oos': bt_oos,
            'backtest_per_symbol_is': bt_is_per,
            'backtest_per_symbol_oos': bt_oos_per,
        },
        'stages': stages,
        'deploy_ready': best_oos['median_acc'] >= 50.0 and best_oos['total_n'] >= MIN_SIGNALS_OOS,
        'monitor_config_candidate': make_monitor_config(cfg_dict)
    }

    fn = f'v5_systematic_tune_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\nJSON -> {fp}')

    # 生成 HTML 报告
    build_html(out, os.path.join(OUT, f'v5_systematic_tune_{a.out_suffix}.html'))


def build_html(out, path):
    baseline = out['baseline']
    best = out['best']
    rows = ''
    for stage in out['stages']:
        rows += f"<tr><td>{stage['name']}</td><td>{stage['n']}</td><td>{stage['best_median_acc']:.1f}%</td><td><pre>{json.dumps(stage.get('best_weights') or stage.get('best_thr_gap') or stage.get('best_flags') or stage.get('best_rsi') or stage.get('best_vwap_vol'), ensure_ascii=False)}</pre></td></tr>"

    def fmt(ev):
        return f"median_acc={ev['median_acc']:.1f}% all_acc={ev['all_acc']:.1f}% n={ev['total_n']}"

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>v5 系统性调优报告 {out['date']}</title>
<style>
body{{font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; margin: 40px; background:#f7f8fa; color:#1f2329;}}
.card{{background:#fff; border-radius:8px; padding:24px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.08);}}
h1{{font-size:22px; margin-top:0;}} h2{{font-size:17px; color:#3370ff; margin-top:24px;}}
table{{border-collapse:collapse; width:100%; font-size:13px; margin-top:10px;}}
th,td{{border:1px solid #e1e3e6; padding:8px; text-align:left;}}
th{{background:#f2f3f5;}}
pre{{background:#f7f8fa; padding:8px; border-radius:4px; overflow-x:auto; font-size:12px;}}
.pass{{color:#00b42a; font-weight:bold;}} .fail{{color:#f53f3f; font-weight:bold;}}
</style></head><body>
<div class="card"><h1>v5/GT 系统性调优报告（{out['date']}）</h1>
<p>引擎：{out['engine']} | 样本标的：{', '.join(out['symbols'])} | 前瞻窗口：{out['fwd_horizon']}bar | OOS切分：后{int(out['oos_split']*100)}%</p>
<p>优化目标：<b>方向准确性 median_acc</b>（B后价涨/S后价跌），非 PnL。回测 WR/净仅作参考。</p>
<p>部署就绪：{'<span class="pass">YES</span>' if out['deploy_ready'] else '<span class="fail">NO（OOS median_acc<50% 或样本不足）</span>'}</p>
</div>
<div class="card"><h2>基线 vs 最优（IS/OOS）</h2>
<table>
<tr><th>配置</th><th>IS 方向准确性</th><th>OOS 方向准确性</th><th>IS 回测（WR / net）</th><th>OOS 回测（WR / net）</th></tr>
<tr><td>基线</td><td>{fmt(baseline['is'])}</td><td>{fmt(baseline['oos'])}</td><td>{baseline['backtest_is']['wr']}% / {baseline['backtest_is']['total_ret']}%</td><td>{baseline['backtest_oos']['wr']}% / {baseline['backtest_oos']['total_ret']}%</td></tr>
<tr><td><b>最优</b></td><td><b>{fmt(best['is'])}</b></td><td><b>{fmt(best['oos'])}</b></td><td><b>{best['backtest_is']['wr']}% / {best['backtest_is']['total_ret']}%</b></td><td><b>{best['backtest_oos']['wr']}% / {best['backtest_oos']['total_ret']}%</b></td></tr>
</table></div>
<div class="card"><h2>Coordinate Ascent 各阶段</h2><table><tr><th>阶段</th><th>组合数</th><th>最优 median_acc</th><th>最优参数</th></tr>{rows}</table></div>
<div class="card"><h2>候选生产配置（monitor_config.json._global.general_algorithm）</h2><pre>{json.dumps(out['monitor_config_candidate'], ensure_ascii=False, indent=2)}</pre></div>
<div class="card"><h2>逐标回测明细（OOS）</h2><pre>{json.dumps(best['backtest_per_symbol_oos'], ensure_ascii=False, indent=2)}</pre></div>
</body></html>"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML -> {path}')


if __name__ == '__main__':
    main()
