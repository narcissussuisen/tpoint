# -*- coding: utf-8 -*-
"""
runtime_identity.py — T0 运行身份与配置一致性预检查
（自迭代闭环硬化方案 v2，docs/self_iteration_loop_hardening_plan.md）

解决的核心问题：「文件生成了，但不知道究竟是哪套算法/哪套配置生成的」。
每次流水线开始时生成 run_id 并锚定运行身份（git commit / VERSION / 策略版本 /
配置 hash / 模型 hash / 成交口径版本），供 T1 step_status 与 T4 effect_ledger 消费。

职责边界：
  - 本脚本只记录与校验，不改任何生产配置、不影响信号行为。
  - canonical hash 函数（config_hash/watchlist_hash/model_hash/
    effective_strategy_hash）以此模块为唯一实现源，T4 effect_ledger.py 必须
    import 复用，禁止第二套实现（同源同模块铁律）。

CLI：
  python scripts/runtime_identity.py --begin [--note ...]   # 流水线头部调用
  python scripts/runtime_identity.py --show [run_id]        # 查看最近/指定运行身份
  python scripts/runtime_identity.py --hashes               # 只打印当前各层 hash（调试）
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RUNTIME_DIR = os.path.join(DATA, "runtime_identity")
CURRENT_RUN = os.path.join(DATA, "step_status", "current_run.json")

MONITOR_CONFIG = os.path.join(DATA, "monitor_config.json")
WATCHLIST = os.path.join(DATA, "watchlist.json")
MODEL_FILE = os.path.join(DATA, "ml", "topbottom_xgb.json")

VERSION_FILE = os.path.join(ROOT, "VERSION")
METHODOLOGY_FILE = os.path.join(ROOT, "METHODOLOGY_VERSION")

# 成交口径版本：T3-A 完成并正式切换（T3-B）前为 samebar-legacy。
# T3 施工时同步修改此常量为 nextbar-v1（并更新 docs/self_iteration_loop_hardening_plan.md）。
EXECUTION_MODEL_VERSION = "samebar-legacy"

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# canonical hash —— 唯一实现源（T4 复用）
# --------------------------------------------------------------------------- #
def _strip_notes(obj):
    """递归剔除注释性键：_note* 前缀 / comment / _comment。
    这些字段不改变交易行为，保留进 hash 会产生假变更警报。"""
    if isinstance(obj, dict):
        return {k: _strip_notes(v) for k, v in obj.items()
                if not (isinstance(k, str) and (k.startswith("_note") or k in ("comment", "_comment")))}
    if isinstance(obj, list):
        return [_strip_notes(x) for x in obj]
    return obj


def canonical_json_str(obj):
    return json.dumps(_strip_notes(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_str(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def config_hash():
    """规范化 monitor_config.json（剔除注释键、排序键）→ sha256。"""
    return sha256_str(canonical_json_str(_load_json(MONITOR_CONFIG)))


def watchlist_hash():
    """规范化 watchlist.json → sha256。"""
    return sha256_str(canonical_json_str(_load_json(WATCHLIST)))


def model_hash():
    """data/ml/topbottom_xgb.json 文件级 sha256；缺失返回 None（fail-open）。"""
    if not os.path.exists(MODEL_FILE):
        return None
    return sha256_file(MODEL_FILE)


def effective_strategy_hash(cfg_h, wl_h, mdl_h, exec_ver, strat_ver, engine, meth_ver):
    """有效策略身份 = 参数层 + 标的池层 + 模型层 + 成交口径层 + 版本层的组合 hash。
    任何一层变化都会使 effective_strategy_hash 变化，从而可区分
    「参数变了 / 标的池变了 / 模型变了 / 成交口径变了」。"""
    payload = {
        "config_hash": cfg_h,
        "watchlist_hash": wl_h,
        "model_hash": mdl_h,
        "execution_model_version": exec_ver,
        "strategy_version": strat_ver,
        "engine": engine,
        "methodology_version": meth_ver,
    }
    return sha256_str(canonical_json_str(payload))


# --------------------------------------------------------------------------- #
# git 状态
# --------------------------------------------------------------------------- #
def git_info():
    commit, dirty, dirty_files = None, None, []
    try:
        r = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            commit = r.stdout.strip()
        r2 = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=20)
        if r2.returncode == 0:
            lines = [ln.strip() for ln in r2.stdout.splitlines() if ln.strip()]
            dirty = bool(lines)
            dirty_files = lines[:20]
    except Exception:
        pass
    return commit, dirty, dirty_files


# --------------------------------------------------------------------------- #
# 配置一致性校验（warn 不阻断）
# --------------------------------------------------------------------------- #
def consistency_checks():
    warns, infos = [], []
    try:
        wl = _load_json(WATCHLIST)
        cfg = _load_json(MONITOR_CONFIG)
    except Exception as e:
        warns.append(f"配置文件不可解析: {e!r}")
        return warns, infos

    wl_symbols = set(wl.keys())
    cfg_symbols = set(k for k in cfg.keys() if k != "_global")
    cfg_only = cfg_symbols - wl_symbols
    wl_only = wl_symbols - cfg_symbols
    if cfg_only:
        warns.append(f"monitor_config per-symbol 存在 watchlist 外标的（陈旧残留?）: {sorted(cfg_only)}")
    if wl_only:
        infos.append(f"watchlist 标的无 per-symbol 条目（纯池级默认，正常）: {sorted(wl_only)}")

    ga = cfg.get("_global", {}).get("general_algorithm", {})
    if not ga.get("strategy_version"):
        warns.append("monitor_config._global.general_algorithm.strategy_version 缺失")
    return warns, infos


def _read_text(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# begin —— 生成运行身份
# --------------------------------------------------------------------------- #
def begin_run(note=""):
    now = datetime.datetime.now()
    date = now.strftime("%Y-%m-%d")
    run_id = now.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid())

    commit, dirty, dirty_files = git_info()
    warns, infos = consistency_checks()

    try:
        cfg_h = config_hash()
    except Exception as e:
        cfg_h = None
        warns.append(f"config_hash 计算失败: {e!r}")
    try:
        wl_h = watchlist_hash()
    except Exception as e:
        wl_h = None
        warns.append(f"watchlist_hash 计算失败: {e!r}")
    try:
        mdl_h = model_hash()
    except Exception as e:
        mdl_h = None
        warns.append(f"model_hash 计算失败: {e!r}")

    try:
        ga = _load_json(MONITOR_CONFIG).get("_global", {}).get("general_algorithm", {})
    except Exception:
        ga = {}
    strat_ver = ga.get("strategy_version")
    engine = ga.get("engine")

    identity = {
        "run_id": run_id,
        "date": date,
        "started_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "git_commit": commit,
        "git_dirty": dirty,
        "git_dirty_files": dirty_files,
        "version": _read_text(VERSION_FILE),
        "methodology_version": _read_text(METHODOLOGY_FILE),
        "strategy_version": strat_ver,
        "engine": engine,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "config_hash": cfg_h,
        "watchlist_hash": wl_h,
        "model_hash": mdl_h,
        "effective_strategy_hash": (
            effective_strategy_hash(cfg_h, wl_h, mdl_h, EXECUTION_MODEL_VERSION,
                                    strat_ver, engine, _read_text(METHODOLOGY_FILE))
            if cfg_h and wl_h else None
        ),
        "config_paths": {
            "monitor_config": os.path.relpath(MONITOR_CONFIG, ROOT),
            "watchlist": os.path.relpath(WATCHLIST, ROOT),
            "model": os.path.relpath(MODEL_FILE, ROOT) if mdl_h else None,
        },
        "consistency_warns": warns,
        "consistency_infos": infos,
        "note": note or "",
    }

    # 落盘（全部 try/except：T0 失败不阻断流水线，但必须留下痕迹）
    try:
        day_dir = os.path.join(RUNTIME_DIR, date)
        os.makedirs(day_dir, exist_ok=True)
        with open(os.path.join(day_dir, run_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(identity, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[runtime_identity] WARN 身份文件落盘失败: {e!r}")
    try:
        os.makedirs(os.path.dirname(CURRENT_RUN), exist_ok=True)
        with open(CURRENT_RUN, "w", encoding="utf-8") as f:
            json.dump({"run_id": run_id, "date": date,
                       "started_at": identity["started_at"]},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[runtime_identity] WARN current_run.json 写入失败: {e!r}")

    return identity


# --------------------------------------------------------------------------- #
# show / hashes
# --------------------------------------------------------------------------- #
def show_run(run_id=None):
    if run_id is None:
        cur = _read_text(CURRENT_RUN)
        if cur:
            try:
                run_id = json.loads(cur).get("run_id")
            except Exception:
                pass
        if run_id is None:
            # 取最新日期目录里最新文件
            try:
                dates = sorted(os.listdir(RUNTIME_DIR), reverse=True)
                for d in dates:
                    fs = sorted(os.listdir(os.path.join(RUNTIME_DIR, d)), reverse=True)
                    if fs:
                        run_id = fs[-1].removesuffix(".json")
                        break
            except Exception:
                pass
    if run_id is None:
        print("no run found")
        return 1
    # run_id 含日期前缀 YYYYMMDD-…
    date_guess = run_id[:4] + "-" + run_id[4:6] + "-" + run_id[6:8]
    path = os.path.join(RUNTIME_DIR, date_guess, run_id + ".json")
    if not os.path.exists(path):
        print(f"run file not found: {path}")
        return 1
    print(json.dumps(_load_json(path), ensure_ascii=False, indent=2))
    return 0


def print_hashes():
    ga = {}
    try:
        ga = _load_json(MONITOR_CONFIG).get("_global", {}).get("general_algorithm", {})
    except Exception:
        pass
    cfg_h, wl_h, mdl_h = None, None, None
    try:
        cfg_h = config_hash()
    except Exception:
        pass
    try:
        wl_h = watchlist_hash()
    except Exception:
        pass
    try:
        mdl_h = model_hash()
    except Exception:
        pass
    eff = None
    if cfg_h and wl_h:
        eff = effective_strategy_hash(cfg_h, wl_h, mdl_h, EXECUTION_MODEL_VERSION,
                                      ga.get("strategy_version"), ga.get("engine"),
                                      _read_text(METHODOLOGY_FILE))
    print(json.dumps({
        "config_hash": cfg_h,
        "watchlist_hash": wl_h,
        "model_hash": mdl_h,
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "strategy_version": ga.get("strategy_version"),
        "engine": ga.get("engine"),
        "effective_strategy_hash": eff,
    }, ensure_ascii=False, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--begin", action="store_true", help="生成运行身份并锚定 current_run")
    ap.add_argument("--show", nargs="?", const="__latest__", default=None,
                    help="查看运行身份（默认最新）")
    ap.add_argument("--hashes", action="store_true", help="只打印当前各层 hash")
    ap.add_argument("--note", default="", help="备注（随身份落盘）")
    args = ap.parse_args()

    if args.begin:
        ident = begin_run(args.note)
        print(ident["run_id"])  # 唯一 stdout 输出：run_id（供 bat 捕获）
        if ident["consistency_warns"]:
            for w in ident["consistency_warns"]:
                print(f"[runtime_identity] WARN {w}", file=sys.stderr)
        return 0
    if args.show is not None:
        rid = None if args.show == "__latest__" else args.show
        return show_run(rid)
    if args.hashes:
        return print_hashes()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
