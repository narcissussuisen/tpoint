# -*- coding: utf-8 -*-
"""core/tick_features.py — 分钟级 tick 衍生特征（P8）

⚠️ 已知数据约束（2026-08-26 实测）：tick_cache 价格与 F 盘 1m **相差 ~10 倍**（复权口径不一致，
如 161129.SZ tick=20.28 vs F-1m=2.023），且时间戳仅 HH:MM。因此：
  - **绝对价格特征不可用**（不能与 1m bar 直接对齐）
  - **相对特征可用**（tick 内部比例/密度/失衡，不依赖绝对价格）
本模块输出全部为**相对/比率/计数特征**，可与 1m bar 的 OHLCV 形状（涨跌方向、量能结构）做形态级交叉验证。

特征（每分钟）：
  trade_count      : 成交笔数（密度）
  buy_ratio        : 主动买量占比（买卖失衡）
  large_tape_count : 大单笔数（≥ 全日 95% 分位）
  max_tape_share   : 最大单笔量占分钟量比例（集中度）
  vwap_dev         : 分钟 close 相对分钟 vwap 偏离率（相对口径）
  hilo_range_pct   : (h-l)/c（分钟振幅）
  same_price_tape  : 同价连续大单次数（iceberg 代理）
  direction_flow   : buy_vol - sell_vol（净主动流向，除以 vol_sum 归一）
"""
import os

import numpy as np
import pandas as pd

from tick_loader import load_tick_day, list_available
from tick_aggregator import aggregate_minute

FEATURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'data', 'tick_features')


def build_minute_features(df):
    """由 tick DataFrame 构建分钟特征 DataFrame（索引 time HH:MM）。"""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    agg = aggregate_minute(df)
    if agg.empty:
        return pd.DataFrame()
    f = pd.DataFrame(index=agg.index)
    f['trade_count'] = agg['trade_count']
    f['buy_ratio'] = agg['buy_ratio'].fillna(0.5)
    f['large_tape_count'] = agg['large_tape_count']
    f['max_tape_share'] = (
        df.assign(pv=df['vol']).groupby('time')['vol']
          .apply(lambda v: float(v.max()) / max(float(v.sum()), 1e-9)))
    f['vwap_dev'] = (agg['c'] - agg['vwap']) / agg['vwap'].replace(0, np.nan) * 100.0
    f['hilo_range_pct'] = (agg['h'] - agg['l']) / agg['c'].replace(0, np.nan) * 100.0
    f['direction_flow'] = (agg['buy_vol'] - agg['sell_vol']) / agg['vol_sum'].replace(0, np.nan)
    # iceberg 代理：同价连续大单次数（同一分钟 price 相同且 vol ≥ 95% 分位的连续段）
    thr = float(np.percentile(df['vol'].values, 95))
    big = (df['vol'] >= thr).astype(int)
    f['same_price_tape'] = df.groupby('time').apply(
        lambda x: _max_run((x['vol'] >= thr).values))
    return f


def _max_run(arr):
    best = cur = 0
    for v in arr:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def build_symbol_features(sym, max_days=None, out_dir=None):
    """构建某标的全部可用日的分钟特征，合并输出 DataFrame（含 date/time 列）。"""
    avail = [d for s, d in list_available(sym=sym) if s == sym]
    if max_days:
        avail = avail[:max_days]
    frames = []
    for d in avail:
        df = load_tick_day(sym, d)
        if df is None:
            continue
        f = build_minute_features(df)
        if f.empty:
            continue
        f = f.reset_index().rename(columns={'time': 'minute'})
        f.insert(0, 'date', int(d))
        f.insert(0, 'sym', sym)
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def save_symbol_features(sym, out_dir=None):
    """构建并落盘特征 parquet 到 data/tick_features/<sym>_features.parquet。"""
    od = out_dir or FEATURE_DIR
    os.makedirs(od, exist_ok=True)
    feats = build_symbol_features(sym)
    if feats.empty:
        return None, 0
    p = os.path.join(od, f'{sym}_features.parquet')
    feats.to_parquet(p, index=False)
    return p, len(feats)


def save_all(max_days=None, out_dir=None):
    """全部可用标的特征落盘。返回 [(sym, path, n_rows)]。"""
    syms = sorted({s for s, _ in list_available()})
    out = []
    for sym in syms:
        p, n = save_symbol_features(sym, out_dir=out_dir)
        if p:
            out.append((sym, p, n))
            print(f'{sym}: {n} rows → {p}')
    return out


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--all':
        save_all()
    else:
        sym = sys.argv[1] if len(sys.argv) > 1 else '161129.SZ'
        p, n = save_symbol_features(sym)
        print(f'{sym}: {n} rows → {p}')
