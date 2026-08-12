# -*- coding: utf-8 -*-
"""
pipeline_preflight.py — tpoint 每日流水线「前置自愈守卫」（2026-08-11 闭环迭代新增）

问题背景（多信号收敛证据）：
  - 2026-08-05：scripts/_today.py 丢失（本地 .git 已不存在）→ bat 的 %D% 变量未展开，九步全败。
  - 2026-08-11：scripts/backtest_screener.py 丢失 → live_roundtrip_review / prod_vs_bt_reconcile
    因 ModuleNotFoundError 失败，bat 仅 echo [WARN] 不中止不告警，产物静默缺失，验证 agent 数小时后才兜底。
  根因：本地 .git 缺失，脚本无法自恢复；若发生 git 重同步/误删，scripts/ 整目录可能被覆盖。

本脚本职责（在 bat 第 1 步前调用，非阻断式）：
  1. 校验关键叶脚本是否存在（backtest_screener / _today，历史上丢过的）；
  2. 缺失则从「tpoint 目录之外」的备份目录 C:/Users/YZP/.workbuddy/tpoint_selfheal/ 自恢复；
  3. 恢复后做真实 import 探测，确认可导入；
  4. 恢复成功 → 全局群推送「自愈恢复」信息（可观测）；恢复失败/无备份 → 全局群推送 ⚠️ 阻塞告警。
  5. 同时对所有流水线脚本做存在性巡检，缺失即记入报告（不自动恢复未备份项）。

非阻断设计：本脚本任何异常都不会导致 bat 中止 —— 它只做恢复 + 告警；即便自身出错，bat 仍继续跑原流水线（由验证 agent 兜底）。

CLI：python scripts/pipeline_preflight.py
退出码：0=全 OK 或已自愈成功；2=存在无法自愈的缺失（已告警）。
"""
import os, sys, json, shutil, subprocess, urllib.request, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = r"C:\Users\YZP\.workbuddy\tpoint_selfheal"
GLOBAL_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103"

# 历史上丢过的叶脚本：(导入名, 在 tpoint 内的相对路径, 备份文件名)
CRITICAL = [
    ("backtest_screener", os.path.join(ROOT, "scripts", "backtest_screener.py"), "backtest_screener.py"),
    ("_today",            os.path.join(ROOT, "scripts", "_today.py"),            "_today.py"),
]

# 全部流水线脚本（仅做存在性巡检，缺失记入报告；不自动恢复未备份项）
PIPELINE_SCRIPTS = [
    "daily_signal_review", "live_roundtrip_review", "review_charts", "build_review_html",
    "push_tpoint_review", "fdisk_daily_update", "prod_vs_bt_reconcile", "daily_report_push",
    "daily_iterate", "daily_closed_loop", "_today", "backtest_screener",
]
CORE_MODULES = ["exit_manager", "datasource", "miji_alpha"]


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def feishu_post(webhook, text):
    try:
        req = urllib.request.Request(webhook, data=json.dumps(
            {"msg_type": "text", "content": {"text": text}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        return f"POST_FAIL:{e}"


def try_import(name):
    """在独立子进程中尝试真实 import（带正确 sys.path），返回 (ok, detail)。"""
    code = (
        "import sys,os;"
        f"ROOT={ROOT!r};"
        "sys.path[:0]=[os.path.join(ROOT,'scripts'),os.path.join(ROOT,'core'),ROOT,"
        "os.path.join(ROOT,'venv','Lib','site-packages'),os.path.join(ROOT,'venv','Lib')];"
        "os.environ.setdefault('MACD_GATE_MODE','floor');"
        f"__import__({name!r})"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        return (r.returncode == 0, (r.stderr or r.stdout).strip()[-400:])
    except Exception as e:
        return (False, str(e))


def main():
    restored, unbacked, broken, missing_other = [], [], [], []

    # 1) 关键脚本自愈
    for mod, path, bak in CRITICAL:
        if os.path.exists(path):
            continue
        bak_path = os.path.join(BACKUP_DIR, bak)
        if os.path.exists(bak_path):
            try:
                shutil.copyfile(bak_path, path)
                restored.append(mod)
            except Exception as e:
                unbacked.append(f"{mod}(copy_err:{e})")
        else:
            unbacked.append(f"{mod}(no_backup)")

    # 2) 恢复后真实 import 探测
    for mod, path, bak in CRITICAL:
        ok, detail = try_import(mod)
        if not ok:
            broken.append(f"{mod}:{detail}")

    # 3) 其他脚本/模块存在性巡检（不自动恢复）
    for s in PIPELINE_SCRIPTS:
        if s in ("backtest_screener", "_today"):
            continue  # 已在 CRITICAL 处理
        p = os.path.join(ROOT, "scripts", f"{s}.py")
        if not os.path.exists(p):
            missing_other.append(f"scripts/{s}.py")
    for c in CORE_MODULES:
        p = os.path.join(ROOT, "core", f"{c}.py")
        if not os.path.exists(p):
            missing_other.append(f"core/{c}.py")

    # 4) 汇总 + 推送
    lines = [f"🔧 [tpoint pipeline_preflight {_ts()}]"]
    if restored:
        lines.append("✅ 自愈恢复: " + ", ".join(restored))
    if broken:
        lines.append("⚠️ 仍无法导入: " + "; ".join(broken))
    if unbacked:
        lines.append("⚠️ 缺失且无备份: " + "; ".join(unbacked))
    if missing_other:
        lines.append("⚠️ 其他缺失(未备份): " + "; ".join(missing_other))
    if not (restored or broken or unbacked or missing_other):
        lines.append("✅ 全部关键脚本/模块就位")
    msg = "\n".join(lines)

    # 日志
    logp = os.path.join(ROOT, "logs", "daily_review.log")
    try:
        with open(logp, "a", encoding="utf-8") as f:
            f.write(f"[{_ts()}] {msg}\n")
    except Exception:
        pass

    # 仅在「有恢复动作」或「存在无法自愈的缺失」时推全局群（避免无谓打扰）
    if restored or broken or unbacked:
        resp = feishu_post(GLOBAL_WEBHOOK, msg)
        print(f"[preflight] alert resp: {resp}")

    print(msg)
    # 退出码：无法自愈的缺失 → 2；否则 0
    return 2 if (broken or unbacked or missing_other) else 0


if __name__ == "__main__":
    sys.exit(main())
