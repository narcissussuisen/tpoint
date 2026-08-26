# -*- coding: utf-8 -*-
"""core/top_bottom_features.py — 顶底捕捉标签与特征融合（P9）

目标：用 tick 相对特征（P8 产出）提升 B/S 信号对顶底的捕捉率（DET EHR@0.5% 口径）。

标签（DET 框架，对齐 v5_systematic_tune_v2）：
  - B 信号（正T）：信号后 horizon 分钟内 close 是否触及 entry + 0.5% → label=1（有利极端）
  - S 信号（反T）：信号后 horizon 分钟内 close 是否触及 entry - 0.5% → label=1
  - 触及 = 未来 N 根 close 的最大值/最小值超过阈值

特征：
  - 1m 基线：signal 处的 vwap_dev / rsi / trend / atr_pct（来自 data['c'/'atr'/'trend'] 等）
  - tick 增强（P8）：该信号分钟的 trade_count / buy_ratio / large_tape_count / vwap_dev /
    hilo_range_pct / direction_flow / same_price_tape（来自 tick_features）

数据对齐：tick 特征按 (date, HH:MM) 与 1m bar 对齐；仅对 tick 覆盖标的（161129/513310 等）启用。
"""
import os

import numpy as np
import pandas as pd

from tick_loader import list_available as tick_avail
from tick_features import build_minute_features, FEATURE_DIR
from tick_loader import load_tick_day

EHR_THRESH = 0.005  # 0.5%


def extremum_label(c_close, entry_price, sig_type, horizon=15):
    """信号后 horizon 根 close 是否触及有利极端。c_close: entry_idx+1 .. entry_idx+horizon。
    B（正T）→ 上触 +0.5%；S（反T）→ 下触 -0.5%。返回 0/1。"""
    if entry_price <= 0 or len(c_close) == 0:
        return 0
    if sig_type == 'B':
        return int(bool(np.max(c_close) >= entry_price * (1 + EHR_THRESH)))
    return int(bool(np.min(c_close) <= entry_price * (1 - EHR_THRESH)))


def build_signal_features(sym, df_1m, data, sigs, tick_features=None, horizon=15):
    """为某日信号构建 特征行 + 顶底标签。tick_features: DataFrame(index=HH:MM) 或 None。
    返回 DataFrame：sym/date/sig_type/entry_price/label + 特征列。"""
    rows = []
    n = data['n']
    c = data['c']
    hhmm_map = {}
    if tick_features is not None and len(tick_features):
        hhmm_map = {str(i): tick_features.loc[str(i)] for i in tick_features.index}
    for s in sigs:
        i = s['idx']
        if i + 1 >= n:
            continue
        future = c[i + 1:min(i + 1 + horizon, n)]
        label = extremum_label(future, s['price'], s['type'], horizon)
        row = {'sym': sym, 'sig_type': s['type'], 'idx': i,
               'entry_price': s['price'], 'label': label,
               # 1m 基线特征
               'vwap_dev': float((c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0)
                           if 'vwap' in data and data['vwap'][i] > 0 else np.nan,
               'rsi': float(data['rsi'][i]) if 'rsi' in data else np.nan,
               'trend': int(data['trend'][i]) if 'trend' in data else 0,
               'atr_pct': float(data['atr'][i] / s['price'] * 100.0) if 'atr' in data and s['price'] > 0 else np.nan,
               }
        # tick 增强特征（若有该分钟）
        if hhmm_map:
            ts = df_1m['trade_time'].astype(str).iloc[i][11:16] if 'trade_time' in df_1m.columns else ''
            tf = hhmm_map.get(ts)
            if tf is not None:
                for col in ('trade_count', 'buy_ratio', 'large_tape_count', 'vwap_dev',
                            'hilo_range_pct', 'direction_flow', 'same_price_tape'):
                    row[f'tick_{col}'] = float(tf[col]) if col in tf.index else np.nan
            else:
                for col in ('trade_count', 'buy_ratio', 'large_tape_count', 'vwap_dev',
                            'hilo_range_pct', 'direction_flow', 'same_price_tape'):
                    row[f'tick_{col}'] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def build_dataset(sym, df_1m, data, sigs, horizon=15, use_tick=True):
    """单标的单日 → 特征数据集。use_tick 时尝试对齐 tick 特征（tick 日期存在才启用）。"""
    date_str = str(df_1m['trade_date'].iloc[0]).replace('-', '')
    tick_feat = None
    if use_tick:
        avail = {(s, d) for s, d in tick_avail(sym=sym)}
        if (sym, date_str) in avail:
            tk = load_tick_day(sym, date_str)
            if tk is not None and len(tk) > 50:
                tick_feat = build_minute_features(tk)
    return build_signal_features(sym, df_1m, data, sigs, tick_feat, horizon)
