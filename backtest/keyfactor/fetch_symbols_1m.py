# -*- coding: utf-8 -*-
"""拉取多只 T+0 LOF/ETF 的 1m 数据, 落 F:/keyfactor_data/1m/{code}_1m.csv。
覆盖约最近 21 交易日 (offset=5000 ~ 20.8 日), 与 161129/513310 现有文件窗口一致。

用法: ./venv/Scripts/python.exe backtest/keyfactor/fetch_symbols_1m.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from datasource import tdx_client, _to_mootdx_sym
import pandas as pd

OUT_DIR = 'F:/keyfactor_data/1m'
TARGET = 5000
PAGES = 7
PER = 800
FREQ = 8

SYMBOLS = {
    '518880.SH': '黄金ETF',
    '159985.SZ': '豆粕ETF',
    '513040.SH': '跨境ETF',
}


def download_one(sym):
    code, market = _to_mootdx_sym(sym)
    cli = tdx_client()
    frames = []
    for p in range(PAGES):
        start = p * PER
        try:
            df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER, market=market)
        except Exception:
            try:
                cli = tdx_client()
                df = cli.bars(symbol=code, frequency=FREQ, start=start, offset=PER, market=market)
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
    big['datetime'] = pd.to_datetime(big['datetime'], errors='coerce')
    big = big.dropna(subset=['datetime'])
    big = big.drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    big = big.tail(TARGET).reset_index(drop=True)
    out = pd.DataFrame({
        'symbol': sym,
        'name': SYMBOLS[sym],
        'trade_date': big['datetime'].dt.strftime('%Y-%m-%d'),
        'trade_time': big['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S'),
        'open': big['open'].astype(float),
        'high': big['high'].astype(float),
        'low': big['low'].astype(float),
        'close': big['close'].astype(float),
        'volume': (big['vol'] if 'vol' in big.columns else big.get('volume', 0)).astype(float),
    })
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for sym in SYMBOLS:
        print(f'拉取 {sym} ({SYMBOLS[sym]}) ...')
        try:
            out = download_one(sym)
        except Exception as e:
            print(f'  ❌ {sym} 异常: {e}')
            out = None
        if out is None or len(out) == 0:
            print(f'  ⚠️ {sym} 无数据 (代码可能无效; 若为 513040 请改 513100/513500 等 513xxx 跨境ETF)')
            continue
        fpath = os.path.join(OUT_DIR, f'{sym}_1m.csv')
        out.to_csv(fpath, index=False, encoding='utf-8-sig')
        dates = sorted(out['trade_date'].unique().tolist())
        print(f'  ✅ {sym} {len(out)}根 -> {fpath}')
        print(f'     覆盖 {len(dates)} 日: {dates[0]} ... {dates[-1]}')


if __name__ == '__main__':
    main()
