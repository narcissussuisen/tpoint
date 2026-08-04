# -*- coding: utf-8 -*-
"""m_factor 深度：背离强度(hist差)与盈利的关系 + 卖点信号质量分解"""
import os, sys
os.environ['MACD_GATE_MODE'] = 'floor'
sys.path.insert(0, '.')
import numpy as np
from core import miji_alpha
from scripts.backtest_screener import load_1m_csv, group_by_day, day_prev_close

FW = 24

def analyze(sym):
    df = load_1m_csv(f'F:/keyfactor_data/1m/{sym}_1m.csv')
    rows = []  # (mf, hist_diff, fwd)
    for date, sub in group_by_day(df):
        pc = day_prev_close(df, date)
        if pc is None or pc <= 0:
            continue
        o = sub['open'].values.astype(float)
        h = sub['high'].values.astype(float)
        lo = sub['low'].values.astype(float)
        c = sub['close'].values.astype(float)
        v = sub['volume'].values.astype(float)
        n = len(c)
        data = miji_alpha.compute_miji_indicators(o, h, lo, c, v, pc)
        dif, dea, hist = data['dif'], data['dea'], data['hist']
        for i in range(40, n - FW):
            mf, md = miji_alpha.macd_divergence_signal(h, lo, c, dif, dea, hist, i)
            if mf == 0:
                continue
            w = 30
            if mf == 1:
                prev_hist_min = hist[max(0, i - w):i].min()
                hist_diff = hist[i] - prev_hist_min  # 正=抬高越多背离越强
                fwd = (c[i + FW] - c[i]) / c[i] * 100
            else:
                prev_hist_max = hist[max(0, i - w):i].max()
                hist_diff = prev_hist_max - hist[i]  # 正=降低越多背离越强
                fwd = (c[i] - c[i + FW]) / c[i] * 100
            rows.append((mf, hist_diff, fwd))
    return rows


if __name__ == '__main__':
    all_rows = []
    for sym in ['688146.SH', '600206.SH', '688347.SH', '688111.SH',
                '688766.SH', '600584.SH', '161129.SZ', '513310.SH']:
        all_rows.extend(analyze(sym))
    arr = np.array([(r[0], r[1], r[2]) for r in all_rows])
    print(f'总信号 {len(arr)}')
    # 按背离强度分桶（买）
    for mf, label in [(1, '买点B'), (-1, '卖点S')]:
        sub = arr[arr[:, 0] == mf]
        print(f'\n=== {label} 按背离强度分桶（{len(sub)} 个）===')
        qs = np.percentile(sub[:, 1], [0, 25, 50, 75, 100])
        print(f'  背离强度分位: {np.round(qs, 4)}')
        # 分 4 桶
        for b in range(4):
            lo_q = np.percentile(sub[:, 1], b * 25)
            hi_q = np.percentile(sub[:, 1], (b + 1) * 25)
            mask = (sub[:, 1] >= lo_q) & (sub[:, 1] <= hi_q)
            bucket = sub[mask]
            if len(bucket) == 0:
                continue
            mean = bucket[:, 2].mean()
            pos = (bucket[:, 2] > 0).mean() * 100
            print(f'  桶{b+1} [{lo_q:.4f},{hi_q:.4f}] n={len(bucket):5d} 均值={mean:+.4f}% 正率={pos:.1f}%')
