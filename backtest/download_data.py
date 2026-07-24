#!/usr/bin/env python3
"""
tickflow 分钟历史数据落地 — 一次性下载存本地CSV
之后回测离线读本地, 不再调tickflow(省钱)
实盘监控继续用mootdx(免费)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasource import MootdxDataSource as TickFlow

tf = TickFlow(api_key='tk_60a2170efd294c82b2245324a268b2a8')

def _load_targets():
    """动态加载标的：data/watchlist.json（单一真相源）。2026-07-21 移除硬编码。"""
    import json as _j
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'watchlist.json')
    try:
        if os.path.exists(_p):
            with open(_p, encoding='utf-8') as _f:
                _wl = _j.load(_f)
            if isinstance(_wl, dict):
                return {c: (v if isinstance(v, str) else v.get('name', c)) for c, v in _wl.items()}
    except Exception:
        pass
    return {}

TARGETS = _load_targets()
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data')
os.makedirs(DATA_DIR, exist_ok=True)

print(f"=== tickflow分钟数据落地 → {DATA_DIR} ===")
total = 0
for sym, name in TARGETS.items():
    try:
        df = tf.klines.get(sym, period='1m', count=5000, as_dataframe=True)
        if df is not None and len(df) > 0:
            df = df.sort_values('trade_time').reset_index(drop=True)
            fpath = os.path.join(DATA_DIR, f'{sym}_1m.csv')
            df.to_csv(fpath, index=False)
            total += len(df)
            print(f"  {name}({sym}): {len(df)}根 → {os.path.basename(fpath)}")
            print(f"    最早:{df['trade_time'].iloc[0]} 最晚:{df['trade_time'].iloc[-1]}")
        else:
            print(f"  {name}({sym}): 无数据")
    except Exception as e:
        print(f"  {name}({sym}): 异常 {e}")

print(f"\n=== 落地完成: {total}根分钟数据, {len(TARGETS)}标的 ===")
print("之后回测读本地CSV, 不再调tickflow。实盘用mootdx免费。")
