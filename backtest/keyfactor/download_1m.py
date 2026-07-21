#!/usr/bin/env python3
"""
Phase 1 — 分层抽样清单的 1 个月(5000 根)1 分钟历史数据落地。
数据源: mootdx (datasource.tdx_client), 等价于 tickflow。
落地: KEYFACTOR_DATA_DIR（默认 F:\workbuddy\keyfactor_data）/1m/{sym}_1m.csv  (与 seed schema 完全一致)
schema: symbol,name,timestamp,trade_date,trade_time,open,high,low,close,volume,amount

分页策略(对 offset 语义鲁棒): 多次 bars(frequency=8) 取页, 合并后按
datetime 排序去重, 取最近 target 根。若某页为空/异常则停止。
支持断点续传: 已存在且行数>=target 的文件跳过。
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

from datasource import tdx_client
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
ONED = os.path.join(DATA, '1m')
MANIFEST = os.path.join(DATA, 'sample_manifest.csv')
os.makedirs(ONED, exist_ok=True)

TARGET = 5000        # 1 个月 ≈ 22交易日 × 240分钟 ≈ 5280, 取 5000
PAGES = 7            # 7 × 800 = 5600 >= 5000, 取尾 5000 (够用, 省时)
PER = 800            # mootdx 单页上限
FREQ = 8             # 1 分钟

def download_one(cli, code, sym, name, target=TARGET):
    # mootdx StdQuotes.bars(symbol, frequency, start, offset=800):
    #   start = 分页位置(从最新向前数), offset = 单页条数(上限800)
    #   -> 必须用 start 翻页, offset 恒传 PER(=800, 被钳制在800)
    #   start=0->[0:800], start=800->[800:1600], ... 各页不重叠
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0, help='仅下载前 N 只 (调试)')
    parser.add_argument('--force', action='store_true', help='覆盖已存在文件')
    args = parser.parse_args()

    if not os.path.exists(MANIFEST):
        print(f"⚠️ 未找到 {MANIFEST}, 请先跑 build_sample.py")
        return
    man = pd.read_csv(MANIFEST, dtype={'code': str})
    if args.limit:
        man = man.head(args.limit)

    cli = tdx_client()
    ok = skip = fail = 0
    for _, r in man.iterrows():
        sym = r['sym']; code = r['code']; name = str(r.get('name', sym))
        fpath = os.path.join(ONED, f"{sym}_1m.csv")
        if os.path.exists(fpath) and not args.force:
            try:
                if sum(1 for _ in open(fpath, 'rb')) - 1 >= TARGET:
                    skip += 1
                    continue
            except Exception:
                pass
        try:
            out = download_one(cli, code, sym, name)
        except Exception as e:
            print(f"  ⚠️ {sym} 异常: {e}")
            fail += 1
            continue
        if out is None or len(out) == 0:
            print(f"  ⚠️ {sym} 无数据")
            fail += 1
            continue
        out.to_csv(fpath, index=False, encoding='utf-8-sig')
        ok += 1
        print(f"  {sym:12s} {name:8s} {len(out)}根 -> {os.path.basename(fpath)}")
    print(f"\n=== 下载完成 ok={ok} skip={skip} fail={fail} ===")
    print(f"  目录: {ONED}")

if __name__ == '__main__':
    main()
