# -*- coding: utf-8 -*-
"""
validate_v3_short_pool.py —— 反T(S→B) + exit_v3 最优参数 扩展池最终验证

最优参数（tune_v3_exit 2026-08-21）：stop_fixed_pct=1.0, time_stop_bars=90, trend_exit=True
数据：F:/keyfactor_data/1m_clean（全池 ≥5 真实天）
输出：output/validate_v3_short_pool_<date>.json + .html
判定：池级 WR ≥55% 且 n ≥ 20 → G1 PASS；并输出标的数/样本量。
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT, STRATEGY_VERSION, ENGINE_FULL
from daily_signal_review import build_data
from exit_manager import cost_for_symbol
from exit_v3 import exit_v3

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

V3_KW = dict(stop_atr_mult=1.2, stop_fixed_pct=1.0, time_stop_bars=90,
             trend_exit=True, use_hard_stop=True, s_signal_exit=True)


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = (np.array([float(x['open']) for x in rs]), np.array([float(x['high']) for x in rs]),
                   np.array([float(x['low']) for x in rs]), np.array([float(x['close']) for x in rs]),
                   np.array([float(x['volume']) for x in rs]))
    return days


def run_short(sym, min_days=5):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days = load_days(path)
    dates = sorted(days.keys())
    if len(dates) < min_days:
        return {'sym': sym, 'error': f'insufficient_days({len(dates)})'}
    cost = cost_for_symbol(sym)
    trips = []
    prev_close = None
    n_ok = 0
    for d in dates:
        o, h, lo, c, v = days[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'vwap': data['vwap'], 'hist': data['hist'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        trips.extend(exit_v3(sigs, prices, direction='short', cost=cost, **V3_KW))
        prev_close = c[-1]
        n_ok += 1
    return {'sym': sym, 'days': n_ok, 'trips': trips}


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg=0.0)
    rets = [float(t['ret_pct']) for t in trips]
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    return dict(n=len(trips), wr=round(100 * wins / len(trips), 1),
                total_ret=round(sum(rets), 2), avg=round(sum(rets) / len(trips), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=200, help='最多验证的标的数（按天数列）')
    ap.add_argument('--min-days', type=int, default=5)
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    # 枚举池
    import glob
    syms = []
    for p in glob.glob(DATA_DIR + '/*_1m.csv'):
        sym = os.path.basename(p).replace('_1m.csv', '')
        with open(p, encoding='utf-8-sig') as f:
            n_days = len({r['trade_date'] for r in csv.DictReader(f)})
        syms.append((sym, n_days))
    syms = sorted(syms, key=lambda x: -x[1])[:a.max_syms]
    print(f'验证池: {len(syms)} 标的（按真实天数降序）')

    results = {}
    all_trips = []
    for sym, nd in syms:
        r = run_short(sym, min_days=a.min_days)
        if 'error' in r:
            results[sym] = {'sym': sym, 'error': r['error']}
            continue
        s = summarize(r['trips'])
        results[sym] = {'sym': sym, 'days': r['days'], **s}
        all_trips.extend(r['trips'])
        print(f'[{sym}] days={r["days"]} n={s["n"]} WR={s["wr"]}% net={s["total_ret"]}%')
    pool = summarize(all_trips)
    n_sym_ok = sum(1 for r in results.values() if 'error' not in r and r['n'] > 0)
    g1 = 'PASS' if (pool['n'] >= 20 and pool['wr'] >= 55.0) else 'FAIL'
    print(f'\n池级: 标的数={n_sym_ok} trips={pool["n"]} WR={pool["wr"]}% net={pool["total_ret"]}% avg={pool["avg"]}% → G1:{g1}')

    ver = {'strategy_version': STRATEGY_VERSION, 'engine_full': ENGINE_FULL}
    out = {'date': a.out_suffix, 'engine': 'general', 'direction': 'short(反T S→B)',
           'exit': 'exit_v3 ' + json.dumps(V3_KW, ensure_ascii=False), **ver,
           'symbols': results, 'pool': pool, 'g1': g1, 'n_sym_ok': n_sym_ok}
    fn = f'validate_v3_short_pool_{a.out_suffix}'
    with open(os.path.join(OUT, fn + '.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # HTML
    rows = ''
    for sym, r in results.items():
        if 'error' in r:
            rows += f'<tr><td>{sym}</td><td colspan="4">SKIP {r["error"]}</td></tr>'
            continue
        rows += f'<tr><td>{sym}</td><td>{r["days"]}</td><td>{r["n"]}</td><td>{r["wr"]}%</td><td>{r["total_ret"]}%</td></tr>'
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>反T+exit_v3 扩展池验证 {out['date']}</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:20px">
<div style="max-width:1100px;margin:0 auto">
<h2 style="color:#1f2a44">反T(S→B) + exit_v3 最优参数 · 扩展池验证 · {out['date']}</h2>
<p style="color:#666">数据: F:/keyfactor_data/1m_clean（全池≥{a.min_days}真实天）｜ exit_v3: hard=1.0% / trend=VWAP反穿+MACD / time=90根</p>
<div style="background:#fff;border-radius:10px;padding:16px;border-left:4px solid {'#2e7d32' if g1=='PASS' else '#d32f2f'}">
<b>池级判定: G1 {'✅ PASS' if g1=='PASS' else '❌ FAIL'}（WR≥55% 且 n≥20）</b><br>
标的数={n_sym_ok}  trips={pool['n']}  WR={pool['wr']}%  net={pool['total_ret']}%  avg={pool['avg']}%</div>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;width:100%;background:#fff;margin-top:14px">
<tr style="background:#eef2f7"><th>标的</th><th>天数</th><th>trips</th><th>WR</th><th>净%</th></tr>
{rows}</table>
<p style="color:#888;font-size:12px;margin-top:10px">口径: 信号质量回测（无冷却/仓位/regime）；A股T+1 反T需底仓配合执行。</p>
</div></body></html>"""
    with open(os.path.join(OUT, fn + '.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'OUT -> {os.path.join(OUT, fn)}.json/.html')


if __name__ == '__main__':
    main()
