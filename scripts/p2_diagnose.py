# -*- coding: utf-8 -*-
"""
p2_diagnose.py —— P2.0 出场弱点诊断（量化 P/L 盈亏比，定位最差案例）

目的：当前出场(atr? + time90 + trail0.4/0.6) 盈亏比仅 0.6-0.9，是 v5 核心弱点。
本脚本在"当前实盘配置"（gap=8、无门控、无ATR硬止损 + trailing0.4/0.6）下跑全池 round-trip，
收集每笔 trip 的 ret_pct，输出：
  - 池级 P/L 盈亏比 = 平均盈利 / |平均亏损|
  - 净 WR、单笔均值
  - 按标的 P/L 盈亏比 / WR（定位弱标的）
  - 最差 20 笔（最大亏损）含 标的/日期
  - 亏损分布（亏 >1% / >2% / >3% 的笔数占比）
供 P2.1 出场修复设计定靶心。

数据：F:/keyfactor_data/1m_clean（与 DET / 生产回测同源）
"""
import sys, csv, json, os, argparse, glob, datetime
import numpy as np
import pandas as pd
from dataclasses import replace

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = r'F:/WorkBuddyItem/automation-2026-08-03-09-39-31'


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


def run_symbol_trips(sym, gap, fixed_stop=None):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return []
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cfg = make_config(use_stop=False,                              # 实盘口径：无ATR硬止损
                      use_fixed_stop=(fixed_stop is not None),     # P2.1：固定百分比硬止损
                      fixed_stop_pct=(fixed_stop if fixed_stop is not None else 1.5))
    cost = cost_for_symbol(sym)
    trips = []
    prev_close = None
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c,
                           'volume': v, 'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data['trend'], 'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, replace(GENERAL_DEFAULT, signal_gap=gap))
        for t in simulate_day(sigs, prices, cfg, cost):
            t['sym'] = sym
            t['date'] = d
            trips.append(t)
        prev_close = c[-1]
    return trips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gap', type=int, default=8)
    ap.add_argument('--fixed-stop', type=float, default=None,
                    help='固定百分比硬止损幅度(%%)，None=关闭(实盘当前口径)。用于 P2.1 扫描 -1.5/-2.0/-2.5')
    ap.add_argument('--max-syms', type=int, default=0)
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    if a.max_syms:
        files = files[:a.max_syms]

    all_trips = []
    sym_rec = {}
    for path in files:
        sym = os.path.basename(path).replace('_1m.csv', '')
        trips = run_symbol_trips(sym, a.gap, a.fixed_stop)
        if not trips:
            continue
        rets = [float(t['ret_pct']) for t in trips]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        aw = float(np.mean(wins)) if wins else 0.0
        al = float(np.mean(losses)) if losses else 0.0
        pl = round(aw / abs(al), 3) if al else None
        sym_rec[sym] = dict(
            n=len(trips), wr=round(100.0 * len(wins) / len(trips), 1),
            pl_ratio=pl, avg_win=round(aw, 4), avg_loss=round(al, 4),
            avg_trip=round(float(np.mean(rets)), 4),
            total_ret=round(float(sum(rets)), 2),
            worst=float(min(rets)),
        )
        all_trips.extend(trips)

    rets = [float(t['ret_pct']) for t in all_trips]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    pool = dict(
        n=len(rets),
        wr=round(100.0 * len(wins) / len(rets), 1),
        pl_ratio=round(aw / abs(al), 3) if al else None,
        avg_win=round(aw, 4), avg_loss=round(al, 4),
        avg_trip=round(float(np.mean(rets)), 4),
        total_ret=round(float(sum(rets)), 2),
        pct_loss_gt1=round(100.0 * sum(1 for r in losses if r < -1.0) / len(losses), 1) if losses else None,
        pct_loss_gt2=round(100.0 * sum(1 for r in losses if r < -2.0) / len(losses), 1) if losses else None,
        pct_loss_gt3=round(100.0 * sum(1 for r in losses if r < -3.0) / len(losses), 1) if losses else None,
    )
    worst20 = sorted(all_trips, key=lambda t: float(t['ret_pct']))[:20]
    worst_list = [dict(sym=t['sym'], date=t['date'], ret=round(float(t['ret_pct']), 2)) for t in worst20]

    # 按 P/L 盈亏比排序的弱标的（P/L<1 且 n>=50）
    weak = sorted([(s, v) for s, v in sym_rec.items() if v['n'] >= 50 and (v['pl_ratio'] or 0) < 1.0],
                  key=lambda kv: kv[1]['pl_ratio'] or 0)[:15]

    out = dict(
        meta=dict(gap=a.gap,
                  fixed_stop=(a.fixed_stop if a.fixed_stop is not None else 'off'),
                  exit='live(no-atr+trail0.4/0.6)' + (f'+FIXSTOP{a.fixed_stop}' if a.fixed_stop else ''),
                  note='P2.0/2.1 出场弱点诊断 + 固定止损扫描'),
        pool=pool,
        worst20=worst_list,
        weak_symbols=[dict(sym=s, **v) for s, v in weak],
        symbols=sym_rec,
    )
    fn = f'p2_diagnose_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    fs_tag = f' FIXSTOP={a.fixed_stop}%' if a.fixed_stop is not None else ' FIXSTOP=off'
    print(f'\n=== P2.0/2.1 出场诊断（gap={a.gap},{fs_tag}, {len(sym_rec)} 标的, {len(rets)} 笔）===')
    print(f"  池级: 净WR={pool['wr']}% P/L盈亏比={pool['pl_ratio']} 单笔={pool['avg_trip']:+.4f}%")
    print(f"  平均盈={pool['avg_win']:+.4f}% 平均亏={pool['avg_loss']:+.4f}%")
    print(f"  亏损中 >1%/{'>2%'}/{'>3%'} 占比 = {pool['pct_loss_gt1']}/{pool['pct_loss_gt2']}/{pool['pct_loss_gt3']}%")
    print(f"  最差单笔: {worst_list[0]}")
    print(f"  P/L<1 弱标的数(n>=50): {len(weak)}")
    print(f'JSON -> {os.path.join(OUT, fn)}')


if __name__ == '__main__':
    main()
