"""core/simulate_bidirectional.py — 双向(正T + 反T)单仓位配对回测
=================================================================================
用途：公平评估 v4 综合评分模型的 B 与 S **双侧**信号质量。

背景：
    exit_manager.simulate_day 仅支持「正T（先买后卖）」配对。在 v4 综合评分模型下，
    默认趋势门控「B 仅允许上升市」导致 B 信号稀少、S 信号主导；而 S 信号在正T 模型里
    无持仓可平 → 几乎不参与 round-trip，于是 v4 的 S 侧质量无法量化（见方法论 v1.1.0 §4.5）。

设计：
    - 独立新模块，**不修改 exit_manager 生产代码**（保持生产链路零风险）。
    - 镜像 exit_manager 的出场规则：硬止损(atr) / 时间止损 / 移动止损 / 反向信号出场 / 收盘强平，
      对 long(正T) 与 short(反T) 完全对称。
    - 单仓位模型：空仓时首个信号建仓（B→开多 / S→开空）；持仓时被反向信号或出场规则平仓；
      同向信号在持仓中忽略。序列 B S B S → 多trip(long) + 空trip(short) 交替。
    - 成本：long 付 买边+卖边；short 付 卖边+买边（双边成本口径与 exit_manager 一致，
      由 cost_for_symbol 按标的自动选：个股含印花 / ETF·LOF 无印花 / 北交所千0.575）。
    - 成交可行性：复用 exit_manager.limit_thr —— 跌停(bar)不可「卖」，因此开空(S)与多仓平仓
      被该 bar 阻断；买回(空平)不受跌停限制（买盘通畅）。

返回：与 exit_manager.simulate_day 同构的 round_trips 列表（含 side 字段），
      可直接喂 aggregate_metrics 做指标汇总。
"""
from typing import Optional, Dict, Any, List, Tuple

import numpy as np

from exit_manager import cost_for_symbol, limit_thr  # 复用生产侧成本/涨跌停口径


def _open(side: str, sig: Dict[str, Any], atr_i: float, config: Dict[str, Any]) -> Dict[str, Any]:
    """按 side 开仓（'L'=多/正T, 'S'=空/反T）。"""
    price = float(sig["price"])
    if side == "L":
        stop = (price - config["stop_atr_mult"] * atr_i) if config["use_stop"] else -1e9
        return {"side": "L", "entry_idx": sig["idx"], "entry_price": price,
                "entry_reason": sig.get("reason", ""), "stop_price": stop, "max_fav": price}
    else:
        stop = (price + config["stop_atr_mult"] * atr_i) if config["use_stop"] else 1e9
        return {"side": "S", "entry_idx": sig["idx"], "entry_price": price,
                "entry_reason": sig.get("reason", ""), "stop_price": stop, "min_fav": price}


def _mk_trip(pos, exit_idx, exit_price, reason, buy_cost, sell_cost, entry_date=None):
    entry_price = pos["entry_price"]
    side = pos["side"]
    if side == "L":
        gross = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    else:  # 空：entry(卖高) - exit(买低)
        gross = (entry_price - exit_price) / entry_price * 100 if entry_price > 0 else 0.0
    net = gross - buy_cost - sell_cost
    return {
        "entry_idx": pos["entry_idx"], "exit_idx": int(exit_idx),
        "entry_price": round(float(entry_price), 2), "exit_price": round(float(exit_price), 2),
        "exit_reason": reason, "ret_pct": round(float(net), 3),
        "gross_ret_pct": round(float(gross), 3),
        "hold_bars": int(exit_idx - pos["entry_idx"]),
        "entry_reason": pos.get("entry_reason", ""), "entry_date": entry_date,
        "side": side,
    }


def _check_exit(pos, i, c, lo, h, atr, trend, b_idx, s_idx, config, block_sell):
    """返回 (exit_price, reason) 或 (None, None)。

    block_sell=True 表示本 bar 跌停不可卖 → 阻断一切「卖」动作（开空 / 多仓平仓 / 多仓止损），
    但不阻断「买」动作（空仓回补 / 空仓止损）。
    """
    side = pos["side"]
    if side == "L":
        # 1) 硬止损（风险兜底，最高优先）
        if config["use_stop"]:
            if config["stop_mode"] == "trend":
                if trend is not None and trend[i] == -1 and not block_sell:
                    return c[i], "STOP"
            elif lo[i] <= pos["stop_price"] and not block_sell:
                return pos["stop_price"], "STOP"
        # 2) 更新浮动盈利高点
        if c[i] > pos["max_fav"]:
            pos["max_fav"] = c[i]
        # 3) S 信号出场（自然出场）
        if config["s_signal_exit"] and i in s_idx and not block_sell:
            return s_idx[i]["price"], "S"
        # 4) 移动止损（浮盈保护）
        if config["use_trailing"]:
            fav_ret = (pos["max_fav"] - pos["entry_price"]) / pos["entry_price"] * 100
            if fav_ret >= config["trail_activate_pct"]:
                trail_stop = pos["max_fav"] * (1 - config["trail_pct"] / 100.0)
                if c[i] <= trail_stop and trail_stop > pos["stop_price"] and not block_sell:
                    return c[i], "TRAIL"
        # 5) 时间止损（超时强平）
        if config["use_time"] and (i - pos["entry_idx"]) >= config["time_stop_bars"] and not block_sell:
            return c[i], "TIME"
    else:  # SHORT（所有出场均为「买」，不受跌停阻断）
        if config["use_stop"]:
            if config["stop_mode"] == "trend":
                if trend is not None and trend[i] == 1:  # 升势确认，空错 → 回补
                    return c[i], "STOP"
            elif h[i] >= pos["stop_price"]:
                return pos["stop_price"], "STOP"
        if c[i] < pos["min_fav"]:
            pos["min_fav"] = c[i]
        if config["s_signal_exit"] and i in b_idx:  # B 信号回补
            return b_idx[i]["price"], "B"
        if config["use_trailing"]:
            fav_ret = (pos["entry_price"] - pos["min_fav"]) / pos["entry_price"] * 100
            if fav_ret >= config["trail_activate_pct"]:
                trail_stop = pos["min_fav"] * (1 + config["trail_pct"] / 100.0)
                if c[i] >= trail_stop and trail_stop < pos["stop_price"]:
                    return c[i], "TRAIL"
        if config["use_time"] and (i - pos["entry_idx"]) >= config["time_stop_bars"]:
            return c[i], "TIME"
    return None, None


def simulate_bidirectional(signals, prices, config, cost=None):
    """对单日信号做双向(正T+反T)单仓位配对模拟，应用与 exit_manager 对称的出场规则。

    参数：
      signals : detect_signals_v4 输出（含 type/idx/price）
      prices  : dict，含 'o','h','lo','c' 数组、'atr'、'trend'、'n'、'date'、'pc'、'sym'
      config  : make_config() 输出（出场配置 dict）
      cost    : (buy_cost_pct, sell_cost_pct)；默认 cost_for_symbol(prices['sym'])
    返回：round_trips 列表（schema 同 exit_manager.simulate_day，额外含 'side'）。
    """
    if cost is None:
        cost = cost_for_symbol(prices.get("sym"))
    buy_cost, sell_cost = cost
    n = prices["n"]
    c = prices["c"]; lo = prices["lo"]; h = prices["h"]; atr = prices["atr"]
    trend = prices.get("trend")
    day_date = prices.get("date")
    _pc = prices.get("pc"); _sym = prices.get("sym")

    # 跌停不可卖：pc+sym 存在时算 locked_down 数组（与 exit_manager 同口径）
    locked_down = None
    if _pc and _pc > 0 and _sym and h is not None:
        _ld = round(float(_pc) * (1 - limit_thr(_sym)), 2)
        locked_down = h <= _ld + 0.02

    b_idx = {s["idx"]: s for s in signals if s["type"] == "B"}
    s_idx = {s["idx"]: s for s in signals if s["type"] == "S"}

    trips = []
    pos = None
    for i in range(2, n):
        block_sell = (locked_down is not None) and bool(locked_down[i])

        if pos is None:
            # 空仓 → 首个信号建仓（开多=买，开空=卖需非跌停）
            if i in b_idx:
                pos = _open("L", b_idx[i], atr[i], config)
            elif i in s_idx and not block_sell:
                pos = _open("S", s_idx[i], atr[i], config)
            continue

        exit_price, reason = _check_exit(
            pos, i, c, lo, h, atr, trend, b_idx, s_idx, config, block_sell)
        if exit_price is not None:
            trips.append(_mk_trip(pos, i, exit_price, reason, buy_cost, sell_cost, day_date))
            pos = None

    # 收盘仍未平仓 → 强平(EOD)
    if pos is not None:
        trips.append(_mk_trip(pos, n - 1, c[n - 1], "EOD", buy_cost, sell_cost, day_date))
    return trips
