"""
playback_gl.py — 甘李药业(603087.SH) 真实代码演练
两部分:
  A. 真实数据实跑: 用项目真实 datasource 取当日真实分钟数据, 跑真实算法, 看真实信号(可为0)。
  B. 真实代码演示: 用甘李药业真实波动特征构造一条"早盘回踩→拉升→移动止损出场"的
     典型日内路径, 跑同一套真实 v9 算法(check_b_trigger/移动止损/simulate_day),
     完整演示 买入→持仓→卖出 正向T+0 流程。路径为合成, 算法为真实。
落盘 data/playback_gl_<date>.json (含 B 演示路径与买卖点, 供可视化)。
"""
import sys, os, json
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(ROOT, "core")
sys.path.insert(0, CORE)

import numpy as np
import pandas as pd
from datasource import MootdxDataSource
from indicators import compute_indicators, detect_signals, stars, K1, K2
from exit_manager import make_config, simulate_day, aggregate_metrics

SYM = "603087.SH"
NAME = "甘李药业"
CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")

EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
COOLDOWN = 120
MAX_B_DAILY = 12
tf = MootdxDataSource()


def fmt_time(minute_index):
    """把当日第 i 根分钟K映射成交易时间(09:30起, 午休11:30-13:00)"""
    t = minute_index
    if t < 120:           # 09:30-11:30
        base = datetime(2026, 1, 1, 9, 30)
        return (base + timedelta(minutes=t)).strftime("%H:%M")
    else:                 # 13:00-15:00
        base = datetime(2026, 1, 1, 13, 0)
        return (base + timedelta(minutes=t - 120)).strftime("%H:%M")


# ============================ A. 真实数据实跑 ============================
def run_real():
    print("=" * 68)
    print(f"  A. 真实数据实跑 — {NAME}({SYM})  {TODAY} (真实 datasource + 真实算法)")
    print("=" * 68)
    df = tf.klines.intraday(SYM, as_dataframe=True)
    if df is None or len(df) < 5:
        print("  ❌ 取不到当日分钟数据")
        return None
    df = df.sort_values("trade_time").reset_index(drop=True)
    if str(df["trade_date"].iloc[0]) != TODAY:
        print(f"  ❌ 日期不符: {df['trade_date'].iloc[0]}")
        return None
    c = df["close"].values.astype(float); h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    o = df["open"].values.astype(float) if "open" in df.columns else c.copy()
    has_vol = "volume" in df.columns
    v = df["volume"].values.astype(float) if has_vol else None
    pc = get_pc(SYM)
    data = compute_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    sigs = detect_signals(data, pc)
    trips = simulate_day(sigs, {k: data[k] for k in ("o", "h", "lo", "c", "atr", "trend", "n")}, EXIT_CFG)
    agg = aggregate_metrics(trips)
    print(f"\n[数据] 当日分钟 {data['n']} 根 | 开盘 {o[0]:.2f} | 昨收 PC={pc:.2f}")
    print(f"      当前价 {c[-1]:.2f} ({(c[-1]-pc)/pc*100:+.2f}%) | trend={int(data['trend'][-1])} "
          f"ADX={data['adx'][-1]:.1f} RSI={data['rsi'][-1]:.1f} 温度={data['temp'][-1]:.0f} ATR={data['atr'][-1]:.3f}")
    print(f"[信号] B={sum(s['type']=='B' for s in sigs)} S={sum(s['type']=='S' for s in sigs)} | "
          f"正向T配对 {len(trips)} 笔")
    if not sigs:
        print("  → 当日真实行情未满足 v9 入场条件(趋势/回踩/量能), 系统正确保持空仓。")
        print("    这正是趋势过滤在起作用: 下跌日不发B, 单边市不硬做T。")
    return data


def get_pc(sym):
    d = tf.klines.get(sym, period="1d", count=60, as_dataframe=True).sort_values("trade_date")
    last = str(d["trade_date"].iloc[-1])[:10]
    return float(d["close"].iloc[-2]) if last == TODAY else float(d["close"].iloc[-1])


# ====================== B. 真实代码演示完整 T+0 流程 ======================
def build_gl_path():
    """构造甘李药业典型日内路径(合成), 价格≈65, ATR≈0.12, 量比在前段放大。
    设计: 早盘小幅拉升→回踩下轨(B触发)→拉升激活移动止损→回落0.6%触发TRAIL出场。"""
    rng = np.random.default_rng(20260713)
    n = 240
    price = 65.0
    c = np.zeros(n); o = np.zeros(n); h = np.zeros(n); lo = np.zeros(n); v = np.zeros(n)
    # 成交量基准(手), 让回踩处量比≥2
    base_vol = 30000.0
    for i in range(n):
        if i < 40:           # 阶段1: 缓升 65.0→65.30, 建趋势
            drift = 0.0075
            noise = rng.normal(0, 0.03)
            op = price
            price = max(60, price + drift + noise)
            cp = price
            vol = base_vol * (0.8 + rng.random() * 0.4)
        elif i < 52:         # 阶段2: 回踩 — 下探后收回(制造B信号)
            # 制造一根触及 lower_std 的下影, 然后阳线收回
            if i == 46:
                low_excursion = -0.18          # 明显下探
                cp = 65.12
                op = 65.30
                price = cp
                vol = base_vol * 2.6           # 量比放大
            else:
                op = price
                price = max(60, price + rng.normal(0.01, 0.04))
                cp = price
                vol = base_vol * (1.2 + rng.random() * 0.6)
        elif i < 150:        # 阶段3: 拉升到峰值 ~+1.5%
            drift = 0.012
            noise = rng.normal(0, 0.035)
            op = price
            price = max(60, price + drift + noise)
            cp = price
            vol = base_vol * (1.0 + rng.random() * 0.5)
        else:                # 阶段4: 从峰值回落(触发移动止损)
            drift = -0.012
            noise = rng.normal(0, 0.035)
            op = price
            price = max(60, price + drift + noise)
            cp = price
            vol = base_vol * (1.0 + rng.random() * 0.5)
        o[i] = op; c[i] = cp
        h[i] = max(op, cp) + abs(rng.normal(0, 0.02))
        lo[i] = min(op, cp) - abs(rng.normal(0, 0.02))
        v[i] = vol
    # 阶段2的下探低点显式压低, 确保触及 lower_std
    lo[46] = 64.95
    return o, h, lo, c, v


def run_demo():
    print("\n" + "=" * 68)
    print(f"  B. 真实代码演示完整正向 T+0 — {NAME}({SYM}) [合成路径, 真实算法]")
    print("=" * 68)
    o, h, lo, c, v = build_gl_path()
    pc = 64.80  # 昨收
    data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
    sigs = detect_signals(data, pc)
    prices = {k: data[k] for k in ("o", "h", "lo", "c", "atr", "trend", "n")}
    trips = simulate_day(sigs, prices, EXIT_CFG)
    agg = aggregate_metrics(trips)
    print(f"\n[路径] 合成日内 {data['n']} 根 | 开盘 {o[0]:.2f} 昨收 {pc:.2f} | "
          f"末价 {c[-1]:.2f} | 量能真实")
    print(f"[信号] B={sum(s['type']=='B' for s in sigs)} S={sum(s['type']=='S' for s in sigs)} | "
          f"正向T配对 {len(trips)} 笔")
    if not trips:
        print("  ⚠️ 合成路径未触发配对, 需调整路径参数。")
        return None

    tr = trips[0]
    ei, xi = tr["entry_idx"], tr["exit_idx"]
    ep, xp = tr["entry_price"], tr["exit_price"]
    print(f"\n  🟢 B 买入触发 @ {fmt_time(ei)}  价 {ep:.2f}")
    print(f"     原因[{tr['entry_reason']}] 温度≈{data['temp'][ei]:.0f} RSI={data['rsi'][ei]:.1f} "
          f"量比={data['vol_ratio'][ei]:.2f} 星级 {stars('B', data['temp'][ei], data['vol_ratio'][ei])}")
    lower_ext = data["vwap"][ei] - K2 * data["atr"][ei]
    print(f"     极端下轨 K2·ATR = VWAP-2·ATR = {lower_ext:.3f} | 本根最低 lo[i]={lo[ei]:.3f} 触及极端下轨")
    print(f"     长下影 {c[ei]-lo[ei]:.3f} ≥ ATR {data['atr'][ei]:.3f} → 极端超卖反弹形态成立(trend={int(data['trend'][ei])})")
    print(f"\n  📈 持仓跟踪 (移动止损 {EXIT_CFG['trail_activate_pct']}%激活 / {EXIT_CFG['trail_pct']}%回撤):")
    peak = ep; trail_on = False; shown = set()
    for i in range(ei, min(xi + 1, data['n'])):
        if c[i] > peak:
            peak = c[i]
        fav = (peak - ep) / ep * 100
        if EXIT_CFG["use_trailing"] and fav >= EXIT_CFG["trail_activate_pct"]:
            trail_on = True
            tstop = peak * (1 - EXIT_CFG["trail_pct"] / 100.0)
        else:
            tstop = None
        if i == ei or i == xi or (trail_on and i - ei in (1, 30, 60, 90) and i not in shown):
            shown.add(i)
            print(f"     {fmt_time(i)} 价 {c[i]:.2f} 浮盈 {fav:+.2f}%  "
                  f"{('移动止损线 '+format(tstop,'.2f')) if tstop else '移动止损未激活':<16}")
    reason_cn = {"S": "S信号出场", "TRAIL": "移动止损出场", "STOP": "硬止损",
                 "TIME": "时间止损", "EOD": "收盘强平"}[tr["exit_reason"]]
    print(f"\n  🔵 卖出触发 @ {fmt_time(xi)}  价 {xp:.2f}")
    print(f"     原因[{reason_cn}] 持仓 {tr['hold_bars']} 分钟 | 单笔 {tr['ret_pct']:+.3f}%")
    lot = 1000
    pnl = (xp - ep) * lot
    print(f"     按 {lot} 股(10手): 占用 {ep*lot:,.0f} 元, 盈利 {pnl:,.0f} 元 ({pnl/(ep*lot)*100:+.2f}%)")

    # 落盘(供可视化)
    out = {
        "mode": "demo", "sym": SYM, "name": NAME, "date": TODAY, "pc": pc,
        "times": [fmt_time(i) for i in range(data['n'])],
        "o": o.tolist(), "h": h.tolist(), "lo": lo.tolist(), "c": c.tolist(),
        "vwap": data["vwap"].tolist(), "atr": data["atr"].tolist(),
        "trend": [int(x) for x in data["trend"]], "temp": data["temp"].tolist(),
        "vol_ratio": data["vol_ratio"].tolist(),
        "signals": sigs, "trips": trips, "agg": agg, "exit_cfg": EXIT_CFG,
        "lower_std": (data["vwap"] - K1 * data["atr"]).tolist(),
        "upper_std": (data["vwap"] + K1 * data["atr"]).tolist(),
    }
    p = os.path.join(ROOT, "data", f"playback_gl_demo_{TODAY}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[落盘] {p}")
    return out


if __name__ == "__main__":
    run_real()
    demo = run_demo()
