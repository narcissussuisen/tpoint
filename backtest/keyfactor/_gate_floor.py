#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_gate_floor.py — floor 门控共享纯函数模块

供 miji_engine.py (隔离/OOS) 与 core/miji_alpha.py (生产/实时) 共同导入，
消除两处独立的门控逻辑重复。

所有函数无状态、无副作用；所有参数通过 kwargs 传入，默认值 = 当前生产硬编码值。

改进开关（从 kwargs 注入，默认关闭=等效于当前行为）：
  - floor_sell_cooldown_bars : 价格天花板 S 冷却期
  - floor_buy_cooldown_bars  : 价格地板 B 冷却期
  - floor_suppress_day_chg   : 涨停/近涨停日关闭天花板 S 通道
  - floor_suppress_buy_day_chg: 跌日关闭地板 B 通道（对称于上一行，防接飞刀）
  - floor_trend_threshold    : 趋势感知缩放（预留）
"""

import numpy as np

# ---- 默认值（与 miji_engine 硬编码一致） ----
_DEFAULT_FLOOR_DEV_PCT = 1.5
_DEFAULT_LOCAL_W = 15
# 跌日 B 通道抑制阈值(%, 日内涨跌幅): day_chg <= 此值 → 关闭价格地板 B 通道(防接飞刀)。
# 对称于 floor_suppress_day_chg(涨日关天花板S); 默认 -1.0 = 当日跌幅≥1%即视为跌日抑制。
# 0.0 = 关闭此抑制(等效原行为)。启发式值, 待多日OOS(F:盘4071标的1m)调参确认。
DEFAULT_FLOOR_SUPPRESS_BUY_DAY_CHG = -1.0


def _is_new_low(c, lo, i, w=_DEFAULT_LOCAL_W):
    """lo[i] 是否创窗口内新低（严格 < 前窗口最低价）。floor 档价格地板 B 用。

    2026-07-26 修正(与生产 core/miji_alpha._is_new_low 同步):
    旧实现用 c[i](收盘) 比前窗 lo.min() -> 结构性漏底。改为用 BAR 自身 lo[i] 比前窗
    lo.min(), 即真正的 swing-low 判定。回测引擎须与生产一致。
    """
    if i < 1:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def _is_new_high(c, h, i, w=_DEFAULT_LOCAL_W):
    """h[i] 是否创窗口内新高（严格 > 前窗口最高价）。floor 档价格天花板 S 用。

    2026-07-26 修正(与生产 core/miji_alpha._is_new_high 同步):
    旧实现用 c[i](收盘) 比前窗 h.max() -> 结构性漏顶。改为用 BAR 自身 h[i] 比前窗
    h.max(), 即真正的 swing-high 判定。回测引擎须与生产一致。
    """
    if i < 1:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


# ===================================================================
#  gate_buy — 买点门控（B 信号放行判定）
# ===================================================================

def gate_buy(g_factor, m_factor, g_dev, i, *,
             macd_gate_mode='strict',
             c=None, lo=None,
             floor_dev_pct=_DEFAULT_FLOOR_DEV_PCT,
             local_w=_DEFAULT_LOCAL_W,
             floor_buy_cooldown_bars=0,
             last_buy_floor_bar=-999,
             trend_state=0,
             floor_trend_threshold=2.0,
             floor_suppress_buy_day_chg=DEFAULT_FLOOR_SUPPRESS_BUY_DAY_CHG,
             day_chg=0.0,
             resonance_score=None,
             min_resonance=None):
    """买点门控：判断当前 bar 是否触发买信号。

    返回: (buy_pass, buy_base, buy_floor)
      - buy_pass : 最终是否放行买
      - buy_base : strict 基础条件是否满足（不含 floor 叠加）
      - buy_floor : 是否由价格地板触发
    """
    # ---- 基础门控 ----
    if macd_gate_mode == 'off':
        buy_base = (g_factor == 1)
    elif macd_gate_mode == 'resonance':
        # v9.3.0 三因子共振：>=min_resonance 个同向因子同时满足才放行
        if i < local_w:
            buy_base = (g_factor == 1)   # 早盘数据不足，降级 gravity-only
        else:
            buy_base = (resonance_score is not None and min_resonance is not None
                        and resonance_score >= min_resonance)
    elif macd_gate_mode in ('strict', 'floor'):
        if i < local_w:
            buy_base = (g_factor == 1)   # 早盘降级 gravity-only
        else:
            buy_base = (m_factor == 1)
    else:
        buy_base = False

    # ---- floor 叠加：价格地板 ----
    buy_floor = False
    if macd_gate_mode == 'floor':
        # 冷却期检查
        if floor_buy_cooldown_bars > 0 and i - last_buy_floor_bar <= floor_buy_cooldown_bars:
            pass  # 冷却中，不触发价格地板
        else:
            # 趋势缩放（预留）：强下跌时收紧地板阈值
            effective_dev_pct = floor_dev_pct
            if trend_state == -1 and floor_trend_threshold > 0:
                effective_dev_pct = floor_dev_pct * 1.5  # 强下跌时地板需更深

            buy_floor = (_is_new_low(c, lo, i, w=local_w)
                         and (g_dev <= -effective_dev_pct))

            # 跌日 B 通道抑制（对称于 floor_suppress_day_chg 的涨日 S 抑制）：
            # 日内跌幅≥阈值时关闭价格地板 B 通道（防接飞刀），但 MACD 背离 B(buy_base) 不受影响。
            # 仅 floor 模式生效；strict/off 模式 buy_floor 恒为 False，无副作用。
            if floor_suppress_buy_day_chg != 0.0 and day_chg <= floor_suppress_buy_day_chg:
                buy_floor = False

    buy_pass = bool(buy_base or buy_floor)
    return buy_pass, buy_base, buy_floor


# ===================================================================
#  gate_sell — 卖点门控（S 信号放行判定）
# ===================================================================

def gate_sell(g_factor, m_factor, g_dev, i, *,
              macd_gate_mode='strict',
              c=None, h=None,
              floor_dev_pct=_DEFAULT_FLOOR_DEV_PCT,
              local_w=_DEFAULT_LOCAL_W,
              floor_sell_cooldown_bars=0,
              last_sell_ceil_bar=-999,
              trend_state=0,
              floor_trend_threshold=2.0,
              floor_suppress_day_chg=20.0,
              day_chg=0.0,
              resonance_score=None,
              min_resonance=None):
    """卖点门控：判断当前 bar 是否触发卖信号。

    返回: (sell_pass, sell_base, sell_ceil)
      - sell_pass : 最终是否放行卖
      - sell_base : strict 基础条件是否满足
      - sell_ceil : 是否由价格天花板触发
    """
    # ---- 基础门控 ----
    if macd_gate_mode == 'off':
        sell_base = (g_factor == -1)
    elif macd_gate_mode == 'resonance':
        # v9.3.0 三因子共振：>=min_resonance 个同向因子同时满足才放行
        if i < local_w:
            sell_base = (g_factor == -1)   # 早盘数据不足，降级 gravity-only
        else:
            sell_base = (resonance_score is not None and min_resonance is not None
                         and resonance_score >= min_resonance)
    elif macd_gate_mode in ('strict', 'floor'):
        if i < local_w:
            sell_base = (g_factor == -1)
        else:
            sell_base = (m_factor == -1)
    else:
        sell_base = False

    # ---- floor 叠加：价格天花板 ----
    sell_ceil = False
    if macd_gate_mode == 'floor':
        # 涨停抑制：日涨幅≥阈值 → 关闭价格天花板 S 通道
        if floor_suppress_day_chg > 0 and day_chg >= floor_suppress_day_chg:
            pass  # 涨停日关闭
        elif floor_sell_cooldown_bars > 0 and i - last_sell_ceil_bar <= floor_sell_cooldown_bars:
            pass  # 冷却中
        else:
            # 趋势缩放（预留）：强上涨时收紧天花板阈值
            effective_dev_pct = floor_dev_pct
            if trend_state == 1 and floor_trend_threshold > 0:
                effective_dev_pct = floor_dev_pct * 1.5  # 强上涨时天花板需更高

            sell_ceil = (_is_new_high(c, h, i, w=local_w)
                         and (g_dev >= effective_dev_pct))

    sell_pass = bool(sell_base or sell_ceil)
    return sell_pass, sell_base, sell_ceil
