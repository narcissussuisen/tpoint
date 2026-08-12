# -*- coding: utf-8 -*-
"""test_tune_guardrails.py — 自动调参「护栏一致性」回归测试（2026-08-11 新增）

## 为什么有这个测试
2026-08-11 晚给 auto_tune.py 加了两条护栏：
  第6条 样本外(OOS)防过拟合检验 —— 未 PASS 不得写生产；
  第7条 反翻烧饼 —— 历史曾主动回退过的参数施加严格 wr 不降(WR_TOL_STRICT=0)。
但 weekly_review.py 的**周度收敛路径**当时仍在直接调 at.best_cell(grid, baseline, param)
就落盘，两条新护栏一条都不走。后果是同一个过拟合候选：周一到周四被 auto_tune 拒绝，
周五被 weekly_review 从旁路放行写进 monitor_config.json —— 「单一真相源」承诺失效。

本测试把两条路径的护栏行为锁死在一起。任何一侧未来再加/改护栏而另一侧没跟上，这里会红。

## 跑法
  venv/Scripts/python.exe tests/test_tune_guardrails.py
全部通过 exit 0；任一失败 exit 1 并打印明细。不触碰真实 monitor_config / auto_tune_state
（全部重定向到 tempdir），不发飞书（at.push 已打桩）。
"""
import os, sys, json, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import auto_tune as at          # noqa: E402
import weekly_review as wr      # noqa: E402
import oos_validate as OV       # noqa: E402

DATE = "2026-08-11"
SYM = "TEST9.SH"

# 基线 vs 网格：候选 0.3/0.5 的 total_ret 大幅优于基线(+5pp)、wr 不降 → 只可能被 OOS/严格wr 拦下。
BASE = {"win_rate": 50.0, "total_ret": 1.00, "n": 80}
GRID_CLEAN = {
    "0.4/0.6": {"win_rate": 50.0, "total_ret": 1.00, "n": 80},   # = 当前生产值
    "0.3/0.5": {"win_rate": 51.0, "total_ret": 6.00, "n": 80},   # 候选
}
# wr 微降 0.2pp：宽松档(WR_TOL=0.5)放行、严格档(WR_TOL_STRICT=0)必须拦住。
GRID_WRDIP = {
    "0.4/0.6": {"win_rate": 50.0, "total_ret": 1.00, "n": 80},
    "0.3/0.5": {"win_rate": 49.8, "total_ret": 6.00, "n": 80},
}

FAILS = []


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def fake_oos(verdict, d_wr=1.0, d_ret=3.0, n=40):
    def _f(sym, param, value, split=0.7, cache=None):
        return {"sym": sym, "param": param, "value": value, "verdict": verdict,
                "reason": f"stub {verdict}", "d_ret_oos": d_ret, "d_wr_oos": d_wr,
                "oos_cand": {"n": n}}
    return _f


def setup(tmp, grid, state):
    """把两个模块的落盘路径与报告目录全部重定向到 tempdir。"""
    cfg = {SYM: {"trail_activate_pct": 0.4, "trail_pct": 0.6,
                 "atr_min_pct": 0.25, "mpr_enable": True}}
    cfgp = os.path.join(tmp, "monitor_config.json")
    stp = os.path.join(tmp, "auto_tune_state.json")
    json.dump(cfg, open(cfgp, "w", encoding="utf-8"))
    json.dump(state, open(stp, "w", encoding="utf-8"))
    json.dump({"date": DATE, "symbols": {SYM: {"baseline": BASE, "trail_grid": grid}}},
              open(os.path.join(tmp, f"factor_opt_{DATE}.json"), "w", encoding="utf-8"))
    at.CFG = wr.CFG = cfgp
    at.STATE = wr.STATE = stp
    at.OUT = wr.OUT = tmp
    return cfgp, stp


def trail_of(cfgp):
    c = json.load(open(cfgp, encoding="utf-8"))
    return (c[SYM]["trail_activate_pct"], c[SYM]["trail_pct"])


def run(mod, argv):
    old = sys.argv
    sys.argv = argv
    try:
        mod.main()
    finally:
        sys.argv = old


def case(label, module, grid, state, verdict, d_wr=1.0):
    """跑一个场景，返回落盘后的 trail 元组。"""
    tmp = tempfile.mkdtemp(prefix="tpoint_guardrail_")
    try:
        cfgp, _ = setup(tmp, grid, state)
        OV.validate_one = fake_oos(verdict, d_wr=d_wr)
        if module is at:
            run(at, ["auto_tune.py", "--date", DATE])
        else:
            run(wr, ["weekly_review.py", "--date", DATE, "--no-push"])
        return trail_of(cfgp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    at.push = lambda t: "STUB_OK"          # 两个模块都走 at.push
    orig_validate = OV.validate_one
    empty = {"history": []}
    rolled = {"history": [{"date": "2026-08-05", "sym": SYM, "param": "trail",
                           "old": [0.3, 0.5], "new": [0.4, 0.6], "action": "rolled_back",
                           "reason": "历史主动回退（wr 恶化）"}]}
    try:
        print("\n== 第6条护栏：样本外(OOS) —— 两条路径都必须拦住 FAIL 候选 ==")
        check("auto_tune  OOS=FAIL 不落盘",
              case("A", at, GRID_CLEAN, empty, "FAIL") == (0.4, 0.6), "配置被改写了")
        check("auto_tune  OOS=PASS 正常收敛",
              case("B", at, GRID_CLEAN, empty, "PASS") == (0.3, 0.5), "PASS 却没落盘")
        check("weekly_rev OOS=FAIL 不落盘（★本次修补的旁路漏洞）",
              case("C", wr, GRID_CLEAN, empty, "FAIL") == (0.4, 0.6),
              "周度收敛绕过了 OOS 护栏，过拟合候选被写进生产配置")
        check("weekly_rev OOS=PASS 正常收敛",
              case("D", wr, GRID_CLEAN, empty, "PASS") == (0.3, 0.5), "PASS 却没落盘")
        check("weekly_rev OOS=INCONCLUSIVE 不落盘（样本不足≠通过）",
              case("E", wr, GRID_CLEAN, empty, "INCONCLUSIVE") == (0.4, 0.6), "样本不足被当成通过")
        check("weekly_rev OOS 段 wr 恶化时 PASS 应被改判 FAIL",
              case("F", wr, GRID_CLEAN, empty, "PASS", d_wr=-3.0) == (0.4, 0.6),
              "OOS wr 恶化仍放行")

        print("\n== 第7条护栏：反翻烧饼严格 wr 不降 —— 两条路径都必须一致 ==")
        check("auto_tune  无回退史 + wr微降0.2pp → 放行（宽松档 WR_TOL=0.5）",
              case("G", at, GRID_WRDIP, empty, "PASS") == (0.3, 0.5), "宽松档误拦")
        check("auto_tune  有回退史 + wr微降0.2pp → 拦住（严格档）",
              case("H", at, GRID_WRDIP, rolled, "PASS") == (0.4, 0.6), "严格档失效=翻烧饼")
        check("weekly_rev 无回退史 + wr微降0.2pp → 放行",
              case("I", wr, GRID_WRDIP, empty, "PASS") == (0.3, 0.5), "宽松档误拦")
        check("weekly_rev 有回退史 + wr微降0.2pp → 拦住（★旁路漏洞）",
              case("J", wr, GRID_WRDIP, rolled, "PASS") == (0.4, 0.6),
              "周度收敛绕过反翻烧饼护栏，会把用户主动回退的参数改回去")

        print("\n== 常量口径同源：auto_tune 与 oos_validate 不得各自漂移 ==")
        check("WR_TOL 一致", at.WR_TOL == orig_wr_tol_of_ov,
              f"auto_tune={at.WR_TOL} oos_validate={orig_wr_tol_of_ov}")
        check("RET_MIN_IMPROVE 一致", at.RET_MIN_IMPROVE == OV.RET_MIN_IMPROVE,
              f"auto_tune={at.RET_MIN_IMPROVE} oos_validate={OV.RET_MIN_IMPROVE}")
        check("WR_TOL_STRICT 为 0（严格不降）", at.WR_TOL_STRICT == 0.0, str(at.WR_TOL_STRICT))
        check("weekly_review 复用 auto_tune 的 OOS 开关而非自持",
              wr.at.OOS_ENABLE is at.OOS_ENABLE, "weekly_review 未复用 auto_tune 常量")
    finally:
        OV.validate_one = orig_validate

    print("\n" + "=" * 64)
    if FAILS:
        print(f"[RESULT] FAILED {len(FAILS)} 项：")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("[RESULT] ALL PASS — auto_tune / weekly_review 护栏口径一致")
    return 0


orig_wr_tol_of_ov = OV.WR_TOL

if __name__ == "__main__":
    sys.exit(main())
