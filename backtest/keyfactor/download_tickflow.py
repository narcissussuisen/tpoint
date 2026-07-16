#!/usr/bin/env python3
"""
Phase1b — tickflow 完整服务落地 1m 历史数据 (3-6 月 / 全市场样本)。
替代 mootdx(datasource.tdx_client)；用 tickflow API key (env TICKFLOW_API_KEY)。
落地: backtest/keyfactor_data/1m/{sym}_1m.csv
schema: symbol,name,timestamp,trade_date,trade_time,open,high,low,close,volume,amount
分页: tf.klines.get(period='1m', count=5000, end_time=<最旧ts>) 逐步回退。
   实测 count 上限=5000 (~20.8 交易日 ≈ 1 月); 6 页 ≈ 6 月。
并行: ThreadPoolExecutor(max_workers), 每标的顺序翻页。
断点续传: 已存在且行数 >= months*4800 的文件跳过 (重跑即续传)。
"""
import sys, os, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
from tickflow import TickFlow

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "keyfactor_data")
ONED = os.path.join(DATA, "1m")
os.makedirs(ONED, exist_ok=True)

PAGE = 5000                # tickflow 实测 1m count 上限
BARS_PER_MONTH = 21 * 240  # ~5040

_lock = threading.Lock()
_counter = {"ok": 0, "skip": 0, "fail": 0, "rows": 0}


def client():
    return TickFlow(api_key=os.environ.get("TICKFLOW_API_KEY", ""))


def get_page(tf, sym, end_time):
    for attempt in range(3):
        try:
            df = tf.klines.get(sym, period="1m", count=PAGE, end_time=end_time,
                              as_dataframe=True)
            return df
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return None


def fetch_symbol(tf, sym, name, months):
    target = int(months * BARS_PER_MONTH)
    pages, end_time, got = [], None, 0
    for _ in range(months * 2 + 2):
        df = get_page(tf, sym, end_time)
        if df is None or len(df) == 0:
            break
        pages.append(df)
        got += len(df)
        oldest = int(df["timestamp"].iloc[0])
        end_time = oldest
        if len(df) < PAGE:
            break
        if got >= target:
            break
    if not pages:
        return None
    big = pd.concat(pages, ignore_index=True)
    big = big.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out = pd.DataFrame({
        "symbol": sym,
        "name": name if name else sym,
        "timestamp": big["timestamp"].astype("int64"),
        "trade_date": big["trade_date"],
        "trade_time": big["trade_time"],
        "open": big["open"].astype(float),
        "high": big["high"].astype(float),
        "low": big["low"].astype(float),
        "close": big["close"].astype(float),
        "volume": big["volume"].astype(float),
        "amount": big["amount"].astype(float),
    })
    return out


def rowcount(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return -1


def worker(tf, sym, name, months, force):
    fpath = os.path.join(ONED, f"{sym}_1m.csv")
    need = int(months * BARS_PER_MONTH * 0.9)
    if (not force) and os.path.exists(fpath) and rowcount(fpath) >= need:
        with _lock:
            _counter["skip"] += 1
        return
    try:
        out = fetch_symbol(tf, sym, name, months)
    except Exception as e:
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} ERR {e}", flush=True)
        return
    if out is None or len(out) == 0:
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} NO DATA", flush=True)
        return
    out.to_csv(fpath, index=False, encoding="utf-8-sig")
    with _lock:
        _counter["ok"] += 1
        _counter["rows"] += len(out)
        n = _counter["ok"] + _counter["skip"] + _counter["fail"]
        if n % 50 == 0:
            print(f"  ...进度 ok={_counter['ok']} skip={_counter['skip']} fail={_counter['fail']} rows={_counter['rows']}", flush=True)


def save_universe(path):
    tf = client()
    q = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
    if q is None or len(q) == 0:
        print("  universe EMPTY")
        return 0
    df = pd.DataFrame({"sym": q["symbol"].tolist()})
    df["name"] = df["sym"]
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  universe saved: {len(df)} -> {path}")
    return len(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(DATA, "universe_ashare_full.csv"))
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--save-universe", action="store_true",
                    help="仅抓取并保存全 A股 universe 清单后退出")
    args = ap.parse_args()

    if args.save_universe:
        save_universe(args.manifest)
        return

    if not os.path.exists(args.manifest):
        print(f"  未找到 {args.manifest}, 先 --save-universe")
        return

    man = pd.read_csv(args.manifest, dtype={"sym": str, "name": str})
    if args.limit:
        man = man.head(args.limit)
    print(f"=== tickflow 下载: {len(man)} 只 × {args.months} 月, workers={args.workers} ===")

    tf = client()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(worker, tf, r["sym"], str(r.get("name", r["sym"])),
                      args.months, args.force) for _, r in man.iterrows()]
        for _ in as_completed(futs):
            pass
    print(f"\n=== 下载完成 ok={_counter['ok']} skip={_counter['skip']} "
          f"fail={_counter['fail']} rows={_counter['rows']} 用时{time.time()-t0:.0f}s ===")
    print(f"  目录: {ONED}")


if __name__ == "__main__":
    main()
