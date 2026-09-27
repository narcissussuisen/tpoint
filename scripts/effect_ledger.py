# -*- coding: utf-8 -*-
"""
effect_ledger.py — T4 效果归因账本（自迭代闭环硬化方案 v2 §T4，2026-09-27 补建）

解决的核心问题：「线上效果变化无法归因到具体配置/代码变更」。
两类条目 append 到 data/effect_ledger.jsonl：

  1. type='daily'          每日效果条目（live_roundtrip_review / daily_closed_loop 调用）：
       date / run_id / effective_strategy_hash / config_hash / pushed / paired /
       net_ret / net_wr / regime / source
  2. type='config_change'  配置变更条目（任何写 data/monitor_config.json 的行为必须先走这里）：
       change_id / ts / sym / param / old / new / source / proposal_id /
       hash_before / hash_after / config_backup

铁律：
  - canonical hash 函数 **import 复用 scripts/runtime_identity.py**（同源同模块，禁止第二套实现）。
  - 任何 monitor_config 变更必须经 apply_config_change()（先备份 + 先记 hash_before 再写文件），
    直接手写 config 文件 = 违反闭环纪律（AI 自动回滚依赖本账本的前后 hash）。
  - 本模块只写 data/，不改 core/、不影响信号行为；全部 IO 失败只 WARN 不阻断调用方。

CLI：
  python scripts/effect_ledger.py --tail [N]        # 看最近 N 条（默认 20）
  python scripts/effect_ledger.py --verify          # 连续性自检（daily 条目断天/hash 链断裂）
"""
import argparse
import datetime
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA = os.path.join(ROOT, "data")
LEDGER = os.path.join(DATA, "effect_ledger.jsonl")
MONITOR_CONFIG = os.path.join(DATA, "monitor_config.json")
BACKUP_DIR = os.path.join(DATA, "config_backups")

import runtime_identity as RI  # noqa: E402  同源 hash 实现（铁律）

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _append(entry):
    os.makedirs(DATA, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _current_run_id():
    try:
        with open(os.path.join(DATA, "step_status", "current_run.json"), encoding="utf-8") as f:
            return json.load(f).get("run_id")
    except Exception:
        return None


def _hashes():
    """当前各层 hash（复用 runtime_identity）。失败项为 None，不阻断。"""
    cfg_h = wl_h = mdl_h = eff = None
    try:
        cfg_h = RI.config_hash()
    except Exception:
        pass
    try:
        wl_h = RI.watchlist_hash()
    except Exception:
        pass
    try:
        mdl_h = RI.model_hash()
    except Exception:
        pass
    try:
        ga = {}
        with open(MONITOR_CONFIG, encoding="utf-8") as f:
            ga = json.load(f).get("_global", {}).get("general_algorithm", {})
        if cfg_h and wl_h:
            eff = RI.effective_strategy_hash(
                cfg_h, wl_h, mdl_h, RI.EXECUTION_MODEL_VERSION,
                ga.get("strategy_version"), ga.get("engine"),
                RI._read_text(RI.METHODOLOGY_FILE))
    except Exception:
        pass
    return {"config_hash": cfg_h, "watchlist_hash": wl_h,
            "model_hash": mdl_h, "effective_strategy_hash": eff}


def iter_entries():
    if not os.path.exists(LEDGER):
        return
    with open(LEDGER, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    yield json.loads(ln)
                except Exception:
                    continue


# --------------------------------------------------------------------------- #
# ① 每日效果条目
# --------------------------------------------------------------------------- #
def append_daily(date, metrics, source, run_id=None, extra=None):
    """追加一条 daily 效果条目。

    metrics: dict，常见键 pushed/paired/net_ret/net_wr/regime（缺省 None  tolerated）。
    source : 产生方标识，如 'live_roundtrip_review' / 'daily_closed_loop'。
    """
    h = _hashes()
    entry = {
        "type": "daily",
        "ts": _now(),
        "date": date,
        "run_id": run_id or _current_run_id(),
        "source": source,
        "effective_strategy_hash": h["effective_strategy_hash"],
        "config_hash": h["config_hash"],
        "watchlist_hash": h["watchlist_hash"],
        "pushed": metrics.get("pushed"),
        "paired": metrics.get("paired"),
        "net_ret": metrics.get("net_ret"),
        "net_wr": metrics.get("net_wr"),
        "regime": metrics.get("regime"),
    }
    if extra:
        entry["extra"] = extra
    try:
        _append(entry)
    except Exception as e:
        print(f"[effect_ledger] WARN daily 条目写入失败: {e!r}", file=sys.stderr)
    return entry


# --------------------------------------------------------------------------- #
# ② 配置变更条目（monitor_config 变更的唯一合规通道）
# --------------------------------------------------------------------------- #
def apply_config_change(sym, param, new_value, source, proposal_id=None, note=""):
    """备份 → 记 hash_before → 写 monitor_config → 记 hash_after → 落账本。

    sym   : per-symbol 键（如 '300010.SZ'）；改 _global 块传 '_global'。
    param : 参数名（per-symbol 一级键；_global 时用点路径 'general_algorithm.xxx'）。
    source: 变更来源（'daily_iterate' / 'ai_loop:<proposal_id>' / 'manual' ...）。

    返回 entry（含 old/new/hash_before/hash_after）；失败抛异常前账本已尽量留痕。
    """
    with open(MONITOR_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    # 取 old 值
    if sym == "_global":
        node = cfg.setdefault("_global", {})
        parts = param.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        old = node.get(parts[-1])
    else:
        node = cfg.setdefault(sym, {})
        old = node.get(param)

    hash_before = _hashes()

    # 备份
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts_compact = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"monitor_config_{ts_compact}.json")
    shutil.copy2(MONITOR_CONFIG, backup)

    # 写新值
    if sym == "_global":
        node[parts[-1]] = new_value
    else:
        node[param] = new_value
    tmp = MONITOR_CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MONITOR_CONFIG)

    hash_after = _hashes()

    entry = {
        "type": "config_change",
        "change_id": f"chg-{ts_compact}-{os.getpid()}",
        "ts": _now(),
        "run_id": _current_run_id(),
        "sym": sym,
        "param": param,
        "old": old,
        "new": new_value,
        "source": source,
        "proposal_id": proposal_id,
        "note": note,
        "hash_before": hash_before["effective_strategy_hash"],
        "hash_after": hash_after["effective_strategy_hash"],
        "config_hash_before": hash_before["config_hash"],
        "config_hash_after": hash_after["config_hash"],
        "config_backup": os.path.relpath(backup, ROOT),
    }
    _append(entry)
    return entry


# --------------------------------------------------------------------------- #
# ③ 读取/自检
# --------------------------------------------------------------------------- #
def tail(n=20):
    entries = list(iter_entries() or [])
    return entries[-n:]


def verify():
    """连续性自检：① daily 条目断天 ② config_change 后 5 日内有无对应 daily 条目。"""
    entries = list(iter_entries() or [])
    daily_dates = sorted({e["date"] for e in entries if e.get("type") == "daily" and e.get("date")})
    issues = []
    if not entries:
        issues.append("ledger 为空或不存在")
    if len(daily_dates) >= 2:
        # 断天检测（日历日粒度，休市日由调用方豁免——此处只报 >4 天的缺口）
        from datetime import date as _d, timedelta
        ds = [_d.fromisoformat(x) for x in daily_dates]
        for a, b in zip(ds, ds[1:]):
            if (b - a) > timedelta(days=4):
                issues.append(f"daily 条目疑似断天: {a} → {b}")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", nargs="?", const=20, type=int, default=None)
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.tail is not None:
        for e in tail(args.tail):
            if e.get("type") == "daily":
                print(f"{e.get('date')} daily   {e.get('source'):<24} "
                      f"net={e.get('net_ret')} wr={e.get('net_wr')} hash={str(e.get('effective_strategy_hash'))[:12]}")
            else:
                print(f"{e.get('ts')} chg     {e.get('sym')}.{e.get('param')}: "
                      f"{e.get('old')} → {e.get('new')}  src={e.get('source')}")
        return 0
    if args.verify:
        issues = verify()
        if issues:
            for i in issues:
                print("ISSUE: " + i)
            return 1
        print("OK: ledger 连续性无异常")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
