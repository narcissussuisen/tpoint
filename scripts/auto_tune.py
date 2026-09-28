# -*- coding: utf-8 -*-
"""
auto_tune.py — 报告驱动的自动调参寻优（2026-08-11 补全；2026-09-28 RSI 治理改 propose-only）

定位：把每日报告（factor_opt_*.json 寻优报告 + live_review/reconcile 实盘报告）转化为
**达标改动提案**。⚠️ 2026-09-28 起 **propose-only**：不再直写 monitor_config.json
（配置变更唯一通道 = daily_agent.apply 三重闸门 / effect_ledger——docs/rsi_loop_agenda.md §三；
trail/atr_min_pct 不在 AI 自动合入白名单 ⇒ 提案交人审/闸门裁决）。

护栏（沿用 2026-08-05 deploy_optimal 纪律：total_ret 优先 + wr 不降）：
  1) 仅采纳样本充足网格单元：n >= MIN_TRIPS；
  2) wr 不降：候选单元格 win_rate >= baseline_wr - WR_TOL；
  3) total_ret 优先：在 2) 过滤后取 total_ret 最大者；要求较基线改善 > RET_MIN_IMPROVE；
  4) 拒绝「wr 虚胖」：因子_opt 的 win_rate 推荐若 total_ret 恶化，本脚本会自动否决
     （如 161129 0.5/0.5 wr+7.4pp 但 ret -2.13→-3.32，将被拒）。
  5) 防抖：同一标的若近 ROLLBACK_DAYS 内已被本脚本改过且未回滚，需更强改善才再改（避免日级抖动）。

落盘：data/ai_proposals/auto_tune_<date>.json（提案）+ data/auto_tune_state.json
（action=proposed，含 old/new + 基线指标），供人审/RSI 闸门裁决与审计。

CLI：python scripts/auto_tune.py --date 2026-08-11 [--dry-run]
（--dry-run 语义弱化为兼容保留：propose-only 模式下两者等价，均不触碰 monitor_config。）

[T1.5 2026-09-27] 本脚本不做模拟：网格指标来自 factor_opt_*.json、OOS 复核来自
oos_validate.validate_one，两者的评估口径均已切 simulate_position_sm 单一仓位状态机
（反T 被建模）。position_model 随 state 落盘以便审计。
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# [2026-09-25] 交易日历单一真源（core/trading_calendar.py）。
# 本脚本此前**完全没有交易日判断**，而它会真实改写 data/monitor_config.json ——
# 若在休市日被 run_daily_review.bat 调用，就会用「当天无信号」的空样本去调生产参数。
# 用户口径：优化必须基于有效样本，误改参数不可逆 ⇒ 这里必须硬拦。
sys.path.insert(0, os.path.join(ROOT, "core"))
from trading_calendar import is_trading_day as _is_trading_day  # noqa: E402
OUT = os.path.join(ROOT, "output")
CFG = os.path.join(ROOT, "data", "monitor_config.json")
STATE = os.path.join(ROOT, "data", "auto_tune_state.json")
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d"

MIN_TRIPS = 50          # 网格单元最小样本
# 2026-08-11 晚修正：原设 2.0pp 属擅自放宽用户 2026-08-05 已确立的「wr 不降」硬约束，
# 导致把 08-05 曾因 wr -1.2pp 主动回退的 513310 0.3/0.5 又改了回去（翻烧饼）。
# 收紧为 0.5pp —— 仅容纳数值噪音，实质等价于「wr 不降」。
WR_TOL = 0.5            # wr 允许下降上限(pp)
WR_TOL_STRICT = 0.0     # 对「历史曾被回退」的标的施加严格不降
RET_MIN_IMPROVE = 0.2   # total_ret 至少改善(pp)才出手
ROLLBACK_DAYS = 7       # 防抖窗口：窗口内已改过则需更强理由
OOS_ENABLE = True       # 第6条护栏：样本外(OOS)防过拟合检验，未 PASS 不得写生产
OOS_SPLIT = 0.7         # IS/OOS 时间切分比例


def push(text):
    try:
        import urllib.request
        req = urllib.request.Request(HOOK, data=json.dumps(
            {"msg_type": "text", "content": {"text": text}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"POST_FAIL:{e}"


def load_json(p):
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def parse_trail(val):
    a, b = (float(x) for x in str(val).split("/"))
    return a, b


def was_rolled_back(cfg, state, sym, param):
    """该标的该参数是否有「历史主动回退」记录 → 施加严格 wr 不降。

    证据来源两处：
      1) monitor_config.json 的 _trail_note/_note 文本（历史回退由 deploy_optimal.py 等写入，
         **不在** auto_tune_state.json 里，auto_tune 原先看不到 → 08-11 翻烧饼根因）；
      2) auto_tune_state.json 中 action=rolled_back 的记录。
    """
    note_keys = ("_trail_note", "_note", "comment") if param == "trail" else ("_note", "comment")
    blob = " ".join(str(cfg.get(sym, {}).get(k, "")) for k in note_keys)
    if ("回退" in blob or "撤回" in blob) and ("wr" in blob or "胜率" in blob):
        return f"monitor_config 备注载明历史回退：{blob[:80]}"
    for h in state.get("history", []):
        if h.get("sym") == sym and h.get("param") == param and h.get("action") == "rolled_back":
            return f"auto_tune_state 载明 {h.get('date')} 曾回滚"
    return None


def best_cell(grid, baseline, param, wr_tol=WR_TOL):
    """在网格中选 total_ret 最大且 wr 不降的单元；无改善返回 None。"""
    bwr, bret = baseline.get("win_rate"), baseline.get("total_ret")
    if bwr is None or bret is None:
        return None
    cands = []
    for val, m in grid.items():
        if not isinstance(m, dict):
            continue
        if m.get("n", 0) < MIN_TRIPS:
            continue
        if m.get("win_rate", 0) < bwr - wr_tol:
            continue
        cands.append((val, m))
    if not cands:
        return None
    best_val, best_m = max(cands, key=lambda x: x[1].get("total_ret", -1e9))
    if best_m.get("total_ret", -1e9) <= bret + RET_MIN_IMPROVE:
        return None
    return best_val, best_m


def cur_param(cfg, sym, param):
    if param == "trail":
        return (cfg.get(sym, {}).get("trail_activate_pct"),
                cfg.get(sym, {}).get("trail_pct"))
    return cfg.get(sym, {}).get(param)


def set_param(cfg, sym, param, val):
    cfg.setdefault(sym, {})
    if param == "trail":
        a, b = parse_trail(val)
        cfg[sym]["trail_activate_pct"] = a
        cfg[sym]["trail_pct"] = b
        return (a, b)
    cfg[sym][param] = val
    return val


def recent_change(state, sym, param, today):
    """防抖：窗口内同标的同参数已改过且未回滚，返回该记录。"""
    try:
        d0 = datetime.date.fromisoformat(today)
    except Exception:
        return None
    for h in state.get("history", [])[::-1]:
        if h.get("sym") == sym and h.get("param") == param and h.get("action") == "applied":
            try:
                dd = datetime.date.fromisoformat(h["date"])
            except Exception:
                continue
            if (d0 - dd).days <= ROLLBACK_DAYS:
                return h
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--dry-run", action="store_true", help="只计算不落盘")
    a = ap.parse_args()
    date = a.date

    # [2026-09-25] 非交易日不调参：本步会真实改写 monitor_config.json，误跑不可逆。
    if not _is_trading_day(date):
        print(f"[auto_tune] {date} 非交易日，跳过（不读不写 monitor_config.json）")
        return

    opt = load_json(os.path.join(OUT, f"factor_opt_{date}.json"))
    if opt is None:
        print(f"[auto_tune] factor_opt_{date}.json 缺失，跳过")
        return
    # [T1.5] 寻优报告的评估口径标识（旧报告缺省视为 simulate_day 纯多头口径，
    # 仅记录不拦截——是否基于旧口径报告出手由人工/weekly_review 判断）
    position_model = opt.get("position_model", "legacy(simulate_day)")
    if position_model != "simulate_position_sm-v1":
        print(f"[auto_tune] ⚠️ factor_opt_{date}.json position_model={position_model}，"
              f"非 simulate_position_sm-v1 口径（旧纯多头网格，反T 未建模），继续但落盘标记")
    cfg = load_json(CFG)
    if cfg is None:
        print("[auto_tune] monitor_config.json 缺失，中止")
        sys.exit(1)
    state = load_json(STATE) or {"history": []}

    changes, rejected, oos_cache = [], [], {}
    for sym, symrep in opt.get("symbols", {}).items():
        if "error" in symrep or "baseline" not in symrep:
            continue
        # 安全护栏：仅改写「已在 monitor_config.json 中」的标的；绝不自动新增监控项
        # （新增会导致 live monitor 缺 mpr_enable/atr_min_pct/vol_confirm 等必填字段而崩溃）。
        # 08-11 实测：factor_opt 含 300757.SZ 但 monitor_config 无该标的 → 旧逻辑会写残缺 trail 项，
        # 现已拦截（watchlist/配置不一致问题交由 weekly_review 诊断告警，不在此静默补全）。
        if sym not in cfg:
            print(f"[auto_tune] 跳过 {sym}：不在 monitor_config.json（watchlist/配置不一致），"
                  f"不自动新增监控项以免 live monitor 缺字段崩溃")
            continue
        baseline = symrep["baseline"]
        for param in ("trail", "atr_min_pct"):
            grid = symrep.get(f"{param}_grid")
            if not grid:
                continue
            # 第7条护栏（反翻烧饼）：历史曾主动回退过 → wr 严格不降
            rb = was_rolled_back(cfg, state, sym, param)
            wr_tol = WR_TOL_STRICT if rb else WR_TOL
            pick = best_cell(grid, baseline, param, wr_tol)
            if not pick:
                if rb:
                    print(f"[auto_tune] {sym} {param}: 无候选通过严格 wr 不降（{rb}）")
                continue
            val, m = pick
            cur = cur_param(cfg, sym, param)
            new = parse_trail(val) if param == "trail" else (float(val),)
            # 比较（trail 为二元组，atr 为单值）
            cur_key = tuple(cur) if param == "trail" else (cur,)
            new_key = new if param == "trail" else (float(val),)
            if cur_key == new_key:
                continue
            d_ret = round(m.get("total_ret", 0) - baseline.get("total_ret", 0), 2)
            d_wr = round(m.get("win_rate", 0) - baseline.get("win_rate", 0), 2)
            # 防抖：近窗已改过 → 要求更显著改善（>2倍阈值）才再改
            rc = recent_change(state, sym, param, date)
            need = RET_MIN_IMPROVE if rc is None else RET_MIN_IMPROVE * 2
            if d_ret <= need:
                continue
            # 第6条护栏（防过拟合）：全样本网格最优 ≠ 真实规律，必须过样本外(OOS)检验。
            # 未 PASS（含样本不足 INCONCLUSIVE）一律不出手 —— 宁可不改，不可拿噪音上生产。
            oos = None
            if OOS_ENABLE:
                try:
                    import oos_validate as OV
                    oos = OV.validate_one(sym, param, val, OOS_SPLIT, oos_cache)
                except Exception as e:
                    print(f"[auto_tune] {sym} {param}: OOS 检验异常 → 保守跳过（{e}）")
                    continue
                # OOS 段的 wr 也必须满足本标的当前 wr 容差（历史回退过的标的=严格不降），
                # 避免 oos_validate 自身容差与 auto_tune 纪律脱钩。
                dwr_oos = oos.get("d_wr_oos")
                if oos.get("verdict") == "PASS" and dwr_oos is not None and dwr_oos < -wr_tol:
                    oos["verdict"] = "FAIL"
                    oos["reason"] = (f"OOS wr Δ{dwr_oos}pp 违反本标的 wr 容差 -{wr_tol}pp"
                                     + (f"（严格档：{rb}）" if rb else ""))
                if oos.get("verdict") != "PASS":
                    print(f"[auto_tune] {sym} {param}={val} 被 OOS 护栏拒绝："
                          f"{oos.get('verdict')} {oos.get('reason') or oos.get('error')}")
                    rejected.append({"sym": sym, "param": param, "value": val,
                                     "verdict": oos.get("verdict"),
                                     "reason": oos.get("reason") or oos.get("error")})
                    continue
            old_disp = list(cur_key)
            changes.append({
                "sym": sym, "param": param, "old": old_disp, "new": list(new_key),
                "d_ret": d_ret, "d_wr": d_wr, "n": m.get("n"),
                "baseline_wr": baseline.get("win_rate"),
                "baseline_ret": baseline.get("total_ret"),
                "chosen_val": val,
                "oos": ({"verdict": oos.get("verdict"), "d_ret_oos": oos.get("d_ret_oos"),
                         "d_wr_oos": oos.get("d_wr_oos"), "n_oos": oos.get("oos_cand", {}).get("n")}
                        if oos else None),
                "strict_wr": bool(rb),
            })

    # ---- 落盘（2026-09-28 RSI 治理：propose-only，不再直写 monitor_config） ----
    # ⛔ 历史行为（直写 monitor_config.json）已移除：与「配置变更唯一通道 =
    # daily_agent.apply（三重闸门）/ effect_ledger」冲突（docs/rsi_loop_agenda.md §三）。
    # 达标改动现在只写提案：data/ai_proposals/auto_tune_<date>.json，
    # 由 RSI 日循环/人审按闸门裁决（trail/atr 均不在 AUTO_MERGE_PARAMS 白名单 ⇒ 人审）。
    proposal_fp = os.path.join(ROOT, "data", "ai_proposals", f"auto_tune_{date}.json")
    if changes:
        os.makedirs(os.path.dirname(proposal_fp), exist_ok=True)
        prop = {
            "date": date, "source": "auto_tune(propose-only)",
            "position_model": position_model,
            "note": "auto_tune 2026-09-28 起 propose-only：以下达标改动不再自动改写 "
                    "monitor_config（唯一通道=daily_agent apply/effect_ledger）；"
                    "trail/atr_min_pct 不在 AI 自动合入白名单 ⇒ 人审裁决。",
            "changes": changes, "rejected": rejected,
        }
        with open(proposal_fp, "w", encoding="utf-8") as f:
            json.dump(prop, f, ensure_ascii=False, indent=2)
        for c in changes:
            state.setdefault("history", []).append({
                "date": date, "sym": c["sym"], "param": c["param"],
                "old": c["old"], "new": c["new"], "d_ret": c["d_ret"],
                "d_wr": c["d_wr"], "n": c["n"],
                "baseline_wr": c["baseline_wr"], "baseline_ret": c["baseline_ret"],
                "position_model": position_model,
                "action": "proposed", "oos": c.get("oos"), "strict_wr": c.get("strict_wr"),
            })
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # 推送摘要
    lines = [f"🤖 [tpoint 自动调参 auto_tune {date}]（propose-only，不直写 monitor_config）"
             f"｜护栏:total_ret优先+wr不降(≤{WR_TOL}pp)+样本外OOS+反翻烧饼"]
    if changes:
        lines.append(f"■ 达标改动 {len(changes)} 项 → 提案 {proposal_fp}（人审/闸门裁决，trail/atr 不在自动白名单）：")
        for c in changes:
            o = c.get("oos") or {}
            lines.append(f"· {c['sym']} {c['param']}: {c['old']}→{c['new']} "
                         f"(ret {c['baseline_ret']}→{round(c['baseline_ret']+c['d_ret'],2)} "
                         f"Δ{c['d_ret']}pp, wr Δ{c['d_wr']}pp, n={c['n']}"
                         + (f"；OOS {o.get('verdict')} Δret{o.get('d_ret_oos')}pp/"
                            f"Δwr{o.get('d_wr_oos')}pp n={o.get('n_oos')}" if o else "") + ")")
    else:
        lines.append("■ 今日无达标自动调参（护栏未通过：或样本不足、或 total_ret 未改善/ wr 恶化、或未过样本外检验）。")
    if rejected:
        lines.append(f"■ 被护栏拒绝 {len(rejected)} 项（全样本最优但样本外不成立=疑似过拟合）：")
        for r in rejected:
            lines.append(f"· {r['sym']} {r['param']}={r['value']}：{r['verdict']} {r['reason']}")
    lines.append(f"口径：F盘全历史网格选 total_ret 最大且 wr 不降单元 → 再过 IS/OOS "
                 f"{OOS_SPLIT:.0%}/{1-OOS_SPLIT:.0%} 样本外复核；拒绝 wr 虚胖；历史回退过的参数施加严格 wr 不降。"
                 f"评估口径 {position_model}（单一仓位状态机，反T 已建模）。"
                 f"明细 output/factor_opt_{date}.json + oos_validate_*.json")
    push("\n".join(lines))
    print("\n".join(lines[:6]))


if __name__ == "__main__":
    main()
