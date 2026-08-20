"""core/evolution.py — 因子演化引擎 MVP（2026-08-18 Phase 3）

战略转向：优化对象从「per-symbol 参数(trail/atr)」升级为「因子/门控规则」，目标函数为
**池级** total_ret + 净夏普 + 逐年稳健（不对单一标的调参）。

机制：
  1. 候选门控 = (factor, side, op, thr)：对某侧信号施加因子阈值过滤。
  2. 池级评估：对每个候选门控，在全池 round-trip 上算 基线 vs 加门控 的池级指标。
  3. 时间序 IS/OOS 切分（walk-forward 简化版）：门控改善必须 OOS 也成立才晋升。
  4. 晋升/淘汰：OOS 池级 total_ret 改善 且 wr 不降 → PROMOTE；否则 DEMOTE。

复用 core/pool_eval.py 与 core/factor_registry.py；纯函数、无状态。
"""
import os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import factor_optimizer as FO
from factor_registry import FACTORS
from exit_manager import aggregate_metrics

# 候选门控：当前生产未启用的因子过滤（mpr/atr 已启用，此处放"新候选"）
CANDIDATES = [
    {'name': 'vol_ratio_b_low',   'factor': 'vol_ratio',  'side': 'B', 'op': '<=', 'thr': 1.2,
     'note': '量能确认 B（缩量回调，抛压衰竭；生产 vol_confirm 已关）'},
    {'name': 'rsi_b_oversold',    'factor': 'rsi',        'side': 'B', 'op': '<',  'thr': 45.0,
     'note': 'B 只在 RSI 偏弱区（抄底不追高）'},
    {'name': 'macd_dif_b_up',     'factor': 'macd_dif',   'side': 'B', 'op': '>',  'thr': 0.0,
     'note': 'B 只在 1m MACD DIF>0（短周期已转强）'},
    {'name': 'trend_ema_b_up',    'factor': 'trend_ema',  'side': 'B', 'op': '>',  'thr': 0.0,
     'note': 'B 只在 EMA20>EMA60 上升趋势'},
]

# 明确反例（用于红测：单标的过拟合门控，池级应被淘汰）
OVERFIT_CANDIDATES = [
    {'name': 'gravity_dev_b_deep', 'factor': 'gravity_dev', 'side': 'B', 'op': '<', 'thr': -3.5,
     'note': 'B 只在深度超跌(dev<-3.5%)：仅少数标的的极端行情触发，池级泛化差'},
]


def _op_ok(val, op, thr):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    if op == '<':
        return f < thr
    if op == '<=':
        return f <= thr
    if op == '>':
        return f > thr
    if op == '>=':
        return f >= thr
    return False


def apply_gate(sig_days, gate):
    """对 (d, data, sigs) 列表按 gate 过滤某侧信号，返回新的 sig_days。"""
    fn = FACTORS[gate['factor']]
    out = []
    for d, data, sigs in sig_days:
        o = data['o']; h = data['h']; lo = data['lo']; c = data['c']; v = data.get('v')
        fac = fn(o, h, lo, c, v)
        kept = []
        for s in sigs:
            if s['type'] != gate['side']:
                kept.append(s)
                continue
            i = s.get('idx', -1)
            if i is None or i < 0 or i >= len(fac):
                kept.append(s)   # 无法取因子值 → 不误杀（fail-open）
                continue
            if _op_ok(fac[i], gate['op'], gate['thr']):
                kept.append(s)
        out.append((d, data, kept))
    return out


def _pool_trips_for(syms, gate=None, days_list=None):
    """按 (syms, gate) 生成全池 round-trip（复用生产管线 + 可选门控过滤）。"""
    import json as _json
    _wl = _json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
    syms = syms or list(_wl.keys())
    trips = []
    for sym in syms:
        name = _wl.get(sym, sym)
        days = days_list.get(sym) if days_list else None
        if days is None:
            try:
                days = FO.sym_days(sym)
            except Exception:
                continue
        ta, tp = FO.prod_trail(sym)
        sig_days = FO.day_signals_trail(sym, name, days, FO.CUR_ATR, ta, tp)
        if gate is not None:
            sig_days = apply_gate(sig_days, gate)
        trips.extend(FO.eval_config(sig_days, ta, tp))
    return trips


def _split_days(days, frac=0.7):
    """时间序切分（days 已按日期升序）。"""
    n = len(days)
    k = int(n * frac)
    return days[:k], days[k:]


def evaluate_gate(gate, syms=None, is_frac=0.7):
    """池级 IS/OOS 评估一个门控。返回 verdict dict。"""
    import json as _json
    wl = _json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
    syms = syms or list(wl.keys())

    # 每个标的：加载 days 并切 IS/OOS
    is_days, oos_days = {}, {}
    for sym in syms:
        try:
            days = FO.sym_days(sym)
        except Exception:
            continue
        is_d, oos_d = _split_days(days, is_frac)
        is_days[sym] = is_d; oos_days[sym] = oos_d

    base_is = _pool_trips_for(syms, gate=None, days_list=is_days)
    gate_is = _pool_trips_for(syms, gate=gate, days_list=is_days)
    base_oos = _pool_trips_for(syms, gate=None, days_list=oos_days)
    gate_oos = _pool_trips_for(syms, gate=gate, days_list=oos_days)

    def m(trips):
        return aggregate_metrics(trips) if trips else {'total': 0, 'win_rate': 0.0, 'total_ret': 0.0, 'sharpe': 0.0}

    bi, gi = m(base_is), m(gate_is)
    bo, go = m(base_oos), m(gate_oos)

    d_ret_oos = round(go.get('total_ret', 0.0) - bo.get('total_ret', 0.0), 3)
    d_wr_oos = round(go.get('win_rate', 0.0) - bo.get('win_rate', 0.0), 2)
    n_oos = go.get('total', 0)

    # 晋升判据：OOS 池级 total_ret 改善 且 wr 不降（>= -0.5pp）
    verdict = ('PROMOTE' if (d_ret_oos > 0 and d_wr_oos >= -0.5 and n_oos >= 10)
               else 'DEMOTE')
    return {
        'gate': gate['name'], 'note': gate.get('note', ''),
        'IS': {'base_ret': round(bi.get('total_ret', 0.0), 3),
               'gate_ret': round(gi.get('total_ret', 0.0), 3),
               'n_base': bi.get('total', 0), 'n_gate': gi.get('total', 0)},
        'OOS': {'base_ret': round(bo.get('total_ret', 0.0), 3),
                'gate_ret': round(go.get('total_ret', 0.0), 3),
                'base_wr': round(bo.get('win_rate', 0.0), 1),
                'gate_wr': round(go.get('win_rate', 0.0), 1),
                'n_gate': n_oos},
        'd_ret_oos_pp': d_ret_oos, 'd_wr_oos_pp': d_wr_oos,
        'verdict': verdict,
    }


def evolve(syms=None, candidates=None, is_frac=0.7):
    """跑全部候选门控，返回 [(verdict dict, ...)] 与汇总。"""
    cands = CANDIDATES if candidates is None else candidates
    results = [evaluate_gate(g, syms, is_frac) for g in cands]
    n_promote = sum(1 for r in results if r['verdict'] == 'PROMOTE')
    return results, {'n_candidates': len(results), 'n_promote': n_promote,
                     'n_tested_total': len(results),  # 多重比较提示用
                     'bonferroni_hint': '每次测试独立检验，多次搜索需对 p 做多重比较校正（deflated-Sharpe 口径）'}


if __name__ == '__main__':
    results, summary = evolve()
    print(__import__('json').dumps({'summary': summary, 'results': results},
                                   ensure_ascii=False, indent=2, default=str))
