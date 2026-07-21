#!/usr/bin/env python3
"""tickflow 完整服务落地 1m 历史数据 (3-6 月 / 全市场样本)。
替代 mootdx(datasource.tdx_client)；用 tickflow API key (env TICKFLOW_API_KEY)。
落地: KEYFACTOR_DATA_DIR（默认 F:\workbuddy\keyfactor_data）/1m/{sym}_1m.csv
schema: symbol,name,timestamp,trade_date,trade_time,open,high,low,close,volume,amount
分页: tf.klines.get(period='1m', count=5000, end_time=<最旧ts>) 逐步回退。
   实测 count 上限=5000 (~20.8 交易日 ≈ 1 月); 6 页 ≈ 6 月。
并行: ThreadPoolExecutor(max_workers), 每标的顺序翻页。
断点续传: 已存在且行数 >= months*0.9*月bar数 的文件跳过。

★ 短历史标记 (.short_history.txt):
   若某票 tickflow 实拉不足 6 月 (首段即 <5000 行 / 或翻页提前耗尽历史),
   视为"上市不足 6 月 / tickflow 无更久数据", 一次性写标记, 此后 resume/backfill 均跳过,
   不再无限白拉。
"""
import sys, os, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
from tickflow import TickFlow
from dl_core import verify_csv, classify, DlError, IntegrityError, NetworkError

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

ONED = os.path.join(DATA, "1m")
os.makedirs(ONED, exist_ok=True)
SHORT_MARKER = os.path.join(DATA, ".short_history.txt")

PAGE = 5000                # tickflow 实测 1m count 上限
BARS_PER_MONTH = 21 * 240  # ~5040

# 超时/护栏策略(2026-07-17, 实测修正并发 hang):
#   ★ 真凶(已根治): worker 在 `with _lock:`(行239)内调 _mark_short(), 而 _mark_short
#     自身也 `with _lock:`(行75); 原 _lock 是普通 Lock(不可重入) -> 同线程持锁再抢
#     同锁自死锁, 任一只"短历史"票即永久卡死全部 worker, 表现为 0 落盘、连 TIMEOUT
#     都不打。治本: 将 _lock 改为 threading.RLock()(见下文), 允许同线程重入。
#   ◇ 以下为防御性兜底(应对"真网络 hang"这一不同且罕见的失效模式, 非本次根因):
#     tickflow 底层 httpx 默认 3 次重试 × 30s; 设 timeout=25s + max_retries=1 让单页
#     快速失败 -> classify 成 TimeoutError_ -> worker 记 fail 进缺口重下; daemon 线程护栏
#     SYMBOL_TIMEOUT 仅防"httpx 超时都不触发"的真死锁, 正常不触发。
HTTPX_TIMEOUT = 25.0      # 单次 httpx 调用超时(秒); 配合 max_retries=1, 单页最坏 ~50s
SYMBOL_TIMEOUT = 70.0     # daemon 线程整只票护栏(秒); 仅真死锁兜底, 正常不会触发


def _call_with_timeout(fn, timeout, *a, **k):
    """在 daemon 线程里跑 fn; 超时返回 (False, TimeoutError); 否则 (True, result)。"""
    box = {}
    def runner():
        try:
            box["r"] = fn(*a, **k)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return (False, TimeoutError(f"tickflow 调用超时 {timeout}s"))
    if "e" in box:
        return (False, box["e"])
    return (True, box.get("r"))

# 可重入锁: worker 在 `with _lock:` 块内(行239)会调用 _mark_short(),
# 而 _mark_short 自身也 `with _lock:`(行75)。若用普通 Lock 则同一线程
# 持锁后再抢同一把锁 -> 自死锁(即本次并发 hang 的真凶)。
# 改用 RLock 允许同线程重入, 死锁消除; 并发安全性不变(写 _short_set/计数
# 仍被锁串行化)。
_lock = threading.RLock()
_counter = {"ok": 0, "skip": 0, "short": 0, "fail": 0, "rows": 0}
_short_set = set()


def _load_short_set():
    if os.path.exists(SHORT_MARKER):
        with open(SHORT_MARKER, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    _short_set.add(s)


def _mark_short(sym):
    with _lock:
        if sym in _short_set:
            return
        _short_set.add(sym)
        try:
            with open(SHORT_MARKER, "a", encoding="utf-8") as f:
                f.write(sym + "\n")
        except Exception:
            pass


def client():
    # 显式短超时 + 仅 1 次重试: 真 hang 的票立即由 httpx 抛超时异常,
    # 经 classify 转为 TimeoutError_ 走失败/重下逻辑, 不再长时间占用 worker 线程。
    return TickFlow(api_key=os.environ.get("TICKFLOW_API_KEY", ""),
                    timeout=HTTPX_TIMEOUT, max_retries=1)


def get_page(tf, sym, end_time):
    """拉一页; 直接调用(无嵌套线程, 避免并发死锁); 失败按框架分类。

    返回 DataFrame; 超时由 client 级 timeout=HTTPX_TIMEOUT 控制
    (Klines 资源方法不转发 per-call timeout, 但 _request 会回退到
    self.timeout, 即 client() 设的 25s), httpx 超时即抛 TimeoutException,
    classify 成 TimeoutError_ 上抛, 交由 fetch_symbol/worker 走失败重下。
    整只票的 daemon 护栏(SYMBOL_TIMEOUT)仅作真死锁兜底, 正常不会触发。
    """
    try:
        return tf.klines.get(sym, period="1m", count=PAGE, end_time=end_time,
                              as_dataframe=True)
    except Exception as e:
        de = classify(e, sym=sym)
        raise de


def fetch_symbol(tf, sym, name, months):
    """返回 (out_df | None, short_bool)。
    short=True 表示 tickflow 实拉不足 6 月 (历史到头 / 首段即短)。"""
    target = int(months * BARS_PER_MONTH)
    pages, end_time, got = [], None, 0
    short = False
    for _ in range(months * 2 + 2):
        try:
            df = get_page(tf, sym, end_time)
        except DlError:
            raise
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
        return None, False
    # 翻页提前耗尽历史 (got < target) => 短历史
    if got < target:
        short = True
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
    return out, short


def rowcount(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return -1


def worker(tf, sym, name, months, force):
    tf = client()  # 每个 worker 独立 client: 避免共享 client 在并发 fetch_symbol 下死锁/连接池争用
    fpath = os.path.join(ONED, f"{sym}_1m.csv")
    tmp = fpath + ".tmp"
    need = int(months * BARS_PER_MONTH * 0.9)
    # ① 已达标 => 跳过
    if (not force) and os.path.exists(fpath) and rowcount(fpath) >= need:
        with _lock:
            _counter["skip"] += 1
        return
    # ② 短历史标记 => 跳过 (除非 --force 强制重下)
    if (not force) and sym in _short_set:
        with _lock:
            _counter["skip"] += 1
        return
    # 抓取: 整只票一次性超时护栏 (daemon 线程包裹 fetch_symbol,
    # 超 SYMBOL_TIMEOUT 即放弃该票 -> 进缺口重下; 仅 2 层线程(pool->daemon),
    # 契合已验证可用的并发模式, 不再逐页嵌套 daemon 导致死锁。
    try:
        ok, res = _call_with_timeout(
            lambda: fetch_symbol(tf, sym, name, months), SYMBOL_TIMEOUT)
    except Exception as e:
        ok, res = False, e
    if not ok:
        if isinstance(res, TimeoutError):
            with _lock:
                _counter["fail"] += 1
            print(f"  !! {sym} TIMEOUT >{SYMBOL_TIMEOUT:.0f}s -> 放弃重下",
                  flush=True)
            return
        with _lock:
            _counter["fail"] += 1
        cat = getattr(res, "category", type(res).__name__)
        print(f"  !! {sym} {cat} ERR {res}", flush=True)
        return
    out, short = res
    if out is None or len(out) == 0:
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} NO DATA", flush=True)
        return
    # ③ 落盘前: 先写 .tmp
    try:
        out.to_csv(tmp, index=False, encoding="utf-8-sig")
    except Exception as e:
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} WRITE ERR {e}", flush=True)
        return
    # ④ 完整性校验 (短历史票只做结构校验)
    rep = verify_csv(tmp, sym, months, expect_full=(not short))
    if not rep.ok:
        # 隔离坏文件到 .bad, 不写最终 .csv -> 留作缺口待重下
        try:
            bad = fpath + ".bad"
            if os.path.exists(bad):
                os.remove(bad)
            os.replace(tmp, bad)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} INTEGRITY {rep.errors} -> 隔离 .bad", flush=True)
        return
    # ⑤ 校验通过 -> 原子改名落盘
    try:
        if os.path.exists(fpath):
            os.remove(fpath)
        os.replace(tmp, fpath)
    except Exception as e:
        with _lock:
            _counter["fail"] += 1
        print(f"  !! {sym} RENAME ERR {e}", flush=True)
        return
    with _lock:
        if short:
            _mark_short(sym)
            _counter["short"] += 1
        else:
            _counter["ok"] += 1
        _counter["rows"] += len(out)
        n = _counter["ok"] + _counter["skip"] + _counter["fail"] + _counter["short"]
        if n % 50 == 0:
            print(f"  ...进度 ok={_counter['ok']} skip={_counter['skip']} "
                  f"short={_counter['short']} fail={_counter['fail']} rows={_counter['rows']}", flush=True)


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


def load_manifest(path):
    return pd.read_csv(path, dtype={"sym": str, "name": str})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(DATA, "universe_ashare_full.csv"))
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--symbol", default="",
                    help="单只模式: 只下这一只 (从 manifest 取 name)")
    ap.add_argument("--symbols-file", default="",
                    help="批量补单只: 每行 sym 或 sym,name")
    ap.add_argument("--save-universe", action="store_true",
                    help="仅抓取并保存全 A股 universe 清单后退出")
    args = ap.parse_args()

    _load_short_set()

    if args.save_universe:
        save_universe(args.manifest)
        return

    if not os.path.exists(args.manifest):
        print(f"  未找到 {args.manifest}, 先 --save-universe")
        return

    man = load_manifest(args.manifest)

    # 单只模式
    if args.symbol:
        row = man[man["sym"] == args.symbol]
        name = str(row.iloc[0]["name"]) if len(row) else args.symbol
        print(f"=== 单只 {args.symbol} × {args.months} 月 ===")
        t0 = time.time()
        tf = client()
        worker(tf, args.symbol, name, args.months, args.force)
        print(f"=== 完成 用时{time.time()-t0:.0f}s "
              f"(ok={_counter['ok']} short={_counter['short']} fail={_counter['fail']}) ===")
        return

    # 批量补单只
    if args.symbols_file:
        syms = []
        with open(args.symbols_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                syms.append(line.split(",")[0])
        print(f"=== 批量补 {len(syms)} 只 × {args.months} 月 ===")
        tf = client()
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(worker, tf, s,
                               str(man[man["sym"] == s]["name"].iloc[0]) if len(man[man["sym"] == s]) else s,
                               args.months, args.force) for s in syms]
            for _ in as_completed(futs):
                pass
        print(f"\n=== 批量补完成 ok={_counter['ok']} short={_counter['short']} "
              f"skip={_counter['skip']} fail={_counter['fail']} rows={_counter['rows']} "
              f"用时{time.time()-t0:.0f}s ===")
        return

    # 正常分块模式
    if args.offset:
        man = man.iloc[args.offset:]
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
          f"short={_counter['short']} fail={_counter['fail']} rows={_counter['rows']} "
          f"用时{time.time()-t0:.0f}s ===")
    print(f"  目录: {ONED}")


if __name__ == "__main__":
    main()
