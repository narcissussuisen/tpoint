"""core/general_signal.py — tpoint 通用算法（全标的适用的做T信号引擎）

=================================================================================
设计定位（2026-08-20 完善方案落地）：
---------------------------------------------------------------------------------
本模块是 tpoint 的**通用算法（general algorithm）**——一套**符号无关（symbol-agnostic）**
的连续评分做T引擎，作为 watchlist 的统一驱动，替代原先偏 miji 布尔触发的监控入口。

核心特性：
  1. **全标的适用**：所有参数均为**比率/相对口径**（tanh(VWAP偏离/ATR)、RSI 中性映射、
     MACD 柱状收敛度、量比缩量度），不依赖任何逐标的硬编码阈值 → 同一套配置驱动
     任意 A 股 1m 标的（主板/科创板/ETF/LOF）。
  2. **实时安全（无后视）**：全部组件仅依赖 [0..i]（EMA/ADR/RSI/VWAP/KDJ/MACD/量比均为
     因果前向算子），trend 由 EMA+ADX 因果判定（indicators.compute_indicators）。
  3. **双向做T（修复 v4 单向死锁）**：
       - B 侧「防接飞刀」：trend∈{0,1}（震荡/上升）正常放 B；trend==-1（确认下跌）仅当
         局部底+超卖反转确认才放 B，避免接飞刀。
       - S 侧全 regime 放行（composite ≤ -sell_threshold），与生产 s_signal_exit 口径一致。
  4. **强度分级 + 质量分**：每条信号带 score / strength / strength_band / components / triggers，
     供评分审计、ML 融合与回测。

与既有代码关系：
  - 复用 composite_scorer 的 CompositeConfig / score_components_at / _macd_hist（连续评分内核）。
  - 兼容 detect_signals_v2/v3/v4 信号格式（type/idx/price/reason/score/...），可直接喂
    exit_manager.simulate_day（正T）与 simulate_bidirectional（反T）。
  - 同时提供「批量信号检测」(detect_signals_general) 与「单 bar 触发」接口
    (check_general_b_trigger / check_general_s_trigger)，后者签名对齐 miji 的
    check_b_trigger / check_s_trigger，供 monitor.detect_for 热插拔替换（flag 门控 + miji 兜底）。
=================================================================================
"""
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

import numpy as np

from composite_scorer import CompositeConfig, score_components_at, _macd_hist
from primitives import compute_rsi


# ========== 版本标识（2026-08-20 统一命名：做T策略 v5 / 引擎 GT v1.0） ==========
# 版本脉络：v3(v10.2.0, 太噪) → v4(composite_scorer, B死锁) → v5 = 通用算法 GT 驱动的做T策略。
# 引擎名 GT（General T-maker）与策略版本解耦；GT v1.0 = 当前 symbol-agnostic 连续评分引擎。
STRATEGY_VERSION = 'v5'
ENGINE_NAME = 'GT'
ENGINE_VERSION = '1.0'
ENGINE_FULL = f'{ENGINE_NAME}-{ENGINE_VERSION}'   # "GT-1.0"
# 兼容旧标识：general_signals_*.json 的 engine 字段沿用 "general"，另加 strategy_version 字段。


@dataclass
class GeneralConfig(CompositeConfig):
    """通用算法全部可配参数（继承 v4 连续评分内核，增加双向门控）。

    默认阈值相比 v4full 略敏感（0.45 vs 0.50）、节奏更密（gap 6 vs 8），
    以适配「全标的通用」而非针对单一标的过拟合；均为比率口径，跨标的稳健。
    """
    # —— B 侧下跌市防接飞刀（核心：修复 v4 单向死锁）——
    b_downtrend_reversal: bool = True   # True: trend==-1 时需反转确认才放 B
    b_rev_rsi: float = 0.35             # 反转确认的 RSI 分量阈值（C_rsi > 此值≈超卖）
    b_rev_w: int = 15                   # 局部底回望窗口（分钟）

    # —— S 侧「防卖飞」保护（默认关，与生产全 regime 放行一致）——
    s_uptrend_guard: bool = False       # True: trend==1 时需反转确认才放 S
    s_rev_rsi: float = 0.65             # 反转确认的 RSI 分量阈值（C_rsi < 此值≈超买）
    s_rev_w: int = 15                   # 局部顶回望窗口

    # —— 通用算法默认节奏（全标的通用，略密于 v4full）——
    buy_threshold: float = 0.45
    sell_threshold: float = 0.45
    signal_gap: int = 6
    max_b: int = 12
    max_s: int = 12
    start_idx: int = 2

    # —— 可选量能确认门控（默认关=纯通用；如需对齐生产 vol_ratio_b_max 可设 1.2）——
    vol_ratio_b_max: Optional[float] = None

    # —— [P4] regime 门控（更高时段平滑趋势，抑制接飞刀）——
    # 与 per-bar b_downtrend_reversal 区别：后者仅在 trend==-1 时要求局部底+超卖才放 B；
    # 本门控在「平滑趋势持续下行」(窗口内 -1 占比超阈) 时直接抑制 B（含超卖反弹），
    # 因持续下行 regime 中抄底反弹多失败（用户关注 2026H1 dip-buying 失效局部 regime）。
    # 仅影响 B（正T 方向）；S 全 regime 放行（与 s_signal_exit 口径一致）。OOS 验证后默认关。
    regime_gate: bool = False
    regime_lookback: int = 40          # 平滑窗口（分钟 bar 数）
    regime_downtrend_suppress: bool = True
    regime_downtrend_thresh: float = 0.5  # 窗口内 -1 占比阈值（≥则抑制 B）


GENERAL_DEFAULT = GeneralConfig()


# ========== 单 bar 触发接口（对齐 miji check_b_trigger / check_s_trigger） ==========

def _composite_at(data, i, cfg: GeneralConfig, rsi_arr, macd_hist_arr):
    cv, cvd, cmd, cr = score_components_at(data, i, cfg, rsi_arr, macd_hist_arr)
    ws = cfg.weight_sum()
    comp = (cfg.w_vwap * cv + cfg.w_vol_div * cvd + cfg.w_macd_div * cmd + cfg.w_rsi * cr) / ws if ws > 0 else 0.0
    return comp, cv, cvd, cmd, cr


def _regime_suppress_b(data, i, cfg: GeneralConfig) -> bool:
    """[P4] regime 门控：sustained downtrend 抑制 B。返回 True=应抑制 B。

    基于 data['trend'] 在 regime_lookback 窗口内的平滑方向（占比超阈即判定持续下行）。
    仅作用于 B 侧；data 无 trend 字段或窗口不足时退化为不抑制（fail-open）。
    """
    if not (cfg.regime_gate and cfg.regime_downtrend_suppress):
        return False
    tr = data.get('trend')
    if tr is None:
        return False
    L = max(2, int(cfg.regime_lookback))
    lo = max(0, i - L + 1)
    seg = tr[lo:i + 1]
    if len(seg) < max(2, int(L * 0.5)):
        return False
    frac_down = float((seg == -1).sum()) / len(seg)
    return frac_down >= cfg.regime_downtrend_thresh


def check_general_b_trigger(data, i, cfg: GeneralConfig = GENERAL_DEFAULT,
                            mpr_enable=None, mpr_periods=None, atr_min_pct=None,
                            vol_ratio_b_max=None, **_ignored) -> Tuple[bool, str]:
    """通用算法 B 触发（对齐 check_b_trigger 签名）。

    返回 (bool, reason)。通用算法自包含，mpr/atr 入参忽略（symbol-agnostic 不叠加逐标的过滤）。
    """
    n = data['n']
    if i < 2 or i >= n:
        return False, ''
    if data.get('atr') is not None and data['atr'][i] <= 0:
        return False, ''
    rsi_arr = data['rsi'] if 'rsi' in data else compute_rsi(data['c'], cfg.rsi_period)
    macd_hist_arr = _macd_hist(data['c'], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    comp, cv, cvd, cmd, cr = _composite_at(data, i, cfg, rsi_arr, macd_hist_arr)
    trend_i = int(data['trend'][i]) if 'trend' in data else 0

    # [P4] regime 门控：持续下行 regime 直接抑制 B（含超卖反弹），防接飞刀
    if _regime_suppress_b(data, i, cfg):
        return False, ''

    # 可选量能确认（默认关）
    _vrb = vol_ratio_b_max if vol_ratio_b_max is not None else cfg.vol_ratio_b_max
    if _vrb is not None and data.get('vol_ratio') is not None and data['vol_ratio'][i] > _vrb:
        return False, ''

    if comp < cfg.buy_threshold:
        return False, ''
    # B 侧下跌市防接飞刀
    if trend_i == -1 and cfg.b_downtrend_reversal:
        is_local_bottom = False
        if i > cfg.b_rev_w:
            w0 = max(0, i - cfg.b_rev_w)
            is_local_bottom = data['lo'][i] <= data['lo'][w0:i].min() + 1e-9
        oversold = cr > cfg.b_rev_rsi
        if not (is_local_bottom and oversold):
            return False, ''
    trig = ", ".join(t for t, v in (('vwap', cv), ('vol_div', cvd), ('macd_div', cmd), ('rsi', cr)) if abs(v) > cfg.trigger_eps) or "综合分达标"
    return True, f"通用算法综合{comp:+.2f}[B] {trig}"


def check_general_s_trigger(data, i, cfg: GeneralConfig = GENERAL_DEFAULT,
                            **_ignored) -> Tuple[bool, str]:
    """通用算法 S 触发（对齐 check_s_trigger 签名）。

    返回 (bool, reason)。默认全 regime 放行（与生产 s_signal_exit 口径一致）；
    若开启 s_uptrend_guard 则趋势市需反转确认（防卖飞）。
    """
    n = data['n']
    if i < 2 or i >= n:
        return False, ''
    if data.get('atr') is not None and data['atr'][i] <= 0:
        return False, ''
    rsi_arr = data['rsi'] if 'rsi' in data else compute_rsi(data['c'], cfg.rsi_period)
    macd_hist_arr = _macd_hist(data['c'], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    comp, cv, cvd, cmd, cr = _composite_at(data, i, cfg, rsi_arr, macd_hist_arr)
    trend_i = int(data['trend'][i]) if 'trend' in data else 0

    if comp > -cfg.sell_threshold:
        return False, ''
    # 可选「防卖飞」：趋势市(trend==1)需局部顶+超买反转确认
    if trend_i == 1 and cfg.s_uptrend_guard:
        is_local_top = False
        if i > cfg.s_rev_w:
            w0 = max(0, i - cfg.s_rev_w)
            is_local_top = data['h'][i] >= data['h'][w0:i].max() - 1e-9
        overbought = cr < cfg.s_rev_rsi
        if not (is_local_top and overbought):
            return False, ''
    trig = ", ".join(t for t, v in (('vwap', cv), ('vol_div', cvd), ('macd_div', cmd), ('rsi', cr)) if abs(v) > cfg.trigger_eps) or "综合分达标"
    return True, f"通用算法综合{comp:+.2f}[S] {trig}"


# ========== 批量信号检测（与 v2/v3/v4 接口兼容，供回测/灰度/引擎批量用） ==========

def detect_signals_general(data, pc, cfg: GeneralConfig = GENERAL_DEFAULT,
                           start_idx: int = 2, max_b: int = 12, max_s: int = 12):
    """通用算法批量信号检测（symbol-agnostic）。

    返回与 detect_signals_v4 / v4full 完全兼容的信号列表（含 score/strength/components/triggers）。
    """
    if pc <= 0:
        return []
    n = data['n']; c = data['c']
    if n < start_idx + 1:
        return []
    rsi_arr = data['rsi'] if 'rsi' in data else compute_rsi(c, cfg.rsi_period)
    macd_hist_arr = _macd_hist(c, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)

    start = max(start_idx, cfg.start_idx, 2)
    sigs: List[Dict[str, Any]] = []
    b_last = -999; s_last = -999; bc = 0; sc = 0
    gap = cfg.signal_gap
    ws = cfg.weight_sum()
    Wb = cfg.b_rev_w

    for i in range(start, n):
        cv, cvd, cmd, cr = score_components_at(data, i, cfg, rsi_arr, macd_hist_arr)
        composite = (cfg.w_vwap * cv + cfg.w_vol_div * cvd + cfg.w_macd_div * cmd + cfg.w_rsi * cr) / ws if ws > 0 else 0.0
        trend_i = int(data['trend'][i]) if 'trend' in data else 0

        emit = None
        if (composite >= cfg.buy_threshold and bc < max_b
                and (i - b_last) >= gap and (i - s_last) >= gap):
            if trend_i == -1 and cfg.b_downtrend_reversal:
                is_local_bottom = False
                if i > Wb:
                    w0 = max(0, i - Wb)
                    is_local_bottom = data['lo'][i] <= data['lo'][w0:i].min() + 1e-9
                if is_local_bottom and (cr > cfg.b_rev_rsi):
                    emit = 'B'
                # 否则防接飞刀拦截
            else:
                emit = 'B'
            # [P4] regime 门控：持续下行 regime 抑制 B（含超卖反弹），防接飞刀
            if emit == 'B' and _regime_suppress_b(data, i, cfg):
                emit = None
        elif (composite <= -cfg.sell_threshold and sc < max_s
              and (i - s_last) >= gap and (i - b_last) >= gap):
            if trend_i == 1 and cfg.s_uptrend_guard:
                is_local_top = False
                if i > cfg.s_rev_w:
                    w0 = max(0, i - cfg.s_rev_w)
                    is_local_top = data['h'][i] >= data['h'][w0:i].max() - 1e-9
                if is_local_top and (cr < cfg.s_rev_rsi):
                    emit = 'S'
                # 否则防卖飞拦截
            else:
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
            'vol_ratio': round(float(data['vol_ratio'][i]), 2) if data.get('vol_ratio') is not None else None,
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
