"""core/composite_scorer_v4full.py — v4 完整做T策略逻辑（修复 B 侧死锁 + 防接飞刀）

=================================================================================
修复动机（根因，2026-08-20 实证）：
---------------------------------------------------------------------------------
原 composite_scorer.DEFAULT_CONFIG 的 B 侧：
    trend_b_allowed=(1,)  # 仅在「上升市(trend==1)」才允许出 B
但 v4 的 composite 是**均值回归连续评分**：
    C_vwap = -tanh((close-vwap)/(k1*atr))  → 价低于VWAP(下跌) 才为正(买)
    C_rsi  = (neutral-rsi)/half            → 超卖(下跌) 才为正(买)
    C_vol_div / C_macd_div                  → 底背离(下跌段) 才为正(买)
→ composite 的高分(≥0.50) 恰好出现在**下跌市(trend==-1)**，
  而 trend==1(上升市) 时价高于VWAP、RSI偏高 → composite 区间峰值仅 0.34~0.36，
  永远够不到 buy_threshold=0.50。
→ 两闸门**方向相反、时点错配** → B 在数学上恒为 0（603039/688111 实测 0 枚）。

注意：trend 由 EMA/ADX 因果算子计算（仅依赖 [0..i]），**实时可判、无后视**。
      问题不在看后视镜，而在 B 侧「评分方向」与「门控方向」自相矛盾。

修复方案（对齐生产 b_trend_filter 语义 + 补全缺失安全）：
---------------------------------------------------------------------------------
采用「防接飞刀」而非「要求上升市」：
  - trend ∈ {0, 1}（震荡 / 上升）：正常放 B（均值回归买点本就在此类环境的回调里）。
  - trend == -1（确认下跌市）：仅当**反转确认**时才放 B，避免接飞刀：
        is_local_bottom = lo[i] 是近 W 根最低（局部底，非半空）
        oversold        = C_rsi > b_rev_rsi（RSI 已超卖拐头）
        二者齐备 → 视为「下跌段内的有效回调买点」，允许 B（平掉浮亏/降成本）。
  - 关闭保护(b_downtrend_reversal=False)即退化为团队 v4_tuned_config 的 [-1,0,1] 全放行。
S 侧：维持全 regime 放行（composite ≤ -sell_threshold），与 v2 生产口径一致。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any

import numpy as np

from composite_scorer import CompositeConfig, score_components_at, _macd_hist
from primitives import compute_rsi


@dataclass
class V4FullConfig(CompositeConfig):
    """在 CompositeConfig 基础上增加 B 侧下跌市反转保护开关。"""
    # —— B 侧下跌市防接飞刀 ——
    b_downtrend_reversal: bool = True   # True: trend==-1 时需反转确认才放 B
    b_rev_rsi: float = 0.35             # 反转确认的 RSI 分量阈值（C_rsi > 此值≈超卖）
    b_rev_w: int = 15                   # 局部底回望窗口（分钟，复用 div_local_w 口径）


V4FULL_DEFAULT = V4FullConfig()


def detect_signals_v4full(data, pc, cfg: V4FullConfig = V4FULL_DEFAULT,
                           start_idx: int = 2, max_b: int = 12, max_s: int = 12):
    """v4 完整策略信号检测（修复 B 死锁 + 防接飞刀）。

    返回与 detect_signals_v4 / v2 / v3 完全兼容的信号列表，额外含 score / components 等。
    兼容 simulate_day（正T: B→S）与 simulate_bidirectional（反T: S→B）。
    """
    if pc <= 0:
        return []
    n = data['n']; c = data['c']
    if n < start_idx + 1:
        return []
    rsi_arr = compute_rsi(c, cfg.rsi_period) if cfg.rsi_period != 14 else data['rsi']
    macd_hist_arr = _macd_hist(c, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)

    start = max(start_idx, cfg.start_idx, 2)
    sigs: List[Dict[str, Any]] = []
    b_last = -999; s_last = -999; bc = 0; sc = 0
    gap = cfg.signal_gap
    ws = cfg.weight_sum()
    W = cfg.b_rev_w

    for i in range(start, n):
        cv, cvd, cmd, cr = score_components_at(data, i, cfg, rsi_arr, macd_hist_arr)
        composite = (cfg.w_vwap * cv + cfg.w_vol_div * cvd + cfg.w_macd_div * cmd + cfg.w_rsi * cr) / ws if ws > 0 else 0.0
        trend_i = int(data['trend'][i])

        emit = None
        if (composite >= cfg.buy_threshold and bc < max_b
                and (i - b_last) >= gap and (i - s_last) >= gap):
            # —— B 侧趋势保护（核心修复）——
            if trend_i == -1 and cfg.b_downtrend_reversal:
                # 反转确认：局部底 + 超卖分量
                is_local_bottom = False
                if i > W:
                    w0 = max(0, i - W)
                    is_local_bottom = data['lo'][i] <= data['lo'][w0:i].min() + 1e-9
                oversold = cr > cfg.b_rev_rsi
                if is_local_bottom and oversold:
                    emit = 'B'
                # 否则（半空下跌 / 未超卖）拦截，防接飞刀
            else:
                # trend ∈ {0,1}：正常放 B（震荡/上升市的回调买点）
                emit = 'B'
        elif (composite <= -cfg.sell_threshold and sc < max_s
              and (i - s_last) >= gap and (i - b_last) >= gap
              and trend_i in cfg.trend_s_allowed):
            emit = 'S'
        if emit is None:
            continue

        strength = abs(composite)
        band = ('strong' if strength >= cfg.strong_band
                else 'medium' if strength >= cfg.medium_band else 'weak')

        triggers = []
        for name, val in (('vwap', cv), ('vol_div', cvd), ('macd_div', cmd), ('rsi', cr)):
            if abs(val) > cfg.trigger_eps:
                triggers.append({'name': name, 'dir': 1 if val > 0 else -1, 'val': round(float(val), 4)})
        trig_names = ", ".join(t['name'] for t in triggers) or "综合分达标"

        sigs.append({
            'type': emit,
            'idx': i,
            'price': round(float(c[i]), 2),
            'score': round(float(composite), 4),
            'strength': round(float(strength), 4),
            'strength_band': band,
            'rsi': round(float(rsi_arr[i]), 1),
            'trend': trend_i,
            'reason': f"综合{composite:+.2f}[{emit}] 触发: {trig_names}",
            'vol_ratio': round(float(data['vol_ratio'][i]), 2),
            'components': {
                'vwap': round(cv, 4), 'vol_div': round(cvd, 4),
                'macd_div': round(cmd, 4), 'rsi': round(cr, 4),
            },
            'weights': {'vwap': cfg.w_vwap, 'vol_div': cfg.w_vol_div,
                        'macd_div': cfg.w_macd_div, 'rsi': cfg.w_rsi},
            'triggers': triggers,
        })
        if emit == 'B':
            b_last = i; bc += 1
        else:
            s_last = i; sc += 1
    return sigs
