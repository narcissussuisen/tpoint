# -*- coding: utf-8 -*-
r"""player_level_tripstats.py — 选手方法线（T2）对表用的逐笔统计（2026-09-28，研究线）

复刻 random_control_validator.validate_symbol 的真实信号 round-trip 口径
（同 09-27 判决产物 output/random_control_300010SZ_20260927*.json），
但保留逐笔 trip 明细，计算 T2 验收所需指标：
  盈亏比 = mean(win)/|mean(loss)|；单笔净差≥+1% 达成率（K1「1~3点」锚）；
  亏损侧分布（中位/最大亏）；逐日净收益序列（连续为负判定）。

两臂：
  baseline = 09-27 判决基线（GENERAL_DEFAULT + signal_gap=6，即 regime_gate OFF 臂）
  regime   = --set regime_gate=true（同 09-27 regime-on 臂）

⚠️ 研究线脚本（scripts/research/），永不进生产。口径与验证器完全一致：
  has_base 读 monitor_config（300010=False ⇒ 只做正T trips，与实盘一致）；
  trail 0.4/0.6 + s_signal_exit + cost_for_symbol；entry=信号 bar close；
  ⚠️ samebar 口径（信号 bar 即成交，系统性偏乐观，T0 水印）。

用法：venv\Scripts\python.exe scripts\research\player_level_tripstats.py [--sym 300010.SZ] [--days 60]
"""
import argparse
import json
import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from random_control_validator import (  # noqa: E402
    find_data_path, load_days_full, SIGNAL_GAP, has_base_of)
from daily_signal_review import build_data  # noqa: E402
from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from exit_manager import make_config, cost_for_symbol  # noqa: E402
from simulate_position_sm import simulate_position_sm  # noqa: E402
import pandas as pd  # noqa: E402


def build_recs(sym, days_req, cfg_overrides=None):
    """与 validate_symbol L337-363 完全同口径的信号日序列。"""
    path = find_data_path(sym)
    days_all, name = load_days_full(path)
    recs = []
    prev_close = None
    for d in sorted(days_all.keys()):
        dd = days_all[d]
        c = dd['c']
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': dd['o'], 'high': dd['h'], 'low': dd['lo'],
                           'close': c, 'volume': dd['v'],
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        prev_close = c[-1]
        if data is None:
            continue
        cfg = replace(GENERAL_DEFAULT, signal_gap=SIGNAL_GAP)
        for k, v in (cfg_overrides or {}).items():
            if hasattr(cfg, k):
                cfg = replace(cfg, **{k: v})
        sigs = detect_signals_general(data, pc, cfg)
        recs.append(dict(date=d, pc=float(pc), sigs=sigs, data=data))
    return (recs[-days_req:] if days_req > 0 else recs), name


def arm_stats(sym, recs, label):
    cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                      trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
    cost = cost_for_symbol(sym)
    hb = has_base_of(sym)
    sigs_by_day = [(r['date'], r['sigs']) for r in recs]
    prices_by_day = [(r['date'], {'o': r['data']['o'], 'h': r['data']['h'],
                                  'lo': r['data']['lo'], 'c': r['data']['c'],
                                  'atr': r['data']['atr'],
                                  'trend': r['data'].get('trend'),
                                  'n': r['data']['n'], 'pc': r['pc'],
                                  'sym': sym, 'date': r['date']}) for r in recs]
    sm = simulate_position_sm(sigs_by_day, prices_by_day,
                              config_long=cfg, config_short=cfg,
                              cost=cost, has_base=hb)
    trips = sm['trips']
    rets = np.array([float(t['ret_pct']) for t in trips]) if trips else np.array([])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    # 逐日净收益（连续为负判定用）：trip 为日内 round-trip，entry_date 即交易日
    day_net = {}
    for t in trips:
        d = t.get('entry_date') or t.get('exit_date') or t.get('date') or ''
        day_net[d] = day_net.get(d, 0.0) + float(t['ret_pct'])
    neg_days = sum(1 for v in day_net.values() if v < 0)
    stats = {
        'arm': label, 'has_base': hb,
        'n_days': len(recs),
        'n_signals': sum(len(r['sigs']) for r in recs),
        'n_trips': len(trips),
        'net_wr': round(float((rets > 0).mean()), 4) if len(rets) else None,
        'mean_net_pp': round(float(rets.mean()), 4) if len(rets) else None,
        'pl_ratio': round(float(wins.mean() / abs(losses.mean())), 3)
        if len(wins) and len(losses) else None,
        'pct_trips_net_ge_1pct': round(float((rets >= 1.0).mean()), 4) if len(rets) else None,
        'pct_trips_net_ge_0_5pct': round(float((rets >= 0.5).mean()), 4) if len(rets) else None,
        'median_loss_pp': round(float(np.median(losses)), 4) if len(losses) else None,
        'mean_loss_pp': round(float(losses.mean()), 4) if len(losses) else None,
        'max_loss_pp': round(float(rets.min()), 4) if len(rets) else None,
        'mean_win_pp': round(float(wins.mean()), 4) if len(wins) else None,
        'median_win_pp': round(float(np.median(wins)), 4) if len(wins) else None,
        'days_negative': neg_days, 'days_total': len(day_net),
    }
    return stats, trips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sym', default='300010.SZ')
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    out = {'sym': args.sym, 'days_req': args.days,
           'caliber': ('与 random_control_validator 09-27 判决同口径：GENERAL_DEFAULT+gap6 '
                       'baseline / +regime_gate=true regime 臂；trail 0.4/0.6 + s_signal_exit '
                       '+ cost_for_symbol；entry=信号 bar close；⚠️ samebar 口径偏乐观'),
           'arms': []}
    for label, ov in (('baseline(OFF臂=09-27基线)', None),
                      ('regime-on', {'regime_gate': True})):
        recs, name = build_recs(args.sym, args.days, ov)
        st, trips = arm_stats(args.sym, recs, label)
        st['name'] = name
        out['arms'].append(st)
        print(json.dumps(st, ensure_ascii=False, indent=1))

    out_path = args.out or os.path.join(
        ROOT, 'output', f'player_level_tripstats_{args.sym.replace(".", "")}.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f'→ {out_path}')


if __name__ == '__main__':
    main()
