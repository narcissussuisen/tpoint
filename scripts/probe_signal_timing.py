# -*- coding: utf-8 -*-
"""统计 miji 信号的时间分布，验证早盘信号占比（开仓纪律设计的现实约束）"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ['MACD_GATE_MODE'] = 'floor'

from core.miji_alpha import compute_miji_indicators, detect_miji_signals
from scripts.backtest_screener import load_1m_csv, group_by_day, day_prev_close

SYMBOLS = {
    '688146.SH': 'F:/keyfactor_data/1m/688146.SH_1m.csv',
    '600206.SH': 'F:/keyfactor_data/1m/600206.SH_1m.csv',
    '688347.SH': 'F:/keyfactor_data/1m/688347.SH_1m.csv',
    '600584.SH': 'F:/keyfactor_data/1m/600584.SH_1m.csv',
    '688766.SH': 'F:/keyfactor_data/1m/688766.SH_1m.csv',
    '161129.SZ': 'F:/keyfactor_data/1m/161129.SZ_1m.csv',
    '513310.SH': 'F:/keyfactor_data/1m/513310.SH_1m.csv',
    '688111.SH': 'F:/keyfactor_data/1m/688111.SH_1m.csv',
}

BUCKETS = ['09:30-09:45', '09:45-10:00', '10:00-10:30', '10:30-11:30', '13:00-14:00', '14:00-15:00']


def bucket_of(t):
    if t < '09:45':
        return BUCKETS[0]
    if t < '10:00':
        return BUCKETS[1]
    if t < '10:30':
        return BUCKETS[2]
    if t < '11:30':
        return BUCKETS[3]
    if t < '14:00':
        return BUCKETS[4]
    return BUCKETS[5]


def main():
    agg_b = {k: 0 for k in BUCKETS}
    agg_s = {k: 0 for k in BUCKETS}
    tot_b = tot_s = 0
    for sym, path in SYMBOLS.items():
        if not os.path.exists(path):
            print(f'  ⚠️ 缺数据: {sym}')
            continue
        df = load_1m_csv(path)
        days = group_by_day(df)
        b_t = {k: 0 for k in BUCKETS}
        s_t = {k: 0 for k in BUCKETS}
        tb = ts = 0
        for date, sub in days:
            pc = day_prev_close(df, date)
            if pc is None or pc <= 0:
                continue
            o = sub['open'].values.astype(float)
            h = sub['high'].values.astype(float)
            lo = sub['low'].values.astype(float)
            c = sub['close'].values.astype(float)
            v = sub['volume'].values.astype(float)
            data = compute_miji_indicators(o, h, lo, c, v, pc)
            sigs = detect_miji_signals(data, pc)
            times = sub['trade_time'].astype(str).str[11:16].values
            for s in sigs:
                bkt = bucket_of(times[s['idx']])
                if s['type'] == 'B':
                    b_t[bkt] += 1
                    tb += 1
                else:
                    s_t[bkt] += 1
                    ts += 1
        print(f'{sym}: B {tb} / S {ts}')
        for k in BUCKETS:
            agg_b[k] += b_t[k]
            agg_s[k] += s_t[k]
        tot_b += tb
        tot_s += ts
    print(f'\n===== 全部 {len(SYMBOLS)} 标的合计: B {tot_b} / S {tot_s} =====')
    print(f'{"时段":<14}{"B":>7}{"B%":>8}{"S":>7}{"S%":>8}')
    for k in BUCKETS:
        print(f'{k:<14}{agg_b[k]:>7}{agg_b[k]/max(tot_b,1)*100:>7.1f}%{agg_s[k]:>7}{agg_s[k]/max(tot_s,1)*100:>7.1f}%')
    early_b = agg_b[BUCKETS[0]] + agg_b[BUCKETS[1]]
    early_s = agg_s[BUCKETS[0]] + agg_s[BUCKETS[1]]
    print(f'\n开盘30分钟(09:30-10:00): B {early_b}/{tot_b} = {early_b/max(tot_b,1)*100:.1f}%, '
          f'S {early_s}/{tot_s} = {early_s/max(tot_s,1)*100:.1f}%')


if __name__ == '__main__':
    main()
