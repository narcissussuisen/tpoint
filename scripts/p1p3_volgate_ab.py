# -*- coding: utf-8 -*-
"""
p1p3_volgate_ab.py —— P1.3 量比门控出场感知 A/B 复核

目的：DET 已证明量比门控(vol_ratio_b_max=1.2)不提升信号质量(DA/TEP/EC 全持平)，
本阶段验证其是否仍有「出场风险」价值——即去掉高量比 B(放量急跌/接飞刀)后，
round-trip 净 WR 与单笔盈亏(P/L)是否改善。

方法：复用生产回测链路 build_data → detect_signals_general → simulate_day(正T B→S)
      + exit_manager.make_config 默认(atr1.5+time90+trail0.4/0.6)。
      全池跑两遍：off=无门控 / on=丢弃 vol_ratio>1.2 的 B 信号。
      对比池级净 WR、单笔均值回报(avg_trip=P/L 代理)、总回报、笔数。

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


def run_symbol(sym, gap, vol_gate, use_stop=True):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return None
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cfg = make_config(use_stop=use_stop)
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
        if vol_gate:
            sigs = [s for s in sigs if not (
                s['type'] == 'B' and isinstance(s.get('vol_ratio'), (int, float))
                and s['vol_ratio'] > 1.2)]
        trips.extend(simulate_day(sigs, prices, cfg, cost))
        prev_close = c[-1]
    return trips


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg_trip=0.0, win=0, loss=0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg_trip=round(sum(rets) / n, 3), win=wins, loss=n - wins)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gap', type=int, default=8, help='信号间隔（P1.1 后生产=8）')
    ap.add_argument('--no-atr', action='store_true',
                    help='复刻实盘出场：make_config(use_stop=False) 即无 ATR 硬止损，仅 trailing 0.4/0.6')
    ap.add_argument('--max-syms', type=int, default=0, help='0=全池')
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    if a.max_syms:
        files = files[:a.max_syms]

    sym_summary = {}
    for path in files:
        sym = os.path.basename(path).replace('_1m.csv', '')
        rec = {}
        for gate, key in ((False, 'off'), (True, 'on')):
            trips = run_symbol(sym, a.gap, gate, use_stop=not a.no_atr)
            if trips:
                rec[key] = summarize(trips)
        if rec:
            sym_summary[sym] = rec

    pool = {}
    for key in ('off', 'on'):
        present = [v[key] for v in sym_summary.values() if key in v]
        wins = sum(s['win'] for s in present)
        loss = sum(s['loss'] for s in present)
        n = wins + loss
        tot = sum(s['total_ret'] for s in present)
        pool[key] = dict(
            n=n,
            wr=round(100.0 * wins / n, 1) if n else 0.0,
            total_ret=round(tot, 2),
            avg_trip=round(tot / n, 3) if n else 0.0,
        )

    out = dict(
        meta=dict(gap=a.gap,
                  note='vol-gate A/B: off=无门控(全 B); on=丢弃 vol_ratio>1.2 的 B',
                  exit=('live(no-atr+trail0.4/0.6)' if a.no_atr else 'prod-default(atr1.5+time90+trail0.4/0.6)'),
                  n_syms=len(sym_summary)),
        pool=pool,
        symbols=sym_summary,
    )
    fn = f'p1p3_volgate_ab_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\n=== P1.3 量比门控 A/B（gap={a.gap}, {len(sym_summary)} 标的）===')
    for key in ('off', 'on'):
        p = pool[key]
        print(f'  {key:>3}: n={p["n"]:>6} 净WR={p["wr"]:>5.1f}% 单笔均值={p["avg_trip"]:+.3f}% 总回报={p["total_ret"]:+.2f}%')
    print(f'JSON -> {os.path.join(OUT, fn)}')


if __name__ == '__main__':
    main()
