# -*- coding: utf-8 -*-
"""
tune_v3_exit.py —— exit_v3 参数扫描（干净数据，聚焦反T S→B 生产化）

目标：反T（S→B）+ exit_v3 三条件止损 池级 WR ≥55%（对齐 G1 / R4 验收）。
扫描：stop_fixed_pct ∈ {0.5, 0.8, 1.0} × time_stop_bars ∈ {45, 60, 90} × trend_exit ∈ {True, False}
输出：output/tune_v3_exit_<date>.json + 控制台最优表
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from daily_signal_review import build_data
from exit_manager import cost_for_symbol
from exit_v3 import exit_v3

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')
SYMS = ['161129.SZ', '513310.SH', '688111.SH']


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


def run_short(sym, **kw):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return []
    days = load_days(path)
    cost = cost_for_symbol(sym)
    trips = []
    prev_close = None
    for d in sorted(days):
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
        trips.extend(exit_v3(sigs, prices, direction='short', cost=cost, **kw))
        prev_close = c[-1]
    return trips


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg=0.0)
    rets = [float(t['ret_pct']) for t in trips]
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    return dict(n=len(trips), wr=round(100 * wins / len(trips), 1),
                total_ret=round(sum(rets), 2), avg=round(sum(rets) / len(trips), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default=','.join(SYMS))
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]
    results = []
    for sp in (0.5, 0.8, 1.0):
        for tb in (45, 60, 90):
            for te in (True, False):
                kw = dict(stop_fixed_pct=sp, time_stop_bars=tb, trend_exit=te,
                          stop_atr_mult=1.2, use_hard_stop=True, s_signal_exit=True)
                all_trips = []
                per = {}
                for sym in syms:
                    t = run_short(sym, **kw)
                    per[sym] = summarize(t)
                    all_trips.extend(t)
                ps = summarize(all_trips)
                tag = f'sp={sp} tb={tb} trend={te}'
                print(f'[{tag}] n={ps["n"]} WR={ps["wr"]}% net={ps["total_ret"]}% avg={ps["avg"]}%')
                results.append({'stop_fixed_pct': sp, 'time_stop_bars': tb, 'trend_exit': te,
                                'pool': ps, 'per_symbol': per})
    best = sorted(results, key=lambda r: -r['pool']['wr'])
    print('\n=== 反T WR top5（目标 ≥55%）===')
    for r in best[:5]:
        p = r['pool']
        print(f"sp={r['stop_fixed_pct']} tb={r['time_stop_bars']} trend={r['trend_exit']} "
              f"n={p['n']} WR={p['wr']}% net={p['total_ret']}% avg={p['avg']}%")
    fn = f'tune_v3_exit_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump({'date': a.out_suffix, 'results': results}, f, ensure_ascii=False, indent=2)
    print(f'JSON -> {os.path.join(OUT, fn)}')


if __name__ == '__main__':
    main()
