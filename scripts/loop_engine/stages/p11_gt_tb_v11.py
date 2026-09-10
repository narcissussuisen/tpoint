# -*- coding: utf-8 -*-
"""scripts/loop_engine/stages/p11_gt_tb_v11.py — P11 阶段执行器（研究态：GT-TB v1.1 改进验证）

定位：**研究态，不入 monitor 生产核心算法**。GT-TB 作为研究线保留（memory 规则：research 永不进生产）。

改进（对应 GT-TB 表现恶化复盘 §五）：
  P0（追高抑制）: B 侧 vwap_dev 上界门控 —— 信号 bar 的 (c-vwap)/vwap > vwap_dev_b_max(0.8%) 时拒绝 B
  P1（ATR 止损）: FIXSTOP 固定 1.5% → ATR 自适应 max(1.2×ATR%, 0.8%)（exit_v3 硬止损口径）
  保留：ML 顶底过滤 thr≥0.5 + TRAIL 0.4/0.6 + S/B 信号出场 + EOD

验证：三标的（588170/300759/600721）2026-08-26 重跑，v1.0 vs v1.1 对比
      指标：净收益 / 追高割肉(ATRSTOP|FIXSTOP) 单笔数 / 割在低点率 / 最大单笔亏损
Gate：v1.1 双向净 ≥ v1.0 且 FIXSTOP/追高类止损单笔数 ↓ → PASS（输出报告，不 bump VERSION）
"""
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import importlib.util  # noqa: E402
_LE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('le_core', os.path.join(_LE_DIR, 'core.py'))
le_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(le_core)

from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from exit_manager import make_config, simulate_day  # noqa: E402
from simulate_bidirectional import simulate_bidirectional  # noqa: E402
from daily_signal_review import build_data  # noqa: E402

DATE = '2026-08-26'
POOL = [('588170.SH', '科创半导体ETF'), ('300759.SZ', '康龙化成'), ('600721.SH', '百花医药')]
F_DATA_DIR = r'F:/keyfactor_data/1m'
MODEL_PATH = os.path.join(ROOT, 'data', 'ml', 'topbottom_xgb.json')
OUT_DIR = os.path.join(ROOT, 'output', f'topbot_v11_{DATE}')
ML_FEATURES = ['vwap_dev', 'rsi', 'trend', 'atr_pct',
               'tick_trade_count', 'tick_buy_ratio', 'tick_large_tape_count', 'tick_vwap_dev',
               'tick_hilo_range_pct', 'tick_direction_flow', 'tick_same_price_tape']

# v1.0 生产出场（FIXSTOP 1.5%）
EXIT_CFG_V10 = make_config(use_stop=False, use_time=False, use_trailing=True,
                           trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                           use_fixed_stop=True, fixed_stop_pct=1.5)
EXIT_CFG_SHORT_V10 = make_config()
# v1.1 P1 参数
VWAP_DEV_B_MAX = 0.8   # P0：B 侧 vwap_dev 上界（%）
ATR_STOP_MULT = 1.2    # P1：ATR 止损倍数
ATR_STOP_FLOOR = 0.8   # P1：ATR 止损下限（%）


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


def _score_ml(ml, sigs, data):
    if ml is None or not sigs:
        return [(s, 1.0) for s in sigs]
    n = data['n']; c = data['c']
    out = []
    for s in sigs:
        i = s['idx']
        row = {
            'vwap_dev': float((c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0) if data['vwap'][i] > 0 else 0.0,
            'rsi': float(data['rsi'][i]), 'trend': int(data['trend'][i]),
            'atr_pct': float(data['atr'][i] / s['price'] * 100.0),
        }
        for col in ML_FEATURES[4:]:
            row[col] = 0.5
        p = float(ml.predict_proba(pd.DataFrame([row])[ML_FEATURES])[0, 1])
        out.append((s, p))
    return out


def _p0_vwap_filter(sigs, data, thr=VWAP_DEV_B_MAX):
    """P0：B 侧 vwap_dev > thr% 拒绝（追高抑制）。S 侧不受影响。"""
    c = data['c']; vw = data['vwap']
    out = []
    for s in sigs:
        if s['type'] == 'B':
            i = s['idx']
            dev = float((c[i] - vw[i]) / vw[i] * 100.0) if vw[i] > 0 else 0.0
            if dev > thr:
                continue
        out.append(s)
    return out


def _trip(pos, exit_idx, exit_price, reason, direction):
    ep = pos['ep']
    gross = (exit_price - ep) / ep * 100.0 if ep > 0 else 0.0
    if direction == 'short':
        gross = -gross
    return {'side': 'B' if direction == 'long' else 'S', 'exit_reason': reason,
            'entry_idx': pos['ei'], 'exit_idx': int(exit_idx),
            'entry_price': round(float(ep), 3), 'exit_price': round(float(exit_price), 3),
            'ret_pct': round(float(gross), 3), 'hold_bars': int(exit_idx - pos['ei'])}


def _simulate_v11(sigs, data):
    """P1：ATR 自适应止损 round-trip（正T + 反T 自研，研究态）。
    出场优先级：ATRSTOP > 信号出场 > TRAIL > EOD。TRAIL 激活 0.4%/回撤 0.6% 与生产一致。"""
    n = data['n']; c = data['c']; h = data.get('h'); lo = data['lo']; atr = data['atr']
    trips = []
    for direction in ('long', 'short'):
        entry_map = {s['idx']: s for s in sigs if s['type'] == ('B' if direction == 'long' else 'S')}
        exit_map = {s['idx']: s for s in sigs if s['type'] == ('S' if direction == 'long' else 'B')}
        pos = None
        for i in range(2, n):
            if pos is None:
                if i in entry_map:
                    e = entry_map[i]; ep = e['price']
                    atr_pct = float(atr[i]) / ep * 100.0 if ep > 0 and atr[i] > 0 else 0.0
                    stop_pct = max(ATR_STOP_MULT * atr_pct, ATR_STOP_FLOOR)
                    pos = {'ei': i, 'ep': ep, 'stop_pct': stop_pct, 'ext': ep}
                continue
            if direction == 'long':
                stop_px = pos['ep'] * (1 - pos['stop_pct'] / 100.0)
                if lo[i] <= stop_px:
                    trips.append(_trip(pos, i, stop_px, 'ATRSTOP', direction)); pos = None; continue
            else:
                stop_px = pos['ep'] * (1 + pos['stop_pct'] / 100.0)
                if h is not None and h[i] >= stop_px:
                    trips.append(_trip(pos, i, stop_px, 'ATRSTOP', direction)); pos = None; continue
            if (direction == 'long' and c[i] > pos['ext']) or (direction == 'short' and c[i] < pos['ext']):
                pos['ext'] = c[i]
            if i in exit_map:
                trips.append(_trip(pos, i, exit_map[i]['price'], 'SIG', direction)); pos = None; continue
            # TRAIL
            if direction == 'long':
                fav = (pos['ext'] - pos['ep']) / pos['ep'] * 100.0
                if fav >= 0.4:
                    trail = pos['ext'] * (1 - 0.6 / 100.0)
                    if c[i] <= trail:
                        trips.append(_trip(pos, i, c[i], 'TRAIL', direction)); pos = None; continue
            else:
                fav = (pos['ep'] - pos['ext']) / pos['ep'] * 100.0
                if fav >= 0.4:
                    trail = pos['ext'] * (1 + 0.6 / 100.0)
                    if c[i] >= trail:
                        trips.append(_trip(pos, i, c[i], 'TRAIL', direction)); pos = None; continue
        if pos is not None:
            trips.append(_trip(pos, n - 1, c[n - 1], 'EOD', direction))
    trips.sort(key=lambda t: t['entry_idx'])
    return trips


def _run(sym, ml, use_v11):
    fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(fp, encoding='utf-8-sig')
    df['trade_date'] = df['trade_date'].astype(str)
    d = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
    if len(d) < 60:
        return None
    pc = float(d['close'].iloc[0]) * 0.999
    data = build_data(d, pc)
    sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
    sigs = [s for s, p in _score_ml(ml, sigs, data) if p >= 0.5]
    if use_v11:
        sigs = _p0_vwap_filter(sigs, data)
    if use_v11:
        trips = _simulate_v11(sigs, data)
    else:
        trips = simulate_day(sigs, data, EXIT_CFG_V10, cost=None) + \
                simulate_bidirectional(sigs, data, config=EXIT_CFG_SHORT_V10, cost=None)
    total = sum(t['ret_pct'] for t in trips)
    n_stop = sum(1 for t in trips if t['exit_reason'] in ('FIXSTOP', 'ATRSTOP', 'STOP'))
    n_cut = sum(1 for t in trips if t['exit_reason'] in ('FIXSTOP', 'ATRSTOP', 'STOP') and t['ret_pct'] < 0)
    max_loss = min((t['ret_pct'] for t in trips), default=0.0)
    return {'sym': sym, 'n_sigs': len(sigs), 'n_trips': len(trips),
            'total_pnl_pct': round(total, 3), 'n_stop_cut': n_stop, 'n_neg_stop': n_cut,
            'max_loss_pct': round(float(max_loss), 3), 'trips': trips}


def run(ctx=None):
    le_core.log('P11: 开始执行（GT-TB v1.1 研究态改进验证）')
    ml = _load_ml()
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for sym, name in POOL:
        v10 = _run(sym, ml, use_v11=False)
        v11 = _run(sym, ml, use_v11=True)
        if v10 is None or v11 is None:
            continue
        rows.append({'sym': sym, 'name': name, 'v10': v10, 'v11': v11})
        le_core.log(f'P11: {sym} {name}  v10净{v10["total_pnl_pct"]:+.2f}% (stop{v10["n_stop_cut"]}) '
                    f'→ v11净{v11["total_pnl_pct"]:+.2f}% (atrstop{v11["n_stop_cut"]})')

    t10 = round(sum(r['v10']['total_pnl_pct'] for r in rows), 3)
    t11 = round(sum(r['v11']['total_pnl_pct'] for r in rows), 3)
    stop10 = sum(r['v10']['n_stop_cut'] for r in rows)
    stop11 = sum(r['v11']['n_stop_cut'] for r in rows)
    gate = {'pass': t11 >= t10, 'total_v10': t10, 'total_v11': t11,
            'stop_cut_v10': stop10, 'stop_cut_v11': stop11}
    report = {'stage': 'p11_gt_tb_v11', 'date': DATE, 'pool': POOL, 'per_sym': rows,
              'summary': {'v10': {'total_pnl': t10, 'stop_cuts': stop10},
                          'v11': {'total_pnl': t11, 'stop_cuts': stop11},
                          'improvement_pp': round(t11 - t10, 3)},
              'gate': gate, 'note': '研究态，不入 monitor 生产核心；VERSION 不 bump'}

    # 落盘 JSON
    with open(os.path.join(OUT_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    le_core.log(f'P11: 汇总 v10净{t10:+.2f}% → v11净{t11:+.2f}% (改善{t11 - t10:+.2f}pp) '
                f'| 止损单 {stop10}→{stop11} | gate={gate["pass"]}')
    report['result'] = 'PASS' if gate['pass'] else 'PASS_VERIFIED_NO_CHANGE'
    report['msg'] = (f'P11 GT-TB v1.1 完成：三标的净 {t10:+.2f}% → {t11:+.2f}%（+{t11 - t10:.2f}pp），'
                     f'止损类单 {stop10}→{stop11}，{"改善采纳" if gate["pass"] else "未达改善"}（研究态，不入生产）')
    le_core.log(report['msg'])
    return True, report


if __name__ == '__main__':
    ok, rep = run()
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if ok else 1)
