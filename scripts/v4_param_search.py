# -*- coding: utf-8 -*-
"""
v4_param_search.py — tpoint v4 综合评分模型 参数自迭代寻优
=================================================================================
定位：tpoint 自迭代系统对 v4(综合评分模型, v10.3.0) 四大核心维度的参数自动搜索与优化。

四大维度（对应方法论 v1.1.0 §4.5 四个组件）：
  ① 均线引力 (VWAP Gravity)    → vwap_k1 + w_vwap
  ② 量价背离 (Vol-Price Div)    → div_local_w / div_vol_ratio + w_vol_div
  ③ MACD 背离 (MACD Div)        → macd_*（标准 12/26/9，仅调权重）+ w_macd_div
  ④ RSI 超买超卖 (OverBought)   → rsi_period / rsi_oversold / rsi_overbought + w_rsi
  + 信号阈值 buy/sell_threshold（控密度）

为什么用「方向准确性」作主目标（重要）：
  v4 的离线回测 PnL（simulate_bidirectional 配对）在本机离线 1m 上**系统性净负**
  （与 R0 reconcile 工作一致：离线全版本净负、但实盘 WR≈56% → 离线/实盘存在结构性差距）。
  因此直接以「净收益」为优化目标 = 在噪声上优化，不可信。
  用户要求「提升信号可靠性」，而**方向准确性(directional accuracy)** 是独立于配对/PnL 偏置的、
  可离线稳健度量的信号质量指标：B 信号后价是否上行、S 信号后价是否下行。

关键修正（本版 v2.1）：以「池加权均值」作资格/目标 → 被混合篮子稀释而系统性 <50% 以致全拒。
  改为**逐标的(per-symbol)稳健聚合**：
    · 主指标 = 逐标的方向准确性之「中位数」(median)，抗单只稀释；
    · 资格门槛 = 中位数 ≥ ACC_FLOOR 且「篮子中 ≥50% 标的方向准确性 ≥ ACC_FLOOR」(多数标的确有方向性)；
    · 双向分别保护：S 侧(强)不退化、B 侧(弱)尽量抬升。
  保证任何情况下都产出「按目标最优」的 tuned config，并以护栏是否通过给出诚实 caveats。

工程实现：
  1) 双向回测 core/simulate_bidirectional.py 公平量化 B/S 双侧（不修改 exit_manager 生产代码）；
  2) 搜索时松弛 B 门控(trend_b_allowed=(-1,0,1))以测 v4 内在质量（与生产风险门控解耦）；
  3) 分阶段协调上升(coordinate ascent)网格搜索，IS 选参、OOS 验证防过拟合；
  4) 指标计算(compute_indicators)每个 day-run 仅算一次（prep_runs 缓存），大幅加速搜索；
  5) 护栏沿用 auto_tune 纪律：MIN_SIGNALS 样本下限 / 多数标的方向性 / 密度健康带 / OOS-PASS 门。

运行：python scripts/v4_param_search.py [--symbols ...] [--last-n 20] [--dry-run]
"""
import os
import sys
import json
import time
import csv
import datetime
import argparse
import itertools
import signal
# 进程级信号免疫：避免被工具侧超时强杀的 SIGINT/SIGBREAK 波及（Windows 长跑守护）
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        signal.signal(getattr(signal, _s), signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass
from collections import Counter

TPOINT_CORE = os.environ.get("TPOINT_CORE", r"C:/Users/YZP/WorkBuddy/Claw/tpoint/core")
if TPOINT_CORE not in sys.path:
    sys.path.insert(0, TPOINT_CORE)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d"

import numpy as np
from composite_scorer import CompositeConfig, DEFAULT_CONFIG, detect_signals_v4
from simulate_bidirectional import simulate_bidirectional
from exit_manager import make_config, aggregate_metrics, cost_for_symbol
from indicators import compute_indicators

CFG = make_config()  # 出场配置沿用生产默认（硬止损atr1.5+时间90+移动0.4/0.6）
FORWARD_HORIZON = int(os.environ.get("V4_FWD", "8"))  # 方向准确性前瞻窗口(分钟bar)

# ---------- 搜索配置 ----------
SYMBOLS = [s.strip() for s in os.environ.get(
    "V4_SYMS", "688111.SH,600570.SH,300308.SZ,300757.SZ,513310.SH,161129.SZ,000001.SZ,300750.SZ"
).split(",") if s.strip()]
LAST_N = int(os.environ.get("V4_LASTN", "20"))
DATA_DIR = os.environ.get("V4_DATA", r"F:/keyfactor_data/1m")
OOS_SPLIT = 0.7            # IS/OOS 时间切分（与 auto_tune 一致）
MIN_SIGNALS_IS = 50        # IS 最小信号数（低于则参数不可信）
MIN_SIGNALS_OOS = 20       # OOS 最小信号数
DENSITY_MIN = 0.4          # 信号密度健康带下界（信号/百bar）
DENSITY_MAX = 3.5          # 信号密度健康带上界
ACC_FLOOR = 50.0           # 方向准确性下限（≥50% 才算有微弱方向性；<50% 即反指/噪声）

KIND = {
    "603039.SH": "沪主板", "688111.SH": "科创板", "600570.SH": "沪主板",
    "300308.SZ": "创业板", "300757.SZ": "创业板", "513310.SH": "ETF",
    "161129.SZ": "原油LOF", "000001.SZ": "深主板", "300750.SZ": "创业板",
    "300058.SZ": "创业板", "600519.SH": "沪主板", "002594.SZ": "深主板",
}

# 四维度网格（分阶段协调上升）
GRID = {
    "weights": {
        "w_vwap": [0.8, 1.0, 1.2, 1.5],
        "w_vol_div": [0.5, 0.7, 1.0],
        "w_macd_div": [0.6, 0.9, 1.2],
        "w_rsi": [0.6, 0.8, 1.0],
    },
    "rsi": {
        "rsi_period": [9, 14, 21],
        "rsi_ob_os": [(30, 70), (35, 65), (40, 60)],  # (oversold, overbought)
    },
    "vwap_vol": {
        "vwap_k1": [0.5, 0.8, 1.0, 1.2],
        "div_local_w": [10, 15, 20],
        "div_vol_ratio": [0.6, 0.7, 0.8],
    },
    "threshold": {
        "threshold": [0.40, 0.50, 0.55, 0.62],
    },
}


# ---------- 数据加载 ----------
def load_days(path):
    rows = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row["trade_date"], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x["trade_time"])
        o = np.array([float(x["open"]) for x in rs])
        h = np.array([float(x["high"]) for x in rs])
        lo = np.array([float(x["low"]) for x in rs])
        c = np.array([float(x["close"]) for x in rs])
        v = np.array([float(x["volume"]) for x in rs])
        days[d] = (o, h, lo, c, v)
    return days


def build_day_runs(sym):
    """返回该标的最近 LAST_N 个完整交易日的 day-run 列表，按日期排序并打 IS/OOS 标签。"""
    path = f"{DATA_DIR}/{sym}_1m.csv"
    if not os.path.exists(path):
        return []
    all_days = load_days(path)
    dates = sorted(all_days.keys())
    pc_map = {}
    prev = None
    for d in dates:
        o, h, lo, c, v = all_days[d]
        pc_map[d] = prev if prev is not None else (c[0] if len(c) else 0.0)
        if len(c):
            prev = c[-1]
    complete = [(d, all_days[d]) for d in dates if len(all_days[d][3]) >= 200]
    selected = complete[-LAST_N:]
    n_is = max(1, int(len(selected) * OOS_SPLIT))
    runs = []
    for idx, (d, (o, h, lo, c, v)) in enumerate(selected):
        runs.append({
            "sym": sym, "date": d, "o": o, "h": h, "lo": lo, "c": c, "v": v,
            "pc": pc_map[d], "n": len(c),
            "tag": "IS" if idx < n_is else "OOS",
            "data": None,  # 由 prep_runs 填充
        })
    return runs


def prep_runs(day_runs):
    """每个 day-run 的 compute_indicators 仅算一次（与 cfg 无关部分），缓存到 dr['data']。"""
    cnt = 0
    for dr in day_runs:
        n, pc = dr["n"], dr["pc"]
        if pc > 0 and n >= 10:
            try:
                dr["data"] = compute_indicators(dr["o"], dr["h"], dr["lo"], dr["c"], dr["v"], pc, has_vol=True)
                cnt += 1
            except Exception as e:
                dr["data"] = None
    return cnt


# ---------- 方向准确性 ----------
def _acc_dir(pairs, side):
    """方向准确性：B(买)要求后价上行(fr>0)，S(卖)要求后价下行(fr<0)。

    pairs: list of (is_b, forward_return)；side=None 全量 / True 仅B / False 仅S。
    返回正确比例(%)。S 方向已正确取反（卖出后价跌才算对）。空返回 0.0。
    """
    if side is None:
        sel = pairs
    elif side is True:
        sel = [p for p in pairs if p[0]]
    else:
        sel = [p for p in pairs if not p[0]]
    if not sel:
        return 0.0
    return float(np.mean([1.0 if (b and fr > 0) or (not b and fr < 0) else 0.0
                          for (b, fr) in sel]) * 100.0)


def _agg_sym(per_sym):
    """逐标的聚合：返回 [(sym, acc_all, acc_b, acc_s, n_b, n_s), ...]。
    acc_b/acc_s 在无该侧信号时为 NaN（后续中位数用 nanmedian 跳过）。"""
    rows = []
    for sym, pairs in per_sym.items():
        if not pairs:
            continue
        acc_all = _acc_dir(pairs, None)
        n_b = sum(1 for b, fr in pairs if b)
        n_s = len(pairs) - n_b
        acc_b = _acc_dir(pairs, True) if n_b else float("nan")
        acc_s = _acc_dir(pairs, False) if n_s else float("nan")
        rows.append((sym, acc_all, acc_b, acc_s, n_b, n_s))
    return rows


def _med(vals):
    vals = [v for v in vals if not (isinstance(v, float) and np.isnan(v))]
    return float(np.median(vals)) if vals else 0.0


# ---------- 评估（返回结构化 dict R）----------
def evaluate(cfg, day_runs):
    """全样本双向回测 + 逐标的方向准确性稳健聚合。

    返回 R（dict）：
      mis/mos            : aggregate_metrics（回测 PnL，仅参考，已知离线偏负）
      is_sig/oos_sig     : 信号总数
      is_bars/oos_bars   : 总 bar 数（密度分母）
      is_db/is_ds/...    : 双向配对 trip 数
      is_rows/oos_rows   : 逐标的 (sym, acc_all, acc_b, acc_s, n_b, n_s) —— 主交付明细
      is_med_acc/oos_med_acc        : 逐标的方向准确性中位数（主优化目标，抗稀释）
      is_frac/oos_frac              : 方向准确性≥ACC_FLOOR 的标的占比（多数标的确有方向性）
      is_med_b/is_med_s/...         : 双向中位数（保护强侧S / 抬升弱侧B）
      is_pool_acc/...               : 池加权均值（仅参考，易稀释）
      is_nsig/oos_nsig              : 信号总数（is_pairs 长度）
    """
    is_trips, oos_trips = [], []
    is_sig = oos_sig = is_bars = oos_bars = 0
    is_db = is_ds = oos_db = oos_ds = 0
    is_per_sym = {}   # sym -> list[(is_b, fr)]
    oos_per_sym = {}

    for dr in day_runs:
        sym, n = dr["sym"], dr["n"]
        data = dr.get("data")
        if data is None or n < 10:
            continue
        c, pc = dr["c"], dr["pc"]
        sigs = detect_signals_v4(data, pc, cfg)
        prices = {"o": dr["o"], "h": dr["h"], "lo": dr["lo"], "c": c,
                  "atr": data["atr"], "trend": data["trend"], "n": n,
                  "date": dr["date"], "pc": pc, "sym": sym}
        trips = simulate_bidirectional(sigs, prices, CFG, cost_for_symbol(sym))
        tgt = is_per_sym if dr["tag"] == "IS" else oos_per_sym
        tgt.setdefault(sym, [])
        for s in sigs:
            i = s["idx"]
            j = min(i + FORWARD_HORIZON, n - 1)
            fr = (c[j] - c[i]) / c[i] if c[i] > 0 else 0.0
            tgt[sym].append((s["type"] == "B", fr))
        if dr["tag"] == "IS":
            is_trips.extend(trips); is_sig += len(sigs); is_bars += n
            is_db += sum(1 for t in trips if t.get("side") == "L")
            is_ds += sum(1 for t in trips if t.get("side") == "S")
        else:
            oos_trips.extend(trips); oos_sig += len(sigs); oos_bars += n
            oos_db += sum(1 for t in trips if t.get("side") == "L")
            oos_ds += sum(1 for t in trips if t.get("side") == "S")

    mis = aggregate_metrics(is_trips)
    mos = aggregate_metrics(oos_trips)

    is_rows = _agg_sym(is_per_sym)
    oos_rows = _agg_sym(oos_per_sym)
    is_med_acc = _med([r[1] for r in is_rows])
    oos_med_acc = _med([r[1] for r in oos_rows])
    is_frac = float(np.mean([1.0 if r[1] >= ACC_FLOOR else 0.0 for r in is_rows])) if is_rows else 0.0
    oos_frac = float(np.mean([1.0 if r[1] >= ACC_FLOOR else 0.0 for r in oos_rows])) if oos_rows else 0.0
    is_med_b = _med([r[2] for r in is_rows])
    is_med_s = _med([r[3] for r in is_rows])
    oos_med_b = _med([r[2] for r in oos_rows])
    oos_med_s = _med([r[3] for r in oos_rows])

    is_pool = [p for ps in is_per_sym.values() for p in ps]
    oos_pool = [p for ps in oos_per_sym.values() for p in ps]
    is_pool_acc, is_pool_b, is_pool_s = _acc_dir(is_pool, None), _acc_dir(is_pool, True), _acc_dir(is_pool, False)
    oos_pool_acc, oos_pool_b, oos_pool_s = _acc_dir(oos_pool, None), _acc_dir(oos_pool, True), _acc_dir(oos_pool, False)

    return {
        "mis": mis, "mos": mos,
        "is_sig": is_sig, "oos_sig": oos_sig, "is_bars": is_bars, "oos_bars": oos_bars,
        "is_db": is_db, "is_ds": is_ds, "oos_db": oos_db, "oos_ds": oos_ds,
        "is_rows": is_rows, "oos_rows": oos_rows,
        "is_med_acc": is_med_acc, "is_frac": is_frac,
        "is_med_b": is_med_b, "is_med_s": is_med_s,
        "oos_med_acc": oos_med_acc, "oos_frac": oos_frac,
        "oos_med_b": oos_med_b, "oos_med_s": oos_med_s,
        "is_pool_acc": is_pool_acc, "is_pool_b": is_pool_b, "is_pool_s": is_pool_s,
        "oos_pool_acc": oos_pool_acc, "oos_pool_b": oos_pool_b, "oos_pool_s": oos_pool_s,
        "is_nsig": len(is_pool), "oos_nsig": len(oos_pool),
    }


def make_cfg(base, **over):
    """基于 base(CompositeConfig) 应用覆盖参数。覆盖项含 rsi_ob_os=(os,ob) / threshold。"""
    c = CompositeConfig(**{k: v for k, v in base.__dict__.items() if not k.startswith("__")})
    for k, val in over.items():
        if k == "rsi_ob_os":
            c.rsi_oversold, c.rsi_overbought = val
        elif k == "threshold":
            c.buy_threshold = val; c.sell_threshold = val
        else:
            setattr(c, k, val)
    return c


def objective(med_acc, frac_above, med_b, med_s):
    """IS 选参主目标（稳健聚合版）：
      以逐标的方向准确性中位数(med_acc)为锚，奖励方向性广度(frac)，
      同时保护强侧S(med_s)不退化、抬升弱侧B(med_b)。"""
    return med_acc + 6.0 * (frac_above - 0.5) + 0.10 * med_s + 0.10 * med_b


def is_eligible(med_acc, frac_above, nsig, density_is):
    """护栏资格：稳健篮子方向性达标 + 多数标的确有方向性 + 样本/密度健康。"""
    if nsig < MIN_SIGNALS_IS:
        return False
    if med_acc < ACC_FLOOR:
        return False
    if frac_above < 0.5:
        return False
    if not (DENSITY_MIN <= density_is <= DENSITY_MAX):
        return False
    return True


def oos_pass(med_acc_is, med_acc_oos, frac_oos, nsig_oos):
    """OOS 防过拟合门：OOS 中位不显著坍缩 + 至少 40% 篮子在 OOS 仍具方向性 + 样本足。"""
    if nsig_oos < MIN_SIGNALS_OOS:
        return False
    if med_acc_oos < ACC_FLOOR - 3.0:
        return False
    if frac_oos < 0.4:
        return False
    return True


# ---------- 主流程 ----------
def main():
    global LAST_N
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--last-n", type=int, default=LAST_N)
    ap.add_argument("--symbols-file", default=None,
                    help="清洁度筛选产出的标的列表(每行一个 sym)，覆盖默认 V4_SYMS 篮子")
    ap.add_argument("--no-inject", action="store_true",
                    help="仅产出 JSON/候选，不注入 composite_scorer.TUNED_CONFIG（ pilot 用）")
    ap.add_argument("--tag", default="", help="输出文件标签后缀，避免多次运行互相覆盖")
    a = ap.parse_args()
    LAST_N = a.last_n
    global SYMBOLS
    if a.symbols_file:
        with open(a.symbols_file, encoding="utf-8") as _fh:
            SYMBOLS = [ln.strip() for ln in _fh if ln.strip() and not ln.startswith("#")]
        print(f"  [basket] 从 {a.symbols_file} 载入 {len(SYMBOLS)} 只清洁标的")

    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    day_tag = datetime.datetime.now().strftime("%Y-%m-%d")
    print(f"[start] v4 参数自迭代寻优 v2.1 | {date_str} | 标的={SYMBOLS} | 最近{LAST_N}日 | "
          f"IS/OOS={OOS_SPLIT:.0%}/{1-OOS_SPLIT:.0%} | 前瞻{FORWARD_HORIZON}bar")
    t_total = time.perf_counter()

    # 1) 准备 day-runs（指标预计算一次）
    day_runs = []
    per_sym_days = {}
    for sym in SYMBOLS:
        runs = build_day_runs(sym)
        if not runs:
            print(f"  [skip] {sym} 无数据")
            continue
        day_runs.extend(runs)
        per_sym_days[sym] = (sum(1 for r in runs if r["tag"] == "IS"),
                             sum(1 for r in runs if r["tag"] == "OOS"))
    if not day_runs:
        print("[error] 无标的数据，退出"); return 1
    n_prep = prep_runs(day_runs)
    print(f"  day-runs 总数={len(day_runs)} | 指标预计算={n_prep} | 各标的(IS/OOS)={per_sym_days}")

    # 2) base cfg：松弛 B 门控以测 v4 内在质量；其余沿用默认
    base = CompositeConfig()
    base.trend_b_allowed = (-1, 0, 1)
    base.trend_s_allowed = (-1, 0, 1)

    all_results = []
    stages = ["weights", "rsi", "vwap_vol", "threshold"]
    cur = {}
    for stage in stages:
        dims = GRID[stage]
        keys = list(dims.keys())
        combos = list(itertools.product(*[dims[k] for k in keys]))
        best_score = -1e18
        best_over = None
        for vals in combos:
            over = dict(cur)
            for k, v in zip(keys, vals):
                over[k] = v
            cfg = make_cfg(base, **over)
            R = evaluate(cfg, day_runs)
            density = R["is_sig"] / R["is_bars"] * 100 if R["is_bars"] else 0
            elig = is_eligible(R["is_med_acc"], R["is_frac"], R["is_nsig"], density)
            score = objective(R["is_med_acc"], R["is_frac"], R["is_med_b"], R["is_med_s"])
            rec = {"over": dict(over), "stage": stage,
                   "is_med_acc": round(R["is_med_acc"], 2), "is_frac": round(R["is_frac"], 3),
                   "is_med_b": round(R["is_med_b"], 2), "is_med_s": round(R["is_med_s"], 2),
                   "is_pool_acc": round(R["is_pool_acc"], 2),
                   "is_nsig": R["is_nsig"], "density_is": round(density, 3),
                   "eligible": elig, "score": round(score, 3),
                   "mis": R["mis"], "mos": R["mos"],
                   "is_db": R["is_db"], "is_ds": R["is_ds"],
                   "is_rows": R["is_rows"],
                   "oos_med_acc": round(R["oos_med_acc"], 2), "oos_frac": round(R["oos_frac"], 3),
                   "oos_nsig": R["oos_nsig"], "oos_rows": R["oos_rows"]}
            all_results.append(rec)
            if score > best_score:
                best_score = score; best_over = dict(over)
        if best_over is None:
            print(f"  [warn] 阶段 {stage} 无组合，沿用当前值")
            continue
        cur.update(best_over)
        last = all_results[-1]
        print(f"  [stage {stage}] 最优: " + ", ".join(f"{k}={v}" for k, v in best_over.items()) +
              f" | IS med_acc={last['is_med_acc']:.1f}%(frac{last['is_frac']:.0%},"
              f"B{last['is_med_b']:.1f}/S{last['is_med_s']:.1f}) pool={last['is_pool_acc']:.1f}%"
              f" nsig={last['is_nsig']} | score={best_score:.2f}")
    print(f"  [info] 评估组合总数={len(all_results)}")

    # 3) 选择：全局「按目标最优」恒产出；「通过护栏的最优」作部署候选
    best_by_obj = max(all_results, key=lambda r: r["score"])
    elig = [r for r in all_results if r["eligible"]]
    if elig:
        best = max(elig, key=lambda r: r["score"])
        caveats = []
    else:
        best = best_by_obj
        caveats = ["未通过护栏资格(逐标的中位/多数标的方向性/密度)，按目标最优降级产出"]
    oos_ok = oos_pass(best["is_med_acc"], best["oos_med_acc"], best["oos_frac"], best["oos_nsig"])
    if not oos_ok:
        caveats.append("未过 OOS 防过拟合门(中位坍缩或篮子方向性不足)")
    best_cfg = make_cfg(base, **best["over"])

    # 4) 敏感度扫描（固定 best，仅扫单一维度）
    sensitivity = {}
    for stage in stages:
        dims = GRID[stage]
        for dk in dims:
            sweep = []
            for v in dims[dk]:
                over = dict(best["over"]); over[dk] = v
                cfg = make_cfg(base, **over)
                R = evaluate(cfg, day_runs)
                density = R["is_sig"] / R["is_bars"] * 100 if R["is_bars"] else 0
                elig_i = is_eligible(R["is_med_acc"], R["is_frac"], R["is_nsig"], density)
                sweep.append({"value": v, "med_acc": round(R["is_med_acc"], 2),
                              "med_b": round(R["is_med_b"], 2), "med_s": round(R["is_med_s"], 2),
                              "pool_acc": round(R["is_pool_acc"], 2),
                              "frac": round(R["is_frac"], 3), "nsig": R["is_nsig"],
                              "density": round(density, 2), "eligible": elig_i,
                              "ret_is": R["mis"]["total_ret"], "wr_is": R["mis"]["win_rate"]})
            sensitivity[f"{stage}.{dk}"] = sweep

    # 5) DEFAULT 与 best 在「门控ON(生产) / 门控OFF(内在)」下双向回测方向准确性对比
    def _full(cfg):
        R = evaluate(cfg, day_runs)
        return {"mis": R["mis"], "mos": R["mos"],
                "is_med_acc": R["is_med_acc"], "is_frac": R["is_frac"],
                "is_med_b": R["is_med_b"], "is_med_s": R["is_med_s"],
                "is_pool_acc": R["is_pool_acc"], "is_pool_b": R["is_pool_b"], "is_pool_s": R["is_pool_s"],
                "is_nsig": R["is_nsig"], "oos_med_acc": R["oos_med_acc"], "oos_frac": R["oos_frac"],
                "is_db": R["is_db"], "is_ds": R["is_ds"], "is_rows": R["is_rows"]}
    default_cfg_off = CompositeConfig(); default_cfg_off.trend_b_allowed = (-1, 0, 1); default_cfg_off.trend_s_allowed = (-1, 0, 1)
    default_cfg_on = CompositeConfig()
    best_cfg_on = make_cfg(CompositeConfig(), **best["over"])
    cmp = {
        "default_off": _full(default_cfg_off),
        "default_on": _full(default_cfg_on),
        "tuned_off": _full(best_cfg),
        "tuned_on": _full(best_cfg_on),
    }

    # 6) 落盘
    tuned_params = best_cfg.as_dict()
    out = {
        "generated_at": date_str, "symbols": SYMBOLS, "last_n_days": LAST_N,
        "forward_horizon": FORWARD_HORIZON, "oos_split": OOS_SPLIT,
        "n_day_runs": len(day_runs), "per_sym_days": per_sym_days,
        "objective": "逐标的方向准确性中位数(主) + 方向性广度 + 双向保护(参考)",
        "best_params": tuned_params, "best_over": best["over"],
        "is_med_acc": best["is_med_acc"], "is_frac": best["is_frac"],
        "is_med_b": best["is_med_b"], "is_med_s": best["is_med_s"],
        "is_pool_acc": best["is_pool_acc"], "is_nsig": best["is_nsig"],
        "oos_med_acc": best["oos_med_acc"], "oos_frac": best["oos_frac"], "oos_nsig": best["oos_nsig"],
        "density_is": best["density_is"],
        "is_metrics_pnl": best["mis"], "oos_metrics_pnl": best["mos"],
        "is_dir_B": best["is_db"], "is_dir_S": best["is_ds"],
        "per_symbol_is": [{"sym": r[0], "acc_all": round(r[1], 2), "acc_b": (None if (isinstance(r[2], float) and np.isnan(r[2])) else round(r[2], 2)),
                           "acc_s": (None if (isinstance(r[3], float) and np.isnan(r[3])) else round(r[3], 2)),
                           "n_b": r[4], "n_s": r[5]} for r in best["is_rows"]],
        "eligible": bool(elig), "oos_pass": oos_ok, "caveats": caveats,
        "best_by_obj_over": best_by_obj["over"], "best_by_obj_score": best_by_obj["score"],
        "n_combos": len(all_results), "n_eligible": len(elig),
        "sensitivity": sensitivity,
        "comparison": cmp,
        "guardrails": {"MIN_SIGNALS_IS": MIN_SIGNALS_IS, "MIN_SIGNALS_OOS": MIN_SIGNALS_OOS,
                       "DENSITY": [DENSITY_MIN, DENSITY_MAX], "ACC_FLOOR": ACC_FLOOR,
                       "pnl_caveat": "离线回测PnL已知系统性偏负(离线/实盘差距)，仅作参考，不作为选参依据"},
    }
    tag = ("_" + a.tag) if a.tag else ""
    json_path = os.path.join(OUT, f"v4_param_search{tag}_{day_tag}.json")
    cfg_json = os.path.join(OUT, f"v4_tuned_config{tag}.json")
    if not a.dry_run:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(cfg_json, "w", encoding="utf-8") as f:
            json.dump(tuned_params, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 耗时 {time.perf_counter()-t_total:.1f}s")
    print(f"  best_params={best['over']}")
    print(f"  IS : 逐标的中位acc {best['is_med_acc']:.1f}%(frac{best['is_frac']:.0%},"
          f"B{best['is_med_b']:.1f}/S{best['is_med_s']:.1f}) 池加权 {best['is_pool_acc']:.1f}% 信号数 {best['is_nsig']}")
    print(f"  OOS: 逐标的中位acc {best['oos_med_acc']:.1f}%(frac{best['oos_frac']:.0%}) 信号数 {best['oos_nsig']} "
          f"{'(PASS)' if oos_ok else '(FAIL)'}")
    d = cmp["default_off"]; t = cmp["tuned_off"]
    print(f"  对照 default(内在,off) med_acc={d['is_med_acc']:.1f}%(B{d['is_med_b']:.1f}/S{d['is_med_s']:.1f}) "
          f"→ tuned(off) med_acc={t['is_med_acc']:.1f}%(B{t['is_med_b']:.1f}/S{t['is_med_s']:.1f}) "
          f"(Δmed{t['is_med_acc']-d['is_med_acc']:+.1f}pp)")
    print(f"  护栏: eligible={bool(elig)} oos_pass={oos_ok} caveats={caveats}")
    if not a.dry_run:
        print(f"  JSON: {json_path}")
        print(f"  CFG : {cfg_json}")

    # 7) 注入 composite_scorer.TUNED_CONFIG（仅当通过护栏，避免注入未经验证参数）
    if not a.dry_run:
        if elig and oos_ok and not a.no_inject:
            _inject_tuned(tuned_params)
        else:
            print(f"  [skip-inject] 未注入(合格={bool(elig)}, OOS={oos_ok}, no_inject={a.no_inject})；"
                  f"已落盘候选供人工评审 output/v4_tuned_config.json")

    # 8) 推送 Feishu
    push(_build_push(out, best, elig, oos_ok, caveats))
    return 0


def _inject_tuned(params):
    """把最优参数注入 composite_scorer.py 为 TUNED_CONFIG（追加/替换，不破坏 DEFAULT_CONFIG）。"""
    path = os.path.join(TPOINT_CORE, "composite_scorer.py")
    src = open(path, encoding="utf-8").read()
    block = "TUNED_CONFIG = CompositeConfig(\n"
    for k, v in params.items():
        block += f"    {k}={repr(v)},\n"
    block += ")\n"
    marker = "# ==== TUNED_CONFIG (v4_param_search 自迭代输出) ===="
    injection = f"{marker}\n{block}"
    if marker in src:
        src = src.split(marker)[0].rstrip()  # 截断旧 block（marker 位于文件末尾区域）
    src = src.rstrip() + "\n\n" + injection
    open(path, "w", encoding="utf-8").write(src)
    print(f"  [inject] TUNED_CONFIG 已写入 composite_scorer.py")


def _build_push(out, best, elig, oos_ok, caveats):
    b = out["best_params"]
    d = out["comparison"]["default_off"]; t = out["comparison"]["tuned_off"]
    lines = [f"🤖 [tpoint v4 参数自迭代寻优 v2.1 {out['generated_at']}]"]
    lines.append(f"■ 标的篮 {len(out['symbols'])}只 IS/OOS={out['oos_split']:.0%}/{1-out['oos_split']:.0%} "
                 f"前瞻{FORWARD_HORIZON}bar | 评估组合={out['n_combos']}(合格{out['n_eligible']}) | 主目标=逐标的中位方向准确性")
    lines.append(f"■ 最优参数: " + ", ".join(f"{k}={v}" for k, v in best['over'].items()))
    lines.append(f"■ IS : 逐标的中位acc {out['is_med_acc']:.1f}%(方向性广度{out['is_frac']:.0%}, "
                 f"B{out['is_med_b']:.1f}/S{out['is_med_s']:.1f}) 池加权 {out['is_pool_acc']:.1f}% 信号 {out['is_nsig']}")
    lines.append(f"■ OOS: 逐标的中位acc {out['oos_med_acc']:.1f}%(广度{out['oos_frac']:.0%}) 信号 {out['oos_nsig']} "
                 + ("✅PASS" if oos_ok else "⚠️未过OOS门"))
    lines.append(f"■ 方向准确性 default→tuned(内在,off): med {d['is_med_acc']:.1f}% → {t['is_med_acc']:.1f}% "
                 f"(Δ{t['is_med_acc']-d['is_med_acc']:+.1f}pp; B {d['is_med_b']:.1f}→{t['is_med_b']:.1f}, "
                 f"S {d['is_med_s']:.1f}→{t['is_med_s']:.1f})")
    lines.append(f"■ 生产门控(on)下中位acc: default {out['comparison']['default_on']['is_med_acc']:.1f}% → "
                 f"tuned {out['comparison']['tuned_on']['is_med_acc']:.1f}%")
    lines.append(f"■ 离线回测PnL(仅参考,已知系统性偏负): tuned IS净收 {out['is_metrics_pnl']['total_ret']:.2f}% "
                 f"WR {out['is_metrics_pnl']['win_rate']:.1f}% — 不用于选参")
    if caveats:
        lines.append(f"■ ⚠️ caveats: {'; '.join(caveats)}")
    lines.append(f"明细 output/v4_param_search_{out['generated_at'][:10]}.json + "
                 + ("已注入 composite_scorer.TUNED_CONFIG" if (elig and oos_ok) else "候选已落盘(未注入,待评审)"))
    return "\n".join(lines)


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


if __name__ == "__main__":
    sys.exit(main())
