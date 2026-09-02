# -*- coding: utf-8 -*-
"""
pipeline_status.py — T1 失败语义透传（自迭代闭环硬化方案 v2）
（docs/self_iteration_loop_hardening_plan.md）

设计（评审修正整合）：
  1. 每步开始前预写 RUNNING —— 进程中途被杀也能识别（INTERRUPTED），
     不再残留上一轮的成功状态。
  2. 每条记录带 run_id（YYYYMMDD-HHMMSS-pid，由 runtime_identity.py --begin 锚定），
     summarize 只读「当前日期 + 当前 run_id」——手工补跑两次不会互相污染。
  3. record 校验 expected_outputs 产物存在性：rc==0 但产物缺失 → DEGRADED；
     rc!=0 → FAILED；rc==77（约定跳过码）→ SKIPPED。
  4. 状态七值：RUNNING / OK / DEGRADED / FAILED / SKIPPED / NOT_RUN / INTERRUPTED。
  5. summarize 尾部调用（--push-fail）：关键步任一 FAILED/INTERRUPTED/NOT_RUN
     → 推 b4eba7a9 全局群 + exit 2（计划任务显示失败）。

CLI：
  python scripts/pipeline_status.py running <step> [--note msg]
  python scripts/pipeline_status.py record <step> <rc> [--expected p1;p2] [--note msg]
  python scripts/pipeline_status.py summarize [--run auto|<run_id>] [--push-fail]

bat 集成铁律：record 前必须先 `set RC=%ERRORLEVEL%` 捕获真实 rc
（本脚本的调用自身会重置 errorlevel）。
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATUS_DIR = os.path.join(DATA, "step_status")
CURRENT_RUN = os.path.join(STATUS_DIR, "current_run.json")

WEBHOOK_GLOBAL = "https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103"

# 流水线步骤序（run_daily_review.bat 的执行顺序；summarize 据此判 NOT_RUN）
STEP_ORDER = [
    "runtime_identity", "preflight", "recompute", "live_review",
    "charts", "build_html", "push_review", "fdisk_update",
    "reconcile", "daily_report", "daily_iterate", "closed_loop", "auto_tune",
]
# 关键步：任一 FAILED/INTERRUPTED/NOT_RUN → 汇总失败（exit 2 + 飞书告警）
CRITICAL_STEPS = {"live_review", "reconcile", "daily_report",
                  "daily_iterate", "closed_loop", "auto_tune"}
# 约定跳过码（如 daily_iterate 因前置失败被阻断时返回 77）
SKIP_RC = 77

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


def day_file(date=None):
    return os.path.join(STATUS_DIR, f"{date or _today()}.jsonl")


def current_run_id():
    """读 current_run.json（由 runtime_identity --begin 写入）。
    缺失时自动兜底重新 begin（T0 失败不阻断 T1 记录链）。"""
    try:
        with open(CURRENT_RUN, encoding="utf-8") as f:
            rid = json.load(f).get("run_id")
        if rid:
            return rid
    except Exception:
        pass
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from runtime_identity import begin_run
        ident = begin_run(note="fallback: current_run.json missing, auto-rebegun by pipeline_status")
        return ident["run_id"]
    except Exception as e:
        print(f"[pipeline_status] WARN fallback begin_run 失败: {e!r}", file=sys.stderr)
        return "fallback-" + _today()


def append_record(rec):
    os.makedirs(STATUS_DIR, exist_ok=True)
    path = day_file(rec.get("date"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_day_records(date=None, run_id=None):
    path = day_file(date)
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if run_id and r.get("run_id") != run_id:
                continue
            rows.append(r)
    return rows


def _last_started_at(rows, run_id, step):
    for r in reversed(rows):
        if r.get("run_id") == run_id and r.get("step") == step and r.get("status") == "RUNNING":
            return r.get("started_at")
    return None


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_running(step, note=""):
    rid = current_run_id()
    append_record({
        "date": _today(), "run_id": rid, "step": step, "status": "RUNNING",
        "started_at": _ts(), "pid": os.getpid(), "message": note or "",
    })
    return 0


def cmd_record(step, rc, expected="", note=""):
    rid = current_run_id()
    rows = load_day_records(run_id=rid)
    expected_outputs = [p for p in (expected or "").split(";") if p.strip()]
    missing = [p for p in expected_outputs if not os.path.exists(p)]

    if rc == SKIP_RC:
        status = "SKIPPED"
    elif rc == 0:
        status = "OK" if not missing else "DEGRADED"
    else:
        status = "FAILED"

    append_record({
        "date": _today(), "run_id": rid, "step": step, "rc": int(rc),
        "status": status,
        "started_at": _last_started_at(rows, rid, step),
        "finished_at": _ts(), "pid": os.getpid(),
        "expected_outputs": expected_outputs,
        "missing_outputs": missing,
        "message": note or "",
    })
    print(f"[pipeline_status] {rid} {step} rc={rc} -> {status}"
          + (f" missing={missing}" if missing else ""))
    return 0


def _feishu_post(webhook, text):
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        return f"POST_FAIL:{e}"


def cmd_summarize(run="auto", push_fail=False, date=None):
    rid = run if (run and run != "auto") else current_run_id()
    d = date or _today()
    rows = load_day_records(date=d, run_id=rid)

    # 每步取最后一条记录
    finals, order_seen = {}, []
    for r in rows:
        s = r.get("step")
        finals[s] = r
        if s not in order_seen:
            order_seen.append(s)

    steps = {}
    for s in STEP_ORDER:
        rec = finals.get(s)
        if rec is None:
            steps[s] = {"status": "NOT_RUN"}
        elif rec.get("status") == "RUNNING":
            steps[s] = {"status": "INTERRUPTED",
                        "detail": "进程中途被杀，无终态记录",
                        "started_at": rec.get("started_at")}
        else:
            steps[s] = {"status": rec.get("status"), "rc": rec.get("rc"),
                        "missing_outputs": rec.get("missing_outputs") or [],
                        "message": rec.get("message") or ""}

    critical_fail = [s for s in STEP_ORDER
                     if s in CRITICAL_STEPS
                     and steps[s]["status"] in ("FAILED", "INTERRUPTED", "NOT_RUN")]
    degraded = [s for s in STEP_ORDER if steps[s]["status"] == "DEGRADED"]

    summary = {
        "date": d, "run_id": rid, "generated_at": _ts(),
        "steps": steps, "critical_fail": critical_fail, "degraded": degraded,
        "overall": "FAIL" if critical_fail else ("DEGRADED" if degraded else "OK"),
    }
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(os.path.join(STATUS_DIR, f"summary_{d}_{rid}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[pipeline_status] WARN summary 落盘失败: {e!r}", file=sys.stderr)

    print(f"[pipeline_status] summary run={rid} overall={summary['overall']}")
    for s in STEP_ORDER:
        st = steps[s]["status"]
        mark = "✅" if st == "OK" else ("⚠️" if st in ("DEGRADED", "SKIPPED") else
               ("❌" if st in ("FAILED", "INTERRUPTED", "NOT_RUN") else "…"))
        print(f"  {mark} {s:<18} {st}")

    if critical_fail and push_fail:
        lines = [f"🚨 [tpoint 流水线失败] run={rid} date={d}"]
        for s in critical_fail:
            st = steps[s]
            lines.append(f"  ❌ {s}: {st['status']}"
                         + (f" rc={st.get('rc')}" if st.get("rc") is not None else "")
                         + (f" missing={st.get('missing_outputs')}" if st.get("missing_outputs") else ""))
        if degraded:
            lines.append(f"  ⚠️ DEGRADED: {', '.join(degraded)}")
        lines.append("详见 data/step_status/ 目录当日记录。")
        _feishu_post(WEBHOOK_GLOBAL, "\n".join(lines))

    return 2 if (critical_fail and push_fail) else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("running")
    p1.add_argument("step")
    p1.add_argument("--note", default="")
    p2 = sub.add_parser("record")
    p2.add_argument("step")
    p2.add_argument("rc", type=int)
    p2.add_argument("--expected", default="", help="分号分隔的应有产物路径")
    p2.add_argument("--note", default="")
    p3 = sub.add_parser("summarize")
    p3.add_argument("--run", default="auto")
    p3.add_argument("--push-fail", action="store_true")
    p3.add_argument("--date", default=None)
    args = ap.parse_args()

    if args.cmd == "running":
        return cmd_running(args.step, args.note)
    if args.cmd == "record":
        return cmd_record(args.step, args.rc, args.expected, args.note)
    if args.cmd == "summarize":
        return cmd_summarize(args.run, args.push_fail, args.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
