# -*- coding: utf-8 -*-
"""
backtest_general_v5.py —— 通用算法(GT v1.0 / 做T策略 v5) tickflow 离线长回测
- 数据: F:/keyfactor_data/1m/<sym>_1m.csv（tickflow 落地，真实 1m）
- 引擎: core/general_signal.detect_signals_general（与 watchlist_engine 同源）
- 出场: exit_manager.make_config 默认（硬止损atr1.5 + 时间止损90 + 移动止损0.4/0.6）
- 配对: simulate_day（正T: B→S），成本 cost_for_symbol（个股含印花 / LOF·ETF 无）
- 输出: output/backtest_general_v5_<date>.json + .html（逐标的 + 池级聚合 + WR 判定）

用法: python backtest_general_v5.py [--last N] [--syms a,b,c]
口径: 与 backtest_v10_2_0.py 一致（信号质量回测，非生产 monitor 完整状态机）
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from general_signal import detect_signals_general, GENERAL_DEFAULT, STRATEGY_VERSION, ENGINE_FULL
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data
from simulate_bidirectional import simulate_dual

DATA_DIR = r'F:/keyfactor_data/1m_clean'   # 2026-08-20 清洗后数据（剔除 ms 合成段/垃圾volume/时段外）
OUT = os.path.join(ROOT, 'output')
SYMBOLS = ['603039.SH', '688111.SH', '300058.SZ', '600570.SH', '161129.SZ', '513310.SH']
KIND = {'603039.SH':'沪主板股','688111.SH':'科创板股','300058.SZ':'创业板股',
        '600570.SH':'沪主板股','161129.SZ':'原油LOF','513310.SH':'中概ETF'}
NAME = {'603039.SH':'泛微网络','688111.SH':'金山办公','300058.SZ':'蓝色光标','600570.SH':'恒生电子',
        '161129.SZ':'原油LOF易方达','513310.SH':'中概互联网ETF'}


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs])
        h = np.array([float(x['high']) for x in rs])
        lo = np.array([float(x['low']) for x in rs])
        c = np.array([float(x['close']) for x in rs])
        v = np.array([float(x['volume']) for x in rs])
        days[d] = (o, h, lo, c, v)
    return days


def run_symbol(sym, last_n=None, min_days=5, dual=False):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    if last_n:
        dates = dates[-last_n:]
    cfg = make_config()
    cost = cost_for_symbol(sym)
    trips_all, short_all = [], []
    prev_close = None
    n_days_ok = 0
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:          # 至少 20 根 1m（上午开盘段）
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})  # 仅 build_data 用列
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        if dual:
            lt, st = simulate_dual(sigs, prices, cfg, cost)
            trips_all.extend(lt); short_all.extend(st)
        else:
            trips_all.extend(simulate_day(sigs, prices, cfg, cost))
        n_days_ok += 1
        prev_close = c[-1]
    if n_days_ok < min_days:
        return {'sym': sym, 'error': f'insufficient_days({n_days_ok})'}
    return {'sym': sym, 'days': n_days_ok, 'first': dates[0] if dates else None,
            'last': dates[-1] if dates else None, 'trips': trips_all,
            'short_trips': short_all if dual else None}


def compute_data(o, h, lo, c, v, pc):
    """兼容占位：实际走 build_data（与生产同源），此处保留避免误引用。"""
    return None


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg_trip=0.0, win=0, loss=0, max_dd=0.0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    loss = n - wins
    rets = [float(t['ret_pct']) for t in trips]
    cum = np.cumsum(rets)
    max_dd = float((cum - np.maximum.accumulate(cum)).min()) if n else 0.0
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg_trip=round(sum(rets) / n, 3), win=wins, loss=loss, max_dd=round(max_dd, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None, help='仅回测最近 N 个交易日')
    ap.add_argument('--syms', default=','.join(SYMBOLS))
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--dual', action='store_true', help='双向模式（正T+反T，量化S侧）')
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]

    results = {}
    for sym in syms:
        r = run_symbol(sym, last_n=a.last, dual=a.dual)
        if 'error' in r:
            print(f'[{sym}] SKIP: {r["error"]}')
            results[sym] = {'sym': sym, 'name': NAME.get(sym, sym), 'kind': KIND.get(sym, ''),
                            'error': r['error']}
            continue
        agg = summarize(r['trips'])
        extra = {}
        if a.dual:
            short_agg = summarize(r['short_trips'] or [])
            extra = {'short': short_agg}
            print(f'[{sym}] {NAME.get(sym,sym)} days={r["days"]} '
                  f'正T trips={agg["n"]} WR={agg["wr"]}% net={agg["total_ret"]}% | '
                  f'反T trips={short_agg["n"]} WR={short_agg["wr"]}% net={short_agg["total_ret"]}%')
        else:
            print(f'[{sym}] {NAME.get(sym,sym)} days={r["days"]} ({r["first"]}~{r["last"]}) '
                  f'trips={agg["n"]} WR={agg["wr"]}% net={agg["total_ret"]}% avg={agg["avg_trip"]}%')
        results[sym] = {'sym': sym, 'name': NAME.get(sym, sym), 'kind': KIND.get(sym, ''),
                        'days': r['days'], 'first': r['first'], 'last': r['last'], **agg, **extra}

    # 池级聚合（WR/净按 trip 汇总；剔除 error 行）
    pool_trips = [r for sym, r in results.items() if 'error' not in r]
    tot_n = sum(r.get('n', 0) for r in pool_trips)
    tot_win = sum(r.get('win', 0) for r in pool_trips)
    tot_ret = sum(r.get('total_ret', 0) for r in pool_trips)
    pool_days = sum(r.get('days', 0) for r in pool_trips)
    pool = dict(n=tot_n, wr=round(100.0 * tot_win / tot_n, 1) if tot_n else 0.0,
                total_ret=round(tot_ret, 2), avg_trip=round(tot_ret / tot_n, 3) if tot_n else 0.0,
                days=pool_days)
    if a.dual:
        sh = [r.get('short', {}) for r in pool_trips]
        s_n = sum(x.get('n', 0) for x in sh)
        s_win = sum(x.get('win', 0) for x in sh)
        s_ret = sum(x.get('total_ret', 0) for x in sh)
        pool['short'] = dict(n=s_n, wr=round(100.0 * s_win / s_n, 1) if s_n else 0.0,
                             total_ret=round(s_ret, 2))
        pool['dual_total_ret'] = round(pool['total_ret'] + pool['short']['total_ret'], 2)

    ver = {'strategy_version': STRATEGY_VERSION, 'engine_full': ENGINE_FULL}
    out = {'date': a.out_suffix, 'engine': 'general', **ver,
           'cfg': {'exit': 'prod-default(atr1.5+time90+trail0.4/0.6)', 'pairing': 'simulate_day 正T' if not a.dual else 'simulate_dual 双向'},
           'last_n': a.last, 'dual': a.dual, 'symbols': results, 'pool': pool,
           'pass_G1': pool['wr'] >= 55.0 if pool['n'] >= 20 else 'insufficient_samples'}
    fn = f'backtest_general_v5_{a.out_suffix}'
    with open(os.path.join(OUT, fn + '.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    build_html(out, os.path.join(OUT, fn + '.html'))
    print(f'\n池级: trips={pool["n"]} WR={pool["wr"]}% net={pool["total_ret"]}% days={pool_days}')
    if a.dual:
        print(f'反T池级: trips={pool["short"]["n"]} WR={pool["short"]["wr"]}% net={pool["short"]["total_ret"]}% | 双向合计净={pool.get("dual_total_ret")}%')
    print(f'G1(WR>=55%, n>=20): {out["pass_G1"]}')


def build_html(out, path):
    dual = out.get('dual', False)
    head_cols = '<th>代码</th><th>名称</th><th>类型</th><th>天数</th><th>区间</th><th>正T trips</th><th>正T WR</th><th>正T 净%</th>' + ('<th>反T trips</th><th>反T WR</th><th>反T 净%</th>' if dual else '') + '</tr>'
    rows = ''
    for sym, r in out['symbols'].items():
        if 'error' in r:
            rows += f'<tr><td>{sym}</td><td>{r.get("name","")}</td><td colspan="{8 if not dual else 11}">SKIP {r["error"]}</td></tr>'
            continue
        rows += f'<tr><td>{sym}</td><td>{r["name"]}</td><td>{r["kind"]}</td><td>{r["days"]}</td>' \
                f'<td>{r.get("first","")}~{r.get("last","")}</td><td>{r["n"]}</td>' \
                f'<td>{r["wr"]}%</td><td>{r["total_ret"]}%</td>'
        if dual:
            sh = r.get('short', {})
            rows += f'<td>{sh.get("n",0)}</td><td>{sh.get("wr",0)}%</td><td>{sh.get("total_ret",0)}%</td>'
        rows += '</tr>'
    p = out['pool']
    verdict = out['pass_G1']
    vtext = {'pass': '✅ PASS（n≥20 且 WR≥55%）', 'fail': '❌ FAIL（WR<55% 或样本不足）'}.get(
        'pass' if verdict is True else 'fail', f'insufficient（{verdict}）')
    pool_extra = ''
    if dual:
        pool_extra = f'<br>反T池级: trips={p["short"]["n"]} WR={p["short"]["wr"]}% net={p["short"]["total_ret"]}% ｜ 双向合计净={p.get("dual_total_ret")}%'
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>通用算法 v5 离线长回测 {out['date']}</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:20px">
<div style="max-width:1100px;margin:0 auto">
<h2 style="color:#1f2a44">通用算法 v5/GT-1.0 离线长回测 · {out['date']}（{'双向' if dual else '正T'}）</h2>
<p style="color:#666">数据源: F:/keyfactor_data/1m（tickflow 真实 1m）｜ 出场: 生产默认(atr1.5+time90+trail0.4/0.6) ｜ 配对: {'simulate_dual（正T B→S + 反T S→B）' if dual else 'simulate_day 正T(B→S)'}</p>
<div style="background:#fff;border-radius:10px;padding:16px;border-left:4px solid {'#2e7d32' if verdict is True else '#d32f2f'}">
<b>池级判定: {vtext}</b><br>
池级: trips={p['n']}  WR={p['wr']}%  net={p['total_ret']}%  (交易日合计 {p.get('days',0)}){pool_extra}</div>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;width:100%;background:#fff;margin-top:14px">
<tr style="background:#eef2f7">{head_cols}
{rows}</table>
<p style="color:#888;font-size:12px;margin-top:10px">口径声明: 信号质量回测（非生产 monitor 完整状态机：无冷却/仓位/regime 门控）；n&lt;20 不判 G1。反T（先卖后买）仅用于量化 S 侧信号质量，A股 T+1 实际执行需底仓配合。</p>
</div></body></html>"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML -> {path}')


if __name__ == '__main__':
    main()
