#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""fdisk_backfill.py — F盘 1m 历史回填器（2026-08-05 · 为 300308 等新进 watchlist 标的补历史）

mootdx bars(frequency=8 1m, start 分页, offset=800) 逐页向前拉取 → 转 tickflow CSV 列格式
（symbol/name/timestamp/ms/trade_date/trade_time/OHLC/volume/amount）→ 写 F:\keyfactor_data\1m\<sym>_1m.csv。
已存在文件 → 幂等合并（按 timestamp 去重，保留全部历史）。
注意：mootdx 服务器 1m 历史上限约 98 个交易日（300308 实测 2026-03-13 起）。

CLI：python scripts/fdisk_backfill.py 300308.SZ --days 150
"""
import os, sys, argparse, datetime, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)

import pandas as pd
from datasource import MootdxDataSource, _to_mootdx_sym

F_DATA = r'F:\keyfactor_data\1m'


def backfill(sym, days=150):
    ds = MootdxDataSource()
    code, _ = _to_mootdx_sym(sym)
    target_bars = days * 240
    pages = []
    start = 0
    while start < target_bars:
        for attempt in range(3):
            try:
                df = ds.client.bars(symbol=code, frequency=8, start=start, offset=800)
                break
            except Exception as e:
                print(f'  retry {attempt} @start={start}: {e}')
                ds.reconnect()
                time.sleep(2)
        else:
            print(f'  ❌ start={start} 三试失败，提前收尾')
            break
        if df is None or len(df) == 0:
            break
        df = df.reset_index(drop=True)   # bars() 返回 datetime 既为索引又为列，先拍平防 concat 歧义
        pages.append(df)
        print(f'  page start={start}: {len(df)} 根 [{df["datetime"].iloc[0]} ~ {df["datetime"].iloc[-1]}]', flush=True)
        if len(df) < 800:
            break
        start += 800
        time.sleep(0.3)
    if not pages:
        print('❌ 无数据')
        return
    full = pd.concat(pages).drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    dt = pd.to_datetime(full['datetime'])
    out = pd.DataFrame({
        'symbol': sym, 'name': sym,
        'timestamp': (dt.astype('int64') // 10 ** 6),
        'trade_date': dt.dt.strftime('%Y-%m-%d'),
        'trade_time': dt.dt.strftime('%Y-%m-%d %H:%M:%S'),
        'open': full['open'], 'high': full['high'], 'low': full['low'], 'close': full['close'],
        'volume': full['volume'], 'amount': full.get('amount', 0),
    })
    path = os.path.join(F_DATA, f'{sym}_1m.csv')
    if os.path.exists(path):
        old = pd.read_csv(path)
        out = pd.concat([old, out]).drop_duplicates(subset=['timestamp']).sort_values('timestamp')
        print(f'合并已有文件 {len(old)} 行 → {len(out)} 行')
    out.to_csv(path, index=False, encoding='utf-8-sig')
    print(f'[ok] {path}：{len(out)} 行，{out["trade_date"].nunique()} 个交易日 '
          f'({out["trade_date"].iloc[0]} ~ {out["trade_date"].iloc[-1]})')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('sym')
    ap.add_argument('--days', type=int, default=150)
    a = ap.parse_args()
    backfill(a.sym, a.days)
