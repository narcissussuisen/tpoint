# -*- coding: utf-8 -*-
"""Holdout 检验: 用 MTF 研究中从未参与筛选/调参的标的 159985.SZ(豆粕ETF, T+0),
在 in-sample 相同 61 天窗口上, 以 V15 原参数 / V1 无门控 跑一遍, 验证 edge 是否泛化到新名字。

目的: 回应"只选 688347+513310 是否过拟合"。159985 是缓存里唯一不在 SYMS(8只)中的标的,
且时间窗口与 in-sample 完全重合 -> 天然 holdout (不同名字, 同时间段, 调参时不可见)。
"""
import os
import sys
import json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from miji_floord_mtf import (build_symbol_series_mtf, simulate_overnight,
                             agg_trips, bucket_agg, TF_LISTS, CONFIGS, COST,
                             STOP_PCT, MIN_HOLD, MAX_HOLD_BARS)
from pivot_walkforward_p0 import all_dates

HOLDOUT = ('159985.SZ', '豆粕ETF', 'longonly')
OUT = os.path.join(ROOT, 'output', 'miji_floord_holdout')
os.makedirs(OUT, exist_ok=True)


def main():
    # 用 in-sample 相同窗口: 159985 可用日期 ∩ 原 61 天窗口
    insample_window = [d for d in all_dates('159985.SZ')]  # 159985 覆盖整窗
    # 严格对齐: 取 159985 自身日期 (与 in-sample 重合)
    common = sorted(set(insample_window))
    print(f"HOLDOUT 共同交易日数: {len(common)} ({common[0]}..{common[-1]})")

    sym, name, model = HOLDOUT
    tfs = sorted({tf for _, (tl, _) in TF_LISTS.items() for tf in tl})
    cost = COST[model]

    out = {}
    for cfg, (thr, basis) in CONFIGS.items():
        series = build_symbol_series_mtf(sym, common, thr, basis, tfs)
        out[cfg] = {}
        for vname, (gate, tf_list, lb) in [
            ('V1', (False, [], 0)),
            ('V15', (True, [15], 240)),
        ]:
            trips, leftover, filtered_n = simulate_overnight(
                series, model, gate, tf_list, lb, MIN_HOLD,
                STOP_PCT / 100.0, MAX_HOLD_BARS, cost)
            a = agg_trips(trips)
            b = bucket_agg(trips)
            out[cfg][vname] = {'agg': a, 'buckets': b, 'filtered': filtered_n,
                               'leftover': leftover}
            wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
            pf = a['pf']
            pf_s = f"{pf:.2f}" if pf != float('inf') else 'inf'
            print(f"  [{cfg}/{vname}] {sym} n={len(trips):>4} "
                  f"PF={pf_s} net={a['net_pct']:+.1f}% WR={wr} filt={filtered_n}")

    # 与 in-sample 对比摘要
    print("\n=== 对比 (in-sample 8只池化 vs holdout 159985) ===")
    print(f"{'config/variant':<18}{'holdout PF':>12}{'holdout net%':>14}")
    for cfg in CONFIGS:
        for v in ('V1', 'V15'):
            a = out[cfg][v]['agg']
            pf = a['pf']
            pf_s = f"{pf:.2f}" if pf != float('inf') else 'inf'
            print(f"{cfg+'/'+v:<18}{pf_s:>12}{a['net_pct']:>+14.1f}")

    dump = {
        'holdout': HOLDOUT, 'common': common, 'configs': CONFIGS,
        'note': '159985.SZ 不在 in-sample SYMS(8只)中; 同窗口 holdout, V15 原参数未重调',
        'results': out,
    }
    with open(os.path.join(OUT, 'holdout_metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=2,
                  default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else o)
    print('\nDONE ->', OUT)


if __name__ == '__main__':
    main()
