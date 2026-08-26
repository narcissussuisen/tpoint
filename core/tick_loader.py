# -*- coding: utf-8 -*-
"""core/tick_loader.py — tick_cache 加载器（P8）

读取 data/tick_cache/<sym>_<YYYYMMDD>.csv（逐笔成交快照，近似 level2 数据）。
CSV 格式（无 header 或带 header，统一处理）：
    time, price, vol, buyorsell, volume, date
  - time     : HH:MM（仅分钟粒度，无秒 → 无法做 3 秒聚合，降级分钟级 tick 特征）
  - price    : 成交价
  - vol      : 本笔量（与 volume 列通常相同；部分文件 vol 为手数）
  - buyorsell: 0=主动买 / 1=主动卖
  - volume   : 本笔量（冗余列）
  - date     : YYYYMMDD

目标：统一输出规范 DataFrame（time/price/vol/direction/date），供 tick_aggregator 聚合。
"""
import os

import numpy as np
import pandas as pd

TICK_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'tick_cache')


def tick_path(sym, date_yyyymmdd):
    return os.path.join(TICK_CACHE_DIR, f'{sym}_{date_yyyymmdd}.csv')


def load_tick_day(sym, date_yyyymmdd, tick_dir=None):
    """读单个标的单日 tick。返回规范化 DataFrame 或 None。

    列：time(str HH:MM) / price(float) / vol(float) / direction(int 0买1卖) / date(int)
    """
    d = tick_dir or TICK_CACHE_DIR
    p = os.path.join(d, f'{sym}_{date_yyyymmdd}.csv')
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, encoding='utf-8-sig')
    if df.empty:
        return None
    # 容错列名（有无 header 两种变体）
    col = {c: c for c in df.columns}
    price = df[col.get('price', col.get('现价', df.columns[1]))]
    t = df[col.get('time', df.columns[0])]
    date_c = df[col.get('date', df.columns[-1])]
    if 'buyorsell' in col:
        bs = df['buyorsell']
    elif 'direction' in col:
        bs = df['direction']
    else:
        bs = pd.Series(0, index=df.index)
    out = pd.DataFrame({
        'time': t.astype(str).str.strip(),
        'price': pd.to_numeric(price, errors='coerce'),
        'vol': pd.to_numeric(df[col.get('vol', 'vol')], errors='coerce') if 'vol' in col else pd.to_numeric(df[col.get('volume', 'volume')], errors='coerce'),
        'direction': pd.to_numeric(bs, errors='coerce').fillna(0).astype(int),
        'date': pd.to_numeric(date_c, errors='coerce'),
    }).dropna(subset=['price', 'vol'])
    return out.reset_index(drop=True)


def list_available(sym=None, tick_dir=None):
    """列出 tick_cache 可用 (sym, date) 对；sym 可过滤。返回 [(sym, YYYYMMDD), ...] 排序。"""
    d = tick_dir or TICK_CACHE_DIR
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith('.csv'):
            continue
        base = fn[:-4]
        if '_' not in base:
            continue
        s, date_s = base.rsplit('_', 1)
        if sym is not None and s != sym:
            continue
        if date_s.isdigit() and len(date_s) == 8:
            out.append((s, date_s))
    return out


if __name__ == '__main__':
    import sys
    av = list_available()
    print(f'tick_cache: {len(av)} (sym, date) pairs, {len(set(s for s, _ in av))} symbols')
    if av:
        s0, d0 = av[0]
        df = load_tick_day(s0, d0)
        print(f'sample {s0}_{d0}: {len(df)} trades')
        print(df.head(5).to_string())
        print('direction value counts:', df['direction'].value_counts().to_dict())
