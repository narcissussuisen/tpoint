# -*- coding: utf-8 -*-
"""scripts/stop_exit_ab.py — 止损推送/执行 A/B 验证（研究态）

问题：生产系统「不推送/不执行止损」是否收益更好？
实验（4 标的全样本）：
  A（现状）  : 正T = trail+FIXSTOP1.5 ｜ 反T = 硬止损atr1.5+trail+time90
  B（去止损）: 正T = 仅 trail+S+EOD  ｜ 反T = 仅 trail+S+EOD（无硬止损/时间止损/固定止损）
指标：双向净收益 / 最大单笔亏损 / WR / 笔数 / EOD 兜底占比
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from exit_manager import make_config, simulate_day, aggregate_metrics  # noqa: E402
from simulate_bidirectional import simulate_bidirectional  # noqa: E402
from daily_signal_review import build_data  # noqa: E402

F_DATA_DIR = r'F:/keyfactor_data/1m'
POOL = ['161129.SZ', '513310.SH', '300759.SZ', '600721.SH']

# A（现状，生产口径）
EXIT_CFG_A = make_config(use_stop=False, use_time=False, use_trailing=True,
                         trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                         use_fixed_stop=True, fixed_stop_pct=1.5)
EXIT_CFG_SHORT_A = make_config()  # 硬止损atr1.5 + time90 + trail0.4/0.6
# B（去止损：仅 trail + S 信号出场 + EOD）
EXIT_CFG_B = make_config(use_stop=False, use_time=False, use_trailing=True,
                         trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                         use_fixed_stop=False)
EXIT_CFG_SHORT_B = make_config(use_stop=False, use_time=False, use_trailing=True,
                               trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                               use_fixed_stop=False)


def _run_sym(df, days, ecfg_long, ecfg_short):
    lt, st = [], []
    for i, day in enumerate(days):
        d = df[df['trade_date'] == day].sort_values('trade_time')
        if len(d) < 10:
            continue
        pc = float(d['close'].iloc[0]) * 0.999
        try:
            data = build_data(d.reset_index(drop=True), pc)
        except Exception:
            continue
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        if not sigs:
            continue
        lt += simulate_day(sigs, data, ecfg_long, cost=None)
        st += simulate_bidirectional(sigs, data, config=ecfg_short, cost=None)
    la, sa = aggregate_metrics(lt), aggregate_metrics(st)
    return {'long_net': la['total_ret'], 'long_wr': la['win_rate'], 'long_n': la['total'],
            'short_net': sa['total_ret'], 'short_wr': sa['win_rate'], 'short_n': sa['total'],
            'long_by_reason': la['by_reason'], 'short_by_reason': sa['by_reason']}


def main():
    print(f'止损 A/B（4 标的全样本）\n')
    rows = []
    for sym in POOL:
        fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
        if not os.path.exists(fp):
            continue
        df = pd.read_csv(fp, encoding='utf-8-sig')
        df['trade_date'] = df['trade_date'].astype(str)
        days = sorted(df['trade_date'].unique())
        a = _run_sym(df, days, EXIT_CFG_A, EXIT_CFG_SHORT_A)
        b = _run_sym(df, days, EXIT_CFG_B, EXIT_CFG_SHORT_B)
        rows.append({'sym': sym, 'days': len(days), 'A': a, 'B': b})
        print(f"{sym}: A 双向{a['long_net'] + a['short_net']:+.1f}% "
              f"(正{a['long_net']:+.1f}/反{a['short_net']:+.1f}) | "
              f"B 双向{b['long_net'] + b['short_net']:+.1f}% "
              f"(正{b['long_net']:+.1f}/反{b['short_net']:+.1f})")

    def agg(key):
        return {'long_net': round(sum(r[key]['long_net'] for r in rows), 2),
                'short_net': round(sum(r[key]['short_net'] for r in rows), 2),
                'total': round(sum(r[key]['long_net'] + r[key]['short_net'] for r in rows), 2),
                'long_wr': round(float(np.mean([r[key]['long_wr'] for r in rows])), 1),
                'short_wr': round(float(np.mean([r[key]['short_wr'] for r in rows])), 1),
                'long_n': sum(r[key]['long_n'] for r in rows),
                'short_n': sum(r[key]['short_n'] for r in rows)}

    s = {'A': agg('A'), 'B': agg('B')}
    print('\n=== 池级汇总 ===')
    for k in ('A', 'B'):
        v = s[k]
        print(f"{k}: 双向{v['total']:+.2f}% | 正T{v['long_net']:+.2f}% (WR{v['long_wr']}%, n={v['long_n']}) "
              f"| 反T{v['short_net']:+.2f}% (WR{v['short_wr']}%, n={v['short_n']})")
    print(f"\nΔ(B-A): 双向 {s['B']['total'] - s['A']['total']:+.2f}pp | "
          f"正T {s['B']['long_net'] - s['A']['long_net']:+.2f}pp | 反T {s['B']['short_net'] - s['A']['short_net']:+.2f}pp")
    # 尾部风险：EOD 兜底占比（A 中因止损离场 vs B 中 EOD 兜底的比例）
    print('\n出场分布（A vs B）:')
    for r in rows:
        la, sa = r['A'], r['B']
        print(f"  {r['sym']}: A 正T{la['long_by_reason']} 反T{sa['short_by_reason']}")
        print(f"           B 正T{lb['long_by_reason'] if (lb := r['B']) else ''} 反T{sb['short_by_reason'] if (sb := r['B']) else ''}")


if __name__ == '__main__':
    main()
