# -*- coding: utf-8 -*-
"""scripts/topbot_diag.py — GT-TB 表现恶化归因诊断（低割高买反向操作识别）

针对 GT-TB（v10.9.0 ML 过滤）逐标的逐笔 round-trip 诊断：
  1. 输出每笔交易明细（entry/exit 时间、价格、reason、净收益、持仓 bar 数）
  2. 出场位置归因：exit 后 15min 内价格是否高于 exit（= 割在局部低点）
  3. 反向操作识别：exit 后 15min 内出现同向/反向新信号且价格更差（低卖→高买 / 高买→低卖）
  4. 量化拖累：割肉亏损 + 回补亏损合计占双向净收益的比例

输出：output/topbot_diag_2026-08-26.json + 控制台汇总
"""
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from exit_manager import make_config, simulate_day  # noqa: E402
from simulate_bidirectional import simulate_bidirectional  # noqa: E402
from daily_signal_review import build_data  # noqa: E402

DATE = '2026-08-26'
POOL = [('588170.SH', '科创半导体ETF'), ('300759.SZ', '康龙化成'), ('600721.SH', '百花医药')]
F_DATA_DIR = r'F:/keyfactor_data/1m'
MODEL_PATH = os.path.join(ROOT, 'data', 'ml', 'topbottom_xgb.json')
OUT_PATH = os.path.join(ROOT, 'output', f'topbot_diag_{DATE}.json')

EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                       use_fixed_stop=True, fixed_stop_pct=1.5)
EXIT_CFG_SHORT = make_config()
ML_FEATURES = ['vwap_dev', 'rsi', 'trend', 'atr_pct',
               'tick_trade_count', 'tick_buy_ratio', 'tick_large_tape_count', 'tick_vwap_dev',
               'tick_hilo_range_pct', 'tick_direction_flow', 'tick_same_price_tape']
LOOKAHEAD = 15   # 出场后回看窗口（分钟）
REBAL_THR = 0.003  # 反向操作判定：价格差 ≥0.3%


def _load_ml():
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        import xgboost as xgb
        m = xgb.XGBClassifier()
        m.load_model(MODEL_PATH)
        return m
    except Exception:
        return None


def _score(ml, sigs, data):
    if ml is None or not sigs:
        return [(s, 1.0) for s in sigs]
    n = data['n']; c = data['c']
    out = []
    for s in sigs:
        i = s['idx']
        row = {
            'vwap_dev': float((c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0) if data['vwap'][i] > 0 else 0.0,
            'rsi': float(data['rsi'][i]),
            'trend': int(data['trend'][i]),
            'atr_pct': float(data['atr'][i] / s['price'] * 100.0),
        }
        for col in ML_FEATURES[4:]:
            row[col] = 0.5
        p = float(ml.predict_proba(pd.DataFrame([row])[ML_FEATURES])[0, 1])
        out.append((s, p))
    return out


def _analyze(sym, name, ml, thr=0.5):
    fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(fp, encoding='utf-8-sig')
    df['trade_date'] = df['trade_date'].astype(str)
    d = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
    if len(d) < 60:
        return None
    pc = float(d['close'].iloc[0]) * 0.999
    data = build_data(d, pc)
    sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
    scored = _score(ml, sigs, data)
    kept = [s for s, p in scored if p >= thr]

    long_trips = simulate_day(kept, data, EXIT_CFG, cost=None)
    short_trips = simulate_bidirectional(kept, data, config=EXIT_CFG_SHORT, cost=None)

    c = d['close'].values
    times = d['trade_time'].astype(str).values
    n = len(d)

    def trip_detail(t, side):
        ei, xi = t['entry_idx'], t['exit_idx']
        # 出场后走势
        fut = c[xi + 1:min(xi + 1 + LOOKAHEAD, n)]
        fwd_max = float(np.max(fut)) if len(fut) else np.nan
        fwd_min = float(np.min(fut)) if len(fut) else np.nan
        # 割肉判定：出场后 15min 内最高价 > 出场价 + 0.3%（说明割在局部低点）
        cut_low = (not np.isnan(fwd_max)) and fwd_max >= t['exit_price'] * (1 + REBAL_THR)
        # 反向回补判定：出场后 15min 内出现新同向信号且价格更差（低卖→高买）
        rebound = (not np.isnan(fwd_max)) and fwd_max >= t['exit_price'] * (1 + REBAL_THR)
        return {
            'side': side, 'exit_reason': t['exit_reason'],
            'entry_time': times[ei][11:16], 'entry_price': t['entry_price'],
            'exit_time': times[xi][11:16], 'exit_price': t['exit_price'],
            'ret_pct': t['ret_pct'], 'hold_bars': t['hold_bars'],
            'fwd_max_15m': round(fwd_max, 3) if not np.isnan(fwd_max) else None,
            'cut_at_local_low': bool(cut_low),       # 割在局部低点（出场后反弹 ≥0.3%）
            'rebound_after_exit': bool(rebound),     # 出场后反弹（同义，供计数）
            'missed_rebound_pct': round((fwd_max / t['exit_price'] - 1) * 100, 2) if not np.isnan(fwd_max) else None,
        }

    detail = [trip_detail(t, 'B') for t in long_trips] + [trip_detail(t, 'S') for t in short_trips]
    detail.sort(key=lambda x: x['entry_time'])

    # 反向操作对：割肉(低点) → 15min 内同向回补（用 kept 信号时间窗匹配）
    rebal_pairs = []
    for td in detail:
        if not td['cut_at_local_low']:
            continue
        ex_min = td['exit_time']
        for s in kept:
            st = times[s['idx']][11:16]
            if st > ex_min and _min_diff(st, ex_min) <= LOOKAHEAD:
                # 同向回补（B 割肉后买回 / S 平仓后反手卖）
                same_side = (s['type'] == 'B' and td['side'] == 'B') or (s['type'] == 'S' and td['side'] == 'S')
                worse = (s['type'] == 'B' and s['price'] >= td['exit_price'] * (1 + REBAL_THR)) or \
                        (s['type'] == 'S' and s['price'] <= td['exit_price'] * (1 - REBAL_THR))
                if same_side and worse:
                    rebal_pairs.append({
                        'cut': {'time': td['exit_time'], 'price': td['exit_price'],
                                'reason': td['exit_reason'], 'ret_pct': td['ret_pct']},
                        'rebuy': {'time': st, 'price': round(float(s['price']), 3),
                                  'type': s['type'], 'p': round(float(dict(scored)[s['idx']]), 2)},
                        'worse_pct': round((s['price'] / td['exit_price'] - 1) * 100, 2),
                    })
                    break

    losses = [t for t in detail if t['ret_pct'] < 0]
    cut_loss = sum(t['ret_pct'] for t in detail if t['cut_at_local_low'] and t['ret_pct'] < 0)
    total_pnl = sum(t['ret_pct'] for t in detail)
    return {
        'sym': sym, 'name': name,
        'n_raw': len(scored), 'n_kept': len(kept), 'n_trips': len(detail),
        'total_pnl_pct': round(total_pnl, 3),
        'n_loss': len(losses),
        'n_cut_at_low': sum(1 for t in detail if t['cut_at_local_low']),
        'cut_loss_pct': round(cut_loss, 3),
        'cut_loss_share_pct': round(cut_loss / total_pnl * 100, 1) if total_pnl != 0 else 0.0,
        'rebal_pairs': rebal_pairs,
        'trips': detail,
    }


def _min_diff(t1, t2):
    h1, m1 = int(t1[:2]), int(t1[3:5])
    h2, m2 = int(t2[:2]), int(t2[3:5])
    return (h1 * 60 + m1) - (h2 * 60 + m2)


def main():
    ml = _load_ml()
    print(f'GT-TB 归因诊断 {DATE} ｜ ML loaded={ml is not None}')
    out = {'date': DATE, 'per_sym': []}
    for sym, name in POOL:
        try:
            r = _analyze(sym, name, ml)
        except Exception as e:
            print(f'  {sym}: 失败 {e!r}')
            continue
        if r is None:
            print(f'  {sym}: 当日数据不足')
            continue
        out['per_sym'].append(r)
        print(f"\n=== {sym} {name} ===")
        print(f"  信号 {r['n_raw']}→kept {r['n_kept']}｜ 回合 {r['n_trips']}｜ 净 {r['total_pnl_pct']:+.2f}%"
              f"｜ 亏损 {r['n_loss']} 笔｜ 割在低点 {r['n_cut_at_low']} 笔"
              f"｜ 割肉拖累 {r['cut_loss_pct']:+.2f}%（占净 {r['cut_loss_share_pct']}%）")
        for t in r['trips']:
            tag = ' ⚠️割低点' if t['cut_at_local_low'] else ''
            print(f"  {t['side']} {t['entry_time']}→{t['exit_time']} [{t['exit_reason']}] "
                  f"entry {t['entry_price']} exit {t['exit_price']} ret {t['ret_pct']:+.2f}% "
                  f"hold {t['hold_bars']}b 后15m高 {t['fwd_max_15m']}{tag}")
        for p in r['rebal_pairs']:
            print(f"  🔁 反向: 割肉@{p['cut']['time']}({p['cut']['reason']},{p['cut']['ret_pct']:+.2f}%)"
                  f" → 同向回补@{p['rebuy']['time']} {p['rebuy']['type']}({p['rebuy']['price']},p={p['rebuy']['p']})"
                  f" 价差 {p['worse_pct']:+.2f}%")
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n明细已落盘: {OUT_PATH}')


if __name__ == '__main__':
    main()
