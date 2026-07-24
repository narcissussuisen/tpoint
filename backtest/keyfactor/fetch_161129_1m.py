#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只拉 161129 最近 1m 数据, 落地到 KEYFACTOR_1M_DIR/161129.SZ_1m.csv。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from _paths import KEYFACTOR_1M_DIR
from datasource import tdx_client
import pandas as pd

SYM = '161129.SZ'
CODE = '161129'
NAME = '原油LOF易方达'
TARGET = 5000
PAGES = 7
PER = 800
FREQ = 8

def download_one(cli, code, sym, name, target=TARGET):
    frames = []
    for p in range(PAGES):
        start = p * PER
        try:
            df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER)
        except Exception:
            try:
                cli = tdx_client()
                df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER)
            except Exception:
                df = None
        if df is None or len(df) == 0:
            break
        frames.append(df)
        if len(df) < PER:
            break
    if not frames:
        return None
    big = pd.concat(frames, ignore_index=True)
    if 'datetime' not in big.columns:
        return None
    big['datetime'] = pd.to_datetime(big['datetime'])
    big = big.drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    big = big.tail(target).reset_index(drop=True)
    out = pd.DataFrame({
        'symbol': sym,
        'name': name,
        'timestamp': (big['datetime'].astype('int64') // 10**6).astype('int64'),
        'trade_date': big['datetime'].dt.strftime('%Y-%m-%d'),
        'trade_time': big['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S'),
        'open': big['open'].astype(float),
        'high': big['high'].astype(float),
        'low': big['low'].astype(float),
        'close': big['close'].astype(float),
        'volume': big['vol'].astype(float) if 'vol' in big.columns else big.get('volume', 0).astype(float),
        'amount': big['amount'].astype(float) if 'amount' in big.columns else 0.0,
    })
    return out

def main():
    cli = tdx_client()
    out = download_one(cli, CODE, SYM, NAME)
    if out is None or len(out) == 0:
        print("⚠️ 无数据")
        return
    os.makedirs(KEYFACTOR_1M_DIR, exist_ok=True)
    fpath = os.path.join(KEYFACTOR_1M_DIR, f"{SYM}_1m.csv")
    out.to_csv(fpath, index=False, encoding='utf-8-sig')
    dates = out['trade_date'].unique().tolist()
    print(f"✅ {SYM} {len(out)}根 -> {fpath}")
    print("覆盖日期:", dates)

if __name__ == '__main__':
    main()
