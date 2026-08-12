# -*- coding: utf-8 -*-
"""
weekly_review.py — tpoint 周度复盘 + 自我修正（2026-08-11 补全 Request 4(c)）

定位：周期性（建议周五 15:50 由独立 automation 驱动）聚合一周自迭代结果，做三件事：
  1) 目标达成度：对照 iteration_state 的 R0 目标（WR_prod_exec(roll20)>=目标% 且 gap<=阈值pp），
     用最新 roll20 / reconcile 评估是否达标。
  2) 自我修正（收敛）：用本周最新 factor_opt 重跑 best_cell，对「仍在监控配置中」的标的自动收敛到
     当前数据下 total_ret 更优的单元（复用 auto_tune **全套**护栏：total_ret优先+wr不降+7日防抖
     +样本外OOS+反翻烧饼）；
     若某已落盘参数在新数据下 total_ret 反低于其优化基线 → 自动回滚到变更前值（防过拟合漂移）。

     ⚠️ 2026-08-11 晚修补（护栏一致性漏洞）：本模块原先直接调 at.best_cell(grid, baseline, param)
     即落盘，既不走 auto_tune 新增的第6条（OOS 样本外检验）也不走第7条（反翻烧饼严格 wr 不降）。
     后果是每日 auto_tune 拒绝的过拟合候选，会在周五被 weekly_review 这条旁路重新写进生产配置
     —— 两套口径漂移，正是本文件开篇「单一真相源」承诺要避免的事。现已对齐。
  3) 回归告警：若某标的实盘 WR 跌破下限且窗口内改过参数 → 告警（实盘稀疏时只告警，不 blind 回滚）。

落盘：改动写入 data/monitor_config.json（与 daily auto_tune 同一文件，统一回滚源）；
      每次动作记入 data/auto_tune_state.json（action=applied / rolled_back）。
推送：a35d7f52 自迭代群（周报）。
"""
import os, sys, json, re, datetime, argparse

# 复用 auto_tune 的护栏与落盘助手（单一真相源，避免两套口径漂移）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_tune as at

# 样本外检验器（第6条护栏）。守卫式导入：缺失时不静默放行，而是把收敛整体降级为「不出手」。
try:
    import oos_validate as OV
except Exception as _e:          # pragma: no cover
    OV = None
    _OV_ERR = str(_e)

ROOT = at.ROOT
OUT = at.OUT
CFG = at.CFG
STATE = at.STATE
HOOK = at.HOOK

WR_FLOOR = 50.0   # 实盘 WR 跌破此值视为回归（结合窗口内改参 → 告警）


def load_targets():
    """从 iteration_state.phase_goal 解析目标；缺省 55% / 1pp。"""
    wr_t, gap_t = 55.0, 1.0
    p = os.path.join(ROOT, "data", "iteration_state.json")
    if os.path.exists(p):
        try:
            s = json.load(open(p, encoding="utf-8"))
            goal = s.get("phase_goal", "")
            mw = re.search(r">=(\d+(?:\.\d+)?)\s*%", goal)
            mg = re.search(r"<=(\d+(?:\.\d+)?)\s*pp", goal)
            if mw:
                wr_t = float(mw.group(1))
            if mg:
                gap_t = float(mg.group(1))
        except Exception:
            pass
    return wr_t, gap_t


def latest_factor_opt(window_start, today):
    best = None
    if not os.path.isdir(OUT):
        return None
    for fn in os.listdir(OUT):
        if not fn.startswith("factor_opt_") or not fn.endswith(".json"):
            continue
        try:
            d = fn[len("factor_opt_"):-len(".json")]
            dt = datetime.date.fromisoformat(d)
        except Exception:
            continue
        if dt > today:
            continue
        if best is None or dt > best[0]:
            best = (dt, os.path.join(OUT, fn))
    return best[1] if best else None


def window_dates(today):
    """周五→本周一..今日；其余→近 7 日（幂等、稳健）。"""
    if today.weekday() == 4:  # Friday
        mon = today - datetime.timedelta(days=today.weekday())
        return mon, today
    return today - datetime.timedelta(days=6), today


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", dest="no_push", action="store_true",
                    help="只算不推（自检/回归验证用，避免污染自迭代群）")
    a = ap.parse_args()
    today = datetime.date.fromisoformat(a.date)
    wstart, wend = window_dates(today)
    wr_target, gap_thr = load_targets()

    cfg = at.load_json(CFG) or {}
    state = at.load_json(STATE) or {"history": []}
    opt_path = latest_factor_opt(wstart, today)
    opt = at.load_json(opt_path) if opt_path else None

    lines = [f"📅 [tpoint 周度复盘+自我修正 {a.date}]"
             f"{'(DRY-RUN 未落盘)' if a.dry_run else ''}",
             f"窗口: {wstart}..{wend}｜目标 WR>={wr_target}% gap<={gap_thr}pp"]

    # ---- 1) 目标达成度 ----
    istate = at.load_json(os.path.join(ROOT, "data", "iteration_state.json")) or {}
    roll = istate.get("roll20", {})
    wr_pe = roll.get("wr_prod_exec")
    wr_rc = roll.get("wr_recalc")
    gap = roll.get("g1_pp")
    goal_ok = (wr_pe is not None and wr_pe >= wr_target
               and gap is not None and abs(gap) <= gap_thr)
    lines.append(f"■ 目标达成（roll20）：WR_prod_exec={wr_pe} WR_recalc={wr_rc} gap={gap}pp → "
                 f"{'✅达标' if goal_ok else '⏳未达标/累积中'}")

    # ---- 2) 自我修正：回滚 + 收敛 ----
    changes, rejected, oos_cache = [], [], {}
    if opt:
        for sym, symrep in opt.get("symbols", {}).items():
            if "error" in symrep or "baseline" not in symrep or sym not in cfg:
                continue  # 仅处理监控内标的（与 auto_tune 同款安全护栏）
            baseline = symrep["baseline"]

            # (a) 回滚检查：窗口内已落盘参数在新数据下 total_ret 反劣于基线 → 回滚
            for h in state.get("history", [])[::-1]:
                if h.get("sym") == sym and h.get("action") == "applied":
                    param = h["param"]
                    grid = symrep.get(f"{param}_grid")
                    if grid and param == "trail":
                        key = "/".join(str(x) for x in h["new"])
                        cell = grid.get(key)
                        if cell and cell.get("total_ret", 0) < baseline.get("total_ret", 0):
                            old_val = h["old"]
                            if not a.dry_run:
                                at.set_param(cfg, sym, param, "/".join(str(x) for x in old_val))
                                state.setdefault("history", []).append({
                                    "date": a.date, "sym": sym, "param": param,
                                    "old": h["new"], "new": old_val,
                                    "action": "rolled_back",
                                    "reason": "weekly_review: 已落盘值在新数据下 total_ret 反劣于基线",
                                })
                            changes.append({"kind": "rolled_back", "sym": sym, "param": param,
                                            "old": h["new"], "new": old_val})
                    break

            # (b) 收敛：重跑 best_cell，自动趋近当前数据下最优单元
            for param in ("trail", "atr_min_pct"):
                grid = symrep.get(f"{param}_grid")
                if not grid:
                    continue
                # 第7条护栏（反翻烧饼）：该标的该参数历史曾主动回退 → wr 严格不降
                rb = at.was_rolled_back(cfg, state, sym, param)
                wr_tol = at.WR_TOL_STRICT if rb else at.WR_TOL
                pick = at.best_cell(grid, baseline, param, wr_tol)
                if not pick:
                    if rb:
                        rejected.append({"sym": sym, "param": param, "value": None,
                                         "verdict": "STRICT_WR", "reason": f"无候选通过严格 wr 不降（{rb}）"})
                    continue
                val, m = pick
                cur = at.cur_param(cfg, sym, param)
                new_key = at.parse_trail(val) if param == "trail" else (float(val),)
                cur_key = tuple(cur) if param == "trail" else (cur,)
                if cur_key == new_key:
                    continue
                rc = at.recent_change(state, sym, param, a.date)
                need = at.RET_MIN_IMPROVE if rc is None else at.RET_MIN_IMPROVE * 2
                d_ret = round(m.get("total_ret", 0) - baseline.get("total_ret", 0), 2)
                if d_ret <= need:
                    continue
                # 第6条护栏（防过拟合）：周度收敛与每日 auto_tune 同纪律，必须过样本外检验。
                # 未 PASS（含 INCONCLUSIVE 样本不足、检验异常、检验器缺失）一律不出手。
                oos = None
                if at.OOS_ENABLE:
                    if OV is None:
                        rejected.append({"sym": sym, "param": param, "value": val,
                                         "verdict": "NO_VALIDATOR",
                                         "reason": f"oos_validate 不可用({_OV_ERR})→保守不出手"})
                        continue
                    try:
                        oos = OV.validate_one(sym, param, val, at.OOS_SPLIT, oos_cache)
                    except Exception as e:
                        rejected.append({"sym": sym, "param": param, "value": val,
                                         "verdict": "ERROR", "reason": f"OOS 检验异常：{e}"})
                        continue
                    dwr_oos = oos.get("d_wr_oos")
                    if oos.get("verdict") == "PASS" and dwr_oos is not None and dwr_oos < -wr_tol:
                        oos["verdict"] = "FAIL"
                        oos["reason"] = (f"OOS wr Δ{dwr_oos}pp 违反本标的 wr 容差 -{wr_tol}pp"
                                         + (f"（严格档：{rb}）" if rb else ""))
                    if oos.get("verdict") != "PASS":
                        rejected.append({"sym": sym, "param": param, "value": val,
                                         "verdict": oos.get("verdict"),
                                         "reason": oos.get("reason") or oos.get("error")})
                        continue
                oos_brief = ({"verdict": oos.get("verdict"), "d_ret_oos": oos.get("d_ret_oos"),
                              "d_wr_oos": oos.get("d_wr_oos"),
                              "n_oos": (oos.get("oos_cand") or {}).get("n")} if oos else None)
                if not a.dry_run:
                    at.set_param(cfg, sym, param, val)
                    state.setdefault("history", []).append({
                        "date": a.date, "sym": sym, "param": param,
                        "old": list(cur_key), "new": list(new_key),
                        "d_ret": d_ret, "d_wr": round(m.get("win_rate", 0) - baseline.get("win_rate", 0), 2),
                        "n": m.get("n"), "baseline_wr": baseline.get("win_rate"),
                        "baseline_ret": baseline.get("total_ret"), "action": "applied",
                        "src": "weekly_review", "oos": oos_brief, "strict_wr": bool(rb),
                    })
                changes.append({"kind": "applied", "sym": sym, "param": param,
                                "old": list(cur_key), "new": list(new_key), "d_ret": d_ret,
                                "oos": oos_brief, "strict_wr": bool(rb)})

    if changes:
        json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        lines.append(f"■ 自我修正（{len(changes)} 项，次日开盘生效，可回滚）：")
        for c in changes:
            tag = "↩️回滚" if c["kind"] == "rolled_back" else "✅收敛"
            extra = f" (Δ{c['d_ret']}pp)" if c.get("d_ret") is not None else ""
            o = c.get("oos") or {}
            oos_txt = (f"；OOS {o.get('verdict')} Δret{o.get('d_ret_oos')}pp/"
                       f"Δwr{o.get('d_wr_oos')}pp n={o.get('n_oos')}" if o else "")
            strict = "［严格wr］" if c.get("strict_wr") else ""
            lines.append(f"  {tag} {c['sym']} {c['param']}: {c['old']}→{c['new']}{extra}{oos_txt}{strict}")
    else:
        lines.append("■ 自我修正：本周无需变更（已收敛至当前数据最优 / 护栏未过 / 防抖期内）。")

    if rejected:
        lines.append(f"■ 被护栏拒绝 {len(rejected)} 项（全样本网格最优但样本外/反翻烧饼不成立）：")
        for r in rejected:
            v = f"={r['value']}" if r.get("value") else ""
            lines.append(f"  ❌ {r['sym']} {r['param']}{v}：{r['verdict']} {r['reason']}")

    lines.append("口径：目标取自 iteration_state.phase_goal；修正复用 auto_tune 全套护栏"
                 f"(total_ret优先+wr不降≤{at.WR_TOL}pp+7日防抖+IS/OOS {at.OOS_SPLIT:.0%}/"
                 f"{1-at.OOS_SPLIT:.0%}样本外检验+反翻烧饼严格wr)。"
                 "明细 output/factor_opt_*.json / output/oos_validate_*.json / data/auto_tune_state.json")
    if not a.no_push:
        at.push("\n".join(lines))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
