#!/usr/bin/env python3
"""tpoint 自迭代下载引擎 (数据完整性 + 异常框架)。

设计 (对应 "做好自迭代 + 自带数据完整性检测 + 完整异常处理框架"):
  - 自迭代: 反复 scan_gaps -> 下载缺口 -> 退避 -> 再扫描, 直到全达标或达到
    max_passes。进程内不依赖人工重跑; 被杀后重跑会从断点(缺口)续。
  - 数据完整性: 每个文件落盘前经 dl_core.verify_csv 校验 (download_tickflow 内联),
    引擎侧再用 IntegrityStore 缓存复核: 不达标文件隔离到 .bad 并重新下载。
  - 异常框架: 抓取/校验异常统一归类为 dl_core.DlError 异常族 (network/timeout/
    ratelimit/auth/api/partial/integrity/disk), 可重试与不可重试分流; 飞书里程碑同步。

用法:
  python download_supervisor.py                  # 自迭代直到完成
  python download_supervisor.py --max-passes 10 --backoff 20
  python download_supervisor.py --audit-only     # 仅做完整性审计并出 HTML 报告
  python download_supervisor.py --reset-store    # 清完整性缓存(强制全量重验)
  python download_supervisor.py --once           # 只跑一轮(扫描+下载缺口)后退出
"""
import os
import sys
import json
import time
import argparse
import subprocess
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR


import pandas as pd
from dl_core import IntegrityStore, verify_csv
from feishu_push import push

DATA = KEYFACTOR_DATA_DIR
MANIFEST = os.path.join(DATA, "universe_ashare_full.csv")
ONED = os.path.join(DATA, "1m")
CKPT = os.path.join(DATA, ".download_ckpt.json")        # 旧 chunk 检查点(保留, 不再主用)
SHORT_MARKER = os.path.join(DATA, ".short_history.txt")
STORE_PATH = os.path.join(DATA, ".integrity_store.json")
DOWNLOADER = os.path.join(HERE, "download_tickflow.py")
LOCK = os.path.join(DATA, ".engine.lock")   # 运行锁: 防双引擎竞跑

DEFAULT_TIMEOUT = 600        # 单轮下载子进程超时(秒) — 缩短以加快 hang 自愈(用户要求 ~10min)
DEFAULT_MAX_PASSES = 12
DEFAULT_BACKOFF = 15.0
DEFAULT_BACKOFF_MAX = 300.0


# ---------------- 共用辅助 ----------------
def load_manifest(path):
    return pd.read_csv(path, dtype={"sym": str, "name": str})


def load_short_set():
    s = set()
    if os.path.exists(SHORT_MARKER):
        with open(SHORT_MARKER, "r", encoding="utf-8") as f:
            for line in f:
                x = line.strip()
                if x:
                    s.add(x)
    return s


def count_done():
    import glob
    return len(glob.glob(os.path.join(ONED, "*.csv")))


def move_to_bad(fpath):
    """把坏文件隔离到 .bad (保留佐证), 让下载器下轮重新拉取。"""
    bad = fpath + ".bad"
    try:
        if os.path.exists(bad):
            os.remove(bad)
        os.replace(fpath, bad)
    except Exception:
        try:
            os.remove(fpath)
        except Exception:
            pass


def acquire_lock():
    """运行锁: 若已有同引擎实例在跑(PID 存活), 直接退出避免双跑竞态。
    陈旧锁(PID 已死)自动清除。"""
    if os.path.exists(LOCK):
        try:
            with open(LOCK, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)          # 抛 OSError 表示进程已死
            print(f"  ⚠️ 引擎已在运行 (PID {pid}), 退出避免双跑竞态")
            sys.exit(0)
        except (OSError, ValueError):
            try:
                os.remove(LOCK)
            except Exception:
                pass
    try:
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


def run_downloader(args, extra):
    cmd = [sys.executable, DOWNLOADER] + extra
    t0 = time.time()
    try:
        r = subprocess.run(cmd, timeout=args.timeout, capture_output=True, text=True)
        return r.returncode, time.time() - t0
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.time() - t0
    except Exception as e:
        return f"ERR {e}", time.time() - t0


# ---------------- 自迭代核心 ----------------
def scan_gaps(args, store, short, man):
    """扫描缺口: 缺失 / 完整性不达标(隔离到 .bad 后计入)。
    返回 [(sym, name, reason), ...]。未变更文件命中 IntegrityStore 缓存, 代价可控。"""
    gaps = []
    for _, r in man.iterrows():
        sym = r["sym"]
        name = str(r.get("name", sym))
        fp = os.path.join(ONED, f"{sym}_1m.csv")
        if not os.path.exists(fp):
            gaps.append((sym, name, "missing"))
            continue
        rep = store.check(sym, fp, args.months, short)
        if rep.ok:
            continue
        # 不达标 -> 隔离, 下轮重拉
        move_to_bad(fp)
        gaps.append((sym, name, rep.errors[0] if rep.errors else "bad"))
    return gaps


def self_iterate(args):
    store = IntegrityStore(STORE_PATH)
    short = load_short_set()
    man = load_manifest(args.manifest)
    total = len(man)
    push(f"【tpoint 引擎】自迭代启动: 共 {total} 只, 已落盘 {count_done()}")
    started = time.time()
    final_ok = False

    for attempt in range(1, args.max_passes + 1):
        gaps = scan_gaps(args, store, short, man)
        good = total - len(gaps)
        pct = 100 * good // total
        if not gaps:
            push(f"【tpoint 引擎】✅ 全部达标 {good}/{total} ({pct}%) "
                  f"用时 {int(time.time() - started)}s")
            final_ok = True
            break
        # 写缺口清单
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         dir=DATA, delete=False, encoding="utf-8")
        for s, _, _ in gaps:
            tf.write(s + "\n")
        tf.close()
        push(f"【tpoint 引擎】第 {attempt}/{args.max_passes} 轮: 缺口 {len(gaps)} 只 "
              f"({pct}% 达标), 重下...")
        rc, dur = run_downloader(
            args, ["--symbols-file", tf.name, "--months", str(args.months),
                    "--workers", str(args.workers), "--force"])
        print(f"  第{attempt}轮 子进程 rc={rc} 用时{dur:.0f}s 缺口{len(gaps)}", flush=True)
        if args.once:
            break
        if attempt < args.max_passes:
            bo = min(args.backoff * (2 ** (attempt - 1)), args.backoff_max)
            print(f"  退避 {bo:.0f}s 后进入下一轮扫描", flush=True)
            time.sleep(bo)

    # 末轮复核
    gaps = scan_gaps(args, store, short, man)
    good = total - len(gaps)
    if not gaps:
        push(f"【tpoint 引擎】✅ 完成: 全部达标 {good}/{total} (100%)")
        final_ok = True
    else:
        with open(os.path.join(DATA, ".engine_gaps.txt"), "w", encoding="utf-8") as f:
            for s, n, r in gaps:
                f.write(f"{s}\t{n}\t{r}\n")
        push(f"【tpoint 引擎】⚠️ 达最大轮次仍有缺口 {len(gaps)}/{total}, "
              f"清单见 .engine_gaps.txt")
    store.flush()      # 批量化落盘收尾, 保证缓存最新
    return final_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--max-passes", type=int, default=DEFAULT_MAX_PASSES)
    ap.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF)
    ap.add_argument("--backoff-max", type=float, default=DEFAULT_BACKOFF_MAX)
    ap.add_argument("--once", action="store_true", help="只跑一轮(扫描+下载缺口)后退出")
    ap.add_argument("--no-notify", action="store_true", help="不发飞书心跳")
    ap.add_argument("--audit-only", action="store_true", help="仅做完整性审计(委托 audit_integrity)")
    ap.add_argument("--reset-store", action="store_true", help="清完整性缓存(强制全量重验)")
    args = ap.parse_args()

    # 审计模式: 委托 audit_integrity
    if args.audit_only:
        from audit_integrity import run_audit
        run_audit(args.manifest, args.months)
        return

    if not os.path.exists(args.manifest):
        print(f"  未找到 {args.manifest}, 先跑 download_tickflow.py --save-universe")
        return
    if args.reset_store and os.path.exists(STORE_PATH):
        try:
            os.remove(STORE_PATH)
            print("  已清完整性缓存")
        except Exception:
            pass

    acquire_lock()
    try:
        self_iterate(args)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
