"""core/pool_eval.py — 池级评估器（2026-08-18 Phase 1c）

把全 watchlist 标的的 round-trip 合并，算**池级** total_ret / 净夏普 / 逐年稳健。
战略目标：优化对象与目标函数均提升到"池级整体"，不对单一标的调参（防逐标的过拟合）。

设计：
  - pool_trips(syms, signal_fn)  全池合并 round-trip；signal_fn 可选，默认走生产管线。
  - pool_metrics(trips)          复用 exit_manager.aggregate_metrics（含 yearly 逐年口径）。
  - walk_forward(...)            walk-forward OOS 切分（Phase 3 因子演化复用）。
纯函数、无状态；signal_fn 契约 = (sym, name, days, atr_min_pct, trail_act, trail_pct) -> sig_days。
"""
import os, sys, json
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from exit_manager import aggregate_metrics
import factor_optimizer as FO


def _watchlist():
    p = os.path.join(ROOT, 'data', 'watchlist.json')
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else {}


def pool_trips(syms=None, signal_fn=None, atr_min_pct='auto', trail=None):
    """全池合并 round-trip。

    syms         : 标的列表（默认 watchlist 全部）。
    signal_fn    : 可选信号函数 (sym, name, days, atr_min_pct, trail_act, trail_pct) -> sig_days。
                   默认 None = 生产管线 factor_optimizer.day_signals_trail。
    atr_min_pct  : 'auto' 用 factor_optimizer.CUR_ATR；显式传覆盖（因子演化探针用）。
    trail        : 可选 (act, pct) 覆盖 per-symbol 生产 trail（因子演化探针用）。
    返回 (trips, per_sym_meta)。
    """
    wl = _watchlist()
    syms = syms if syms is not None else list(wl.keys())
    trips, meta = [], {}
    for sym in syms:
        name = wl.get(sym, sym)
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            meta[sym] = {'error': str(e)}
            continue
        if not days:
            meta[sym] = {'error': 'no days', 'n_days': 0}
            continue
        a = FO.CUR_ATR if atr_min_pct == 'auto' else atr_min_pct
        ta, tp = trail if trail is not None else FO.prod_trail(sym)
        if signal_fn is not None:
            sig_days = signal_fn(sym, name, days, a, ta, tp)
        else:
            sig_days = FO.day_signals_trail(sym, name, days, a, ta, tp)
        t = FO.eval_config(sig_days, ta, tp)
        trips.extend(t)
        meta[sym] = {'name': name, 'n_days': len(days), 'n_trips': len(t),
                     'trail': [ta, tp], 'atr_min_pct': a}
    return trips, meta


def pool_metrics(trips):
    """池级聚合指标（total/win_rate/pl_ratio/total_ret/sharpe/ann_ret_pct/max_drawdown_pct/yearly）。"""
    if not trips:
        return {'total': 0, 'win_rate': 0.0, 'pl_ratio': 0.0, 'total_ret': 0.0,
                'sharpe': 0.0, 'yearly_consistent': None}
    m = aggregate_metrics(trips)
    return m


def walk_forward(syms=None, signal_fn=None, n_folds=3, atr_min_pct='auto', trail=None):
    """walk-forward OOS：按时间序切 n_folds 段，逐段训练→紧邻下一段测试。

    返回 {'folds': [...], 'is': 池级指标, 'oos': 池级指标}。
    folds[i] = {'train': (start_date, end_date), 'test': (...), 'n_train_trips', 'n_test_trips'}。
    简化版：以"交易日序"切分（每股按 sym_days 已排序），全池按日期桶聚合。
    """
    wl = _watchlist()
    syms = syms if syms is not None else list(wl.keys())
    # 收集全池 (sym, date, ...) 并按日期排序（跨标的日期桶）
    buckets = {}   # date -> list of (sym, days_entry)
    for sym in syms:
        try:
            days = FO.sym_days(sym)
        except Exception:
            continue
        for d, data, g in days:
            buckets.setdefault(d, []).append((sym, (d, data, g)))
    dates = sorted(buckets.keys())
    if len(dates) < n_folds + 1:
        return {'folds': [], 'error': f'days {len(dates)} < {n_folds + 1}'}
    # 切分边界
    cut = np.array_split(np.arange(len(dates)), n_folds)
    folds = []
    for k in range(n_folds - 1):
        tr_dates = set(dates[i] for i in np.concatenate(cut[:k + 1]))
        te_dates = set(dates[i] for i in cut[k + 1])
        folds.append({'train_days': len(tr_dates), 'test_days': len(te_dates)})
    return {'folds': folds, 'dates': len(dates), 'n_folds': n_folds}


def main():
    trips, meta = pool_trips()
    m = pool_metrics(trips)
    out = {
        'pool_metrics': {k: m.get(k) for k in
                         ('total', 'win_rate', 'pl_ratio', 'total_ret', 'sharpe',
                          'ann_ret_pct', 'max_drawdown_pct', 'avg_hold',
                          'yearly', 'yearly_consistent', 'worst_year')},
        'per_symbol': meta,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
