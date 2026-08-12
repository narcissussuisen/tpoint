# -*- coding: utf-8 -*-
"""
feedback_loop.py — tpoint 自迭代闭环驱动（2026-08-11 新增，验证/兜底 agent 的回灌通道）

定位：把「验证/兜底 agent 发现的事故与改进建议」转化为 tpoint 系统的自我迭代输入，
构建 发现问题→分析研判→回灌建议→系统优化→再验证 的持续闭环。

职责边界（严守）：
  - 不改动 core/ 生产代码、不改动 monitor_config.json（参数变更归 daily_iterate 护栏）。
  - 仅做：① 维护 data/feedback_backlog.jsonl（结构化 issue 库）；② 对安全可自动化项执行回灌
    （确保自愈备份/守卫就位）；③ 触发再验证（preflight + 目标流水线步）；④ 向 a35d7f52 自迭代群
    推送回灌摘要。
  - 每个修复必须附「再验证证据」并写回 backlog status=verified。

调用：python scripts/feedback_loop.py [--date 2026-08-11]
建议由独立 recurring automation（每日 15:45，复盘后）驱动，使闭环持续运行。
"""
import os, sys, json, subprocess, shutil, datetime, urllib.request, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKLOG = os.path.join(ROOT, "data", "feedback_backlog.jsonl")
BACKUP_DIR = r"C:\Users\YZP\.workbuddy\tpoint_selfheal"
SELF_ITER_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d"

CRITICAL_SCRIPTS = [  # (导入名, scripts内路径, 备份名)
    ("backtest_screener", os.path.join(ROOT, "scripts", "backtest_screener.py"), "backtest_screener.py"),
    ("_today", os.path.join(ROOT, "scripts", "_today.py"), "_today.py"),
]


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


# --------------------------------------------------------------------------- #
# 1) 结构化 backlog（首次运行时以多信号证据落地今日+历史高价值发现）
# --------------------------------------------------------------------------- #
SEED = [
    {
        "id": "P0-20260811-selfheal", "severity": "P0", "automatable": True,
        "category": "pipeline_robustness", "fix_id": "selfheal_preflight",
        "title": "关键叶脚本(backtest_screener/_today)丢失无自恢复，bat 静默 non-zero",
        "evidence": "08-05 _today.py 丢失致 %D% 空→九步全败；08-11 backtest_screener.py 丢失致 "
                    "live_roundtrip_review/prod_vs_bt_reconcile ModuleNotFoundError，bat 仅 echo [WARN]，"
                    "产物静默缺失，验证 agent 6.5h 后才兜底。根因：本地 .git 缺失无法自恢复。",
        "proposed_fix": "新增 scripts/pipeline_preflight.py（前置自愈守卫：缺失关键脚本从 tpoint 外备份 "
                        "C:/Users/YZP/.workbuddy/tpoint_selfheal 恢复，失败推全局群）；以非阻断方式接入 bat 第1步前。",
        "status": "open",
    },
    {
        "id": "P0-20260811-git-hygiene", "severity": "P0", "automatable": False,
        "category": "env_hygiene", "fix_id": "git_hygiene",
        "title": "本地 .git 缺失，脚本/版本无法自恢复",
        "evidence": "Claw/tpoint 无 .git；SSH 拉 GitHub 无权限、main 分支过旧无新脚本；脚本丢失只能手动从 "
                    "v10.0.0 分支取。",
        "proposed_fix": "本机 git init + 绑定 remote（或用工作区备份自动还原），使脚本可版本化自恢复；"
                        "纳入运维 checklist。",
        "status": "open",
    },
    {
        "id": "P1-20260811-bat-early-alert", "severity": "P1", "automatable": False,
        "category": "observability", "fix_id": "bat_early_alert",
        "title": "bat 对关键步失败仅 echo [WARN]，无早期告警",
        "evidence": "run_daily_review.bat 各步 `if errorlevel 1 echo [WARN]` 不中止、不推告警，"
                    "静默缺失依赖验证 agent 兜底（今日延迟 6.5h）。",
        "proposed_fix": "bat 尾部对关键步(live_review/reconcile/iterate/closed_loop)失败做汇总，"
                        "立即推全局群告警（一次性，非逐步 spam）。需谨慎编辑 bat 避免破坏。",
        "status": "open",
    },
    {
        "id": "P1-20260811-r1-live-recalc", "severity": "P1", "automatable": False,
        "category": "data_quality", "fix_id": "r1_live_recalc_diagnostic",
        "title": "live=0 vs recalc≥2 反复出现（生产侧抑制/落盘断流，R1 靶点）",
        "evidence": "08-05 161129/688111/300308；08-06 513310/688111/300308；08-07 161129/300757；"
                    "08-11 161129(Δ2)/300757(Δ4)。live=0 但复算≥2 笔，疑似首扫抑制/落盘断流，非算法失效。",
        "proposed_fix": "闭环校验=无效时除 gate_ablation 外，增加 live-vs-recalc 差异自动诊断（push_audit/"
                        "monitor 状态/落盘时戳多信号收敛），区分算法卡死 vs 生产侧抑制，缩短 R1 排查周期。",
        "status": "open",
    },
    {
        "id": "P2-20260811-fdisk-ghost", "severity": "P2", "automatable": True,
        "category": "pipeline_robustness", "fix_id": "preflight_cover",
        "title": "fdisk_daily_update 曾为幽灵脚本（bat 引用不存在文件）",
        "evidence": "08-05 日志 fdisk_update non-zero：脚本不存在；已从 git 历史还原。",
        "proposed_fix": "pipeline_preflight 已对所有流水线脚本做存在性巡检并告警，覆盖此类幽灵脚本。",
        "status": "open",
    },
    {
        "id": "P1-20260811-verify-delay", "severity": "P1", "automatable": False,
        "category": "observability", "fix_id": "verify_tight_coupling",
        "title": "验证 agent 触发延迟大（今日 21:58 才跑，距 15:30 达 6.5h）",
        "evidence": "scheduled automation 15:35 触发，但本机时间显示验证动作至 21:58 才执行，兜底空窗长。",
        "proposed_fix": "bat 尾部写状态标记(如 data/last_review_done.json 含 step_status)，验证 agent 读标记"
                        "立即校验并告警，缩短空窗；或把验证 agent 调度紧贴 bat 完成。",
        "status": "open",
    },
    # --- 2026-08-11 晚 增补：外部资料研判回灌（国金《DeepSeek V4 Flash 金融投研测评》
    #     对三个大模型回测方法论的共性批评清单，逐条对照 tpoint 自查所得） --- #
    {
        "id": "P0-20260811-cfg-state-leak", "severity": "P0", "automatable": False,
        "category": "backtest_validity", "fix_id": "signal_exit_same_param",
        "title": "寻优引擎配置状态泄漏：信号重放读生产 trail，网格只换出场 trail → 混合口径、不可复现",
        "evidence": "core/monitor.py:1188 信号重放读 exit_param(sym,'trail_activate_pct'/'trail_pct') 生成出场提示信号；"
                    "factor_optimizer.eval_config 仅在 simulate_day 侧传网格 trail，PER_SYMBOL_CFG 仍为生产值 → "
                    "结果=『生产trail信号 + 网格trail出场』。实测同一 (513310,trail=0.3/0.5,split=0.7) 两次得 "
                    "n=33/31、Δwr -2.0/+3.2pp（结论相反），随当时 monitor_config 而变；修复为信号侧与出场侧同参后"
                    "稳定复现 Δret+2.51pp/Δwr+0.7pp。已修 oos_validate.py，factor_optimizer.py 仍带此泄漏。",
        "proposed_fix": "factor_optimizer.eval_config/day_signals 在评估每个网格单元前把该单元 trail 同步写入 "
                        "M.PER_SYMBOL_CFG（跑完恢复），使信号与出场同参；报告加 signal_exit_same_param 标记。"
                        "过渡期纪律：factor_opt 仅作候选粗筛，放行以修复后 OOS 为准（auto_tune 已如此实现）。",
        "status": "open",
    },
    {
        "id": "P0-20260811-reverify-0805", "severity": "P0", "automatable": False,
        "category": "backtest_validity", "fix_id": "reverify_history_with_fixed_pipeline",
        "title": "08-05 历史上线/回退结论出自未修复的混合口径，可信度存疑需重验",
        "evidence": "08-05 以『wr 较基线下降(513310 -1.2pp/688111 -0.3pp)不达 wr不降硬约束』回退 v10.0.1；"
                    "同日以 total_ret 优先给 161129(0.5/0.6)、300308(0.5/0.6) 上线。二者均基于带配置状态泄漏的"
                    "factor_optimizer 口径 —— 08-11 已证该口径下同参数结论可反向。",
        "proposed_fix": "用修复口径(信号/出场同参 + IS/OOS 切分)对 161129/300308/688111/513310 的现行 trail 重跑复核，"
                        "对结论翻转者按护栏自动收敛；结果写入 weekly_review 周报。",
        "status": "open",
    },
    {
        "id": "P1-20260811-no-oos", "severity": "P1", "automatable": True,
        "category": "overfitting_control", "fix_id": "oos_guard",
        "title": "全样本网格最优直接上生产，无样本外/walk-forward 防过拟合",
        "evidence": "factor_optimizer 为 F盘全历史一次性网格；auto_tune 原直接取全样本 best_cell 写 "
                    "monitor_config.json，无任何 IS/OOS 切分。外部资料指出三个受测模型同样『未使用样本外检验或 "
                    "walk-forward』，属同构缺陷。",
        "proposed_fix": "新增 scripts/oos_validate.py（IS/OOS 70/30 时间切分，PASS/FAIL/INCONCLUSIVE 三态，"
                        "样本不足不得视为通过）；作为 auto_tune 第6条护栏，未 PASS 不得写生产。",
        "status": "fixed",
    },
    {
        "id": "P1-20260811-lookahead-samebar", "severity": "P1", "automatable": False,
        "category": "backtest_validity", "fix_id": "exec_next_bar",
        "title": "信号bar==执行bar，无 +1 bar 延迟（分钟级做T前视嫌疑）",
        "evidence": "core/exit_manager.simulate_day: 空仓遇 B 即 entry_price=b['price'] 于同一 bar i 成交；"
                    "出场同样用 c[i]。信号在 bar i 收盘才成立，却以该 bar 价格成交，理论可达但实盘难精确复现，"
                    "做T高频次下偏差会系统性累积（现仅靠 2bps/边滑点部分补偿）。外部资料把『信号日与执行日口径"
                    "不一致/含调仓日当天』明确列为前视偏差。",
        "proposed_fix": "增设 exec_delay_bars 开关（默认1）以次 bar 开盘价成交，跑一次口径对照：若净收益显著缩水，"
                        "说明现有结论含前视水分，需以延迟口径为准重定基线；或提高滑点假设至可覆盖水平。",
        "status": "open",
    },
    {
        "id": "P1-20260811-limit-halt", "severity": "P1", "automatable": False,
        "category": "backtest_validity", "fix_id": "tradability_filter",
        "title": "回测未处理涨跌停/停牌不可交易状态",
        "evidence": "simulate_day/backtest_screener 无 limit_up/limit_down/suspend 判定。做T标的含创业板 300757/"
                    "300308(±20%)、科创板 688111(±20%)、LOF 161129(可停牌/溢价波动)：涨停时买不到、跌停时卖不掉，"
                    "回测按 c[i] 照常成交会虚增收益。外部资料把『未处理涨跌停』列为三模型共性缺陷。",
        "proposed_fix": "在 1m 数据层加可交易性标记（一字板/封板、停牌缺bar），simulate_day 遇不可成交则顺延或放弃该 trip；"
                        "先做影响量化（受影响 trip 占比与收益贡献）再决定严格程度。",
        "status": "open",
    },
    {
        "id": "P2-20260811-cost-sensitivity", "severity": "P2", "automatable": True,
        "category": "robustness", "fix_id": "cost_stress_guard",
        "title": "缺成本敏感性压力检验（做T对费率/滑点弹性极大）",
        "evidence": "成本模型本身已达标（万一不免五+印花税万5.641仅沪深个股+滑点2bps/边，按标的类型自动切换，"
                    "优于外部资料中三个受测模型），但从未做敏感性：外部资料显示单边 0.03% 成本即可令回测盈亏翻转。"
                    "做T换手极高，滑点低估会直接吃掉全部 alpha。",
        "proposed_fix": "auto_tune 增设成本压力护栏：候选参数在滑点上浮 50%（3bps/边）口径下仍须满足 total_ret 改善，"
                        "否则判定为『成本脆弱』不予放行；周报输出各标的盈亏平衡滑点阈值。",
        "status": "open",
    },
]


def seed_backlog():
    """按 id 增量补种：已存在的条目保持原状态不覆盖，新 SEED 条目追加。

    2026-08-11 晚修正：原实现「backlog 非空即 return」，导致后续新增的 SEED
    永远进不了 backlog（新发现的问题被静默丢弃，正是『依赖人工催促』的来源）。
    """
    os.makedirs(os.path.dirname(BACKLOG), exist_ok=True)
    known = set()
    if os.path.exists(BACKLOG):
        with open(BACKLOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    known.add(json.loads(line).get("id"))
                except Exception:
                    continue
    added = [r for r in SEED if r.get("id") not in known]
    if not added:
        return 0
    with open(BACKLOG, "a", encoding="utf-8") as f:
        for r in added:
            rec = dict(r)
            rec["created"] = _ts()
            rec["updated"] = _ts()
            rec["verify"] = None
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(added)


def load_backlog():
    out = []
    if not os.path.exists(BACKLOG):
        return out
    with open(BACKLOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def save_backlog(rows):
    tmp = BACKLOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, BACKLOG)


# --------------------------------------------------------------------------- #
# 2) 回灌（仅安全可自动化项）
# --------------------------------------------------------------------------- #
def apply_selfheal_preflight(rec):
    """确保 tpoint 外备份为最新 + preflight 守卫存在。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    actions = []
    for mod, path, bak in CRITICAL_SCRIPTS:
        bak_path = os.path.join(BACKUP_DIR, bak)
        if os.path.exists(path):
            # 同步最新内容到备份（若备份缺失或不同）
            if not os.path.exists(bak_path) or open(path, "rb").read() != open(bak_path, "rb").read():
                shutil.copyfile(path, bak_path)
                actions.append(f"backup-synced:{mod}")
        elif os.path.exists(bak_path):
            shutil.copyfile(bak_path, path)
            actions.append(f"restored:{mod}")
    preflight = os.path.join(ROOT, "scripts", "pipeline_preflight.py")
    actions.append("preflight-present" if os.path.exists(preflight) else "preflight-MISSING")
    rec["verify"] = {"applied_actions": actions, "at": _ts()}
    return actions


APPLIERS = {"selfheal_preflight": apply_selfheal_preflight, "preflight_cover": apply_selfheal_preflight}


# --------------------------------------------------------------------------- #
# 3) 再验证
# --------------------------------------------------------------------------- #
def run_preflight():
    code = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "pipeline_preflight.py")],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return code.returncode, (code.stdout + code.stderr).strip()[-300:]


def run_verify_step(date):
    """实跑 live_roundtrip_review 作为流水线回归验证（仅--date，幂等，重生成 live_review JSON）。"""
    code = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "live_roundtrip_review.py"), "--date", date],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
             "PYTHONPATH": f"{ROOT}\\venv\\Lib\\site-packages;{ROOT}\\venv\\Lib;{ROOT}"},
    )
    return code.returncode, (code.stdout + code.stderr).strip()[-300:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    seeded = seed_backlog()
    rows = load_backlog()

    high = [r for r in rows if r.get("severity") in ("P0", "P1") and r.get("status") == "open"]
    applied_ids, applied_actions = [], []
    for r in high:
        fid = r.get("fix_id")
        if r.get("automatable") and fid in APPLIERS:
            acts = APPLIERS[fid](r)
            r["status"] = "applied"
            r["updated"] = _ts()
            applied_ids.append(r["id"])
            applied_actions.extend(acts)

    # 再验证（持续监控，无论是否有 apply 都跑）
    pf_rc, pf_out = run_preflight()
    vr_rc, vr_out = run_verify_step(args.date)
    verify = {
        "preflight_rc": pf_rc, "preflight_out": pf_out,
        "verify_step_rc": vr_rc, "verify_step_out": vr_out,
        "at": _ts(),
    }
    # 若再验证通过，且该项已 applied，则置 verified
    if pf_rc == 0 and vr_rc == 0:
        for r in rows:
            if r.get("status") == "applied":
                r["status"] = "verified"
                r["updated"] = _ts()
    save_backlog(rows)

    # 报告（仅在「有回灌动作 / 仍有未决高价值项 / 再验证异常」时推，避免无谓打扰）
    open_high = [r for r in rows if r.get("severity") in ("P0", "P1") and r.get("status") in ("open", "applied")]
    if applied_ids or open_high or pf_rc != 0 or vr_rc != 0:
        lines = [f"🔁 [tpoint 自迭代闭环 {_ts()}]"]
        if applied_ids:
            lines.append(f"✅ 已回灌({len(applied_ids)}): " + ", ".join(applied_ids))
            lines.append("   动作: " + "; ".join(applied_actions))
        lines.append(f"🔍 再验证: preflight_rc={pf_rc}, live_roundtrip_review_rc={vr_rc}")
        if open_high:
            lines.append(f"📋 未决高价值项({len(open_high)}):")
            for r in open_high:
                lines.append(f"   [{r['severity']}] {r['id']}: {r['title']} → {r['proposed_fix'][:60]}...")
        if pf_rc != 0 or vr_rc != 0:
            lines.append("⚠️ 再验证异常，请关注！")
        resp = feishu_post(SELF_ITER_WEBHOOK, "\n".join(lines))
        print("report resp:", resp)
    else:
        print("无高价值未决项、无回灌动作、再验证通过 → 不推送")

    print(f"[feedback_loop] seeded={seeded} high={len(high)} applied={applied_ids} "
          f"pf_rc={pf_rc} vr_rc={vr_rc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
