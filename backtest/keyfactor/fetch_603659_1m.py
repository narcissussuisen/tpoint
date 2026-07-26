# -*- coding: utf-8 -*-
"""临时拉取 603659.SH 最近 5000 根 1m 数据，覆盖 F:/keyfactor_data/1m/603659.SH_1m.csv。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from datasource import tdx_client
import pandas as pd

SYM = '603659.SH'
CODE = '603659'
NAME = '璞泰来'
OUT = r'F:/keyfactor_data/1m/603659.SH_1m.csv'
TARGET = 5000
PAGES = 7
PER = 800
FREQ = 8

frames = []
cli = tdx_client()
for p in range(PAGES):
    start = p * PER
    try:
        df = cli.bars(symbol=CODE, frequency=FREQ, start=start, offset=PER)
    except Exception:
        try:
            cli = tdx_client()
            df = cli.bars(symbol=CODE, frequency=FREQ, start=start, offset=PER)
        except Exception:
            df = None
    if df is None or len(df) == 0:
        break
    frames.append(df)
    if len(df) < PER:
        break

if not frames:
    print('无数据')
    sys.exit(1)

big = pd.concat(frames, ignore_index=True)
big['datetime'] = pd.to_datetime(big['datetime'])
big = big.drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
big = big.tail(TARGET).reset_index(drop=True)

out = pd.DataFrame({
    'symbol': SYM,
    'name': NAME,
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
out.to_csv(OUT, index=False, encoding='utf-8-sig')
print(f'写入 {OUT} 共 {len(out)} 行, 日期范围 {out["trade_date"].iloc[0]} ~ {out["trade_date"].iloc[-1]}')
