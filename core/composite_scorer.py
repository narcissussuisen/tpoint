"""core/composite_scorer.py — 综合评分信号模型 (tpoint v10.3.0)
=================================================================================
将「三大核心策略 + RSI 超买超卖」整合为统一的连续评分引擎：

    三大核心策略（出处：v14《散户专属做T秘籍》/ 方法论 v1.x §4）
      1) 分时均线引力（VWAP Gravity）    —— 价偏离 VWAP±K·ATR 的均值回归连续分
      2) 量价背离（Vol-Price Divergence）—— 价极值 + 缩量的动能衰竭连续分
      3) 分时 MACD 背离（MACD Divergence）—— 价极值 + 柱状收敛/放大的连续分
    + RSI 超买超卖（Overbought/Oversold）  —— 线性映射连续分

引擎逻辑（与 v3 布尔触发本质不同）：
    v3 (detect_signals_v3) 用「多条件 AND」布尔触发 → 离散 0/1 信号，易信号爆发/漏触；
    本模型 (detect_signals_v4) 每个组件输出连续子评分 C ∈ [-1,1]（方向×强度），
    加权求和得综合分 composite = Σ w·C / Σ w ∈ [-1,1]，
    综合分越阈值才出信号 → 天然支持「强度分级 / 多因子融合 / 权重可配」，
    更接近生产可用的评分引擎，且可通过阈值直接控信号密度。

计算逻辑（全部因果前向，无未来函数）：
    C_vwap    = -tanh( (close - vwap) / (k1·atr) )        # 价低于带→正(买)，高于带→负(卖)
    C_vol_div = +shrink(买底背离) / -shrink(卖顶背离)      # shrink∈[0,1] 由量缩程度决定
    C_macd_div= +strength(买底背离) / -strength(卖顶背离)  # strength∈[0,1] 由 MACD 不确认程度
    C_rsi     = clip( (rsi_neutral - rsi) / half_range, -1, 1 )
    composite = (w_vwap·C_vwap + w_vol_div·C_vol_div + w_macd_div·C_macd_div + w_rsi·C_rsi) / Σw
    信号：composite ≥ buy_threshold → B；composite ≤ -sell_threshold → S；否则 HOLD
    强度：strength = |composite|，分 strong(≥0.6) / medium(≥0.45) / weak 三档
    触发条件：triggers = 所有 |C|>trigger_eps 组件的有符号贡献，供审计/解释

结构化输出（每条信号）：
    type, idx, price, score(综合分,带符号), strength(|score|), strength_band,
    rsi, trend, reason, vol_ratio, components{vwap,vol_div,macd_div,rsi}, weights{...}, triggers[...]

参数全可配（见 CompositeConfig / DEFAULT_CONFIG）。默认权重：
    w_vwap=1.2（生产验证的均值回归主因子） > w_macd_div=0.9 > w_rsi=0.8 > w_vol_div=0.7
    （量价背离实证净负，刻意低配）。
阈值默认 buy/sell=0.35，强度档 0.60/0.45，趋势门控沿用 v2 生产口径。

与既有代码关系：纯新增模块，不修改 v9 / v2 / v3 / monitor / exit_manager。
detect_signals_v4 返回格式与 v2/v3 兼容（含 type/idx/price/reason），可直接喂 exit_manager.simulate_day。
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

import numpy as np

from primitives import ema, compute_rsi


# ========== 配置 ==========

@dataclass
class CompositeConfig:
    """综合评分模型全部可配参数。默认值见 DEFAULT_CONFIG。

    权重用于综合分加权（自动归一化，故权重绝对值只表征相对重要性）；
    信号类参数控制出信号门槛与密度；策略类参数控制各组件计算口径。
    """
    # —— RSI 超买超卖 ——
    rsi_period: int = 14            # RSI 计算周期（可配；默认 14，与 v9 一致）
    rsi_oversold: float = 35.0      # RSI 超卖线（≤ 此值 → 买入分趋 +1）
    rsi_overbought: float = 65.0    # RSI 超买线（≥ 此值 → 卖出分趋 -1）
    rsi_neutral: float = 50.0       # RSI 中性线（映射零点）

    # —— 策略权重（综合分加权，自动归一化）——
    w_vwap: float = 1.2             # 神技#1 均线引力 权重
    w_vol_div: float = 0.7          # 神技#2 量价背离 权重
    w_macd_div: float = 0.9         # 神技#3 MACD 背离 权重
    w_rsi: float = 0.8             # RSI 超买超卖 权重

    # —— 神技#1 均线引力 ——
    vwap_k1: float = 0.8            # 标准轨倍数（价偏离此即进入回归区，沿用 K1_V2）
    vwap_k2: float = 1.8            # 极端轨倍数（仅用于解释，不参与 tanh 饱和）
    # —— 神技#2 量价背离 ——
    div_local_w: int = 15           # 局部极值窗口（分钟）
    div_vol_ratio: float = 0.7      # 量缩阈值（当前量比 < 此值视为缩量 → 背离成立）
    # —— 神技#3 MACD 背离 ——
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # —— 信号/强度阈值 ——
    # 默认 0.50 落在方法论 §8 健康信号密度带(0.5~2.0 信号/百bar)附近；
    # 想更灵敏可降到 0.35（密度↑但易超带），想更严控可升到 0.55（密度↓至 ~1.6）。
    buy_threshold: float = 0.50      # 综合分 ≥ 此值 → 出 B
    sell_threshold: float = 0.50    # 综合分 ≤ -此值 → 出 S
    strong_band: float = 0.62       # |score| ≥ 此值 → 强度 strong
    medium_band: float = 0.50       # |score| ≥ 此值 → 强度 medium（否则 weak）
    trigger_eps: float = 0.02       # 组件贡献绝对值 > 此值才计入 triggers 明细

    # —— 趋势门控（沿用 v2 生产口径，可配）——
    trend_b_allowed: tuple = (1,)            # 允许出 B 的 trend 取值（默认仅上升市）
    trend_s_allowed: tuple = (-1, 0, 1)     # 允许出 S 的 trend 取值（默认全放行）

    # —— 信号节奏（防同段行情两面抓 + 控密度，沿用 v2）——
    signal_gap: int = 8               # 同型 + 跨型最小间隔（分钟）
    max_b: int = 12
    max_s: int = 12
    start_idx: int = 2

    def weight_sum(self) -> float:
        return self.w_vwap + self.w_vol_div + self.w_macd_div + self.w_rsi

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # tuple 不可 JSON 序列化，转 list
        d["trend_b_allowed"] = list(self.trend_b_allowed)
        d["trend_s_allowed"] = list(self.trend_s_allowed)
        return d


DEFAULT_CONFIG = CompositeConfig()


def _macd_hist(c, fast, slow, signal):
    """MACD 柱状（因果前向）：hist = (DIF - DEA) * 2。"""
    ema_f = ema(c, fast)
    ema_s = ema(c, slow)
    dif = ema_f - ema_s
    dea = ema(dif, signal)
    return (dif - dea) * 2.0


# ========== 单 bar 组件评分 ==========

def score_components_at(data, i, cfg: CompositeConfig, rsi_arr, macd_hist_arr):
    """计算第 i 根 bar 的四个组件连续分，返回 (C_vwap, C_vol_div, C_macd_div, C_rsi)。

    全部仅依赖 [0..i]（因果前向，无未来函数）。atr<=0 或边界处组件置 0。
    """
    c = data['c']; h = data['h']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']
    has_vol = data['has_vol']; vr = data['vol_ratio']
    W = cfg.div_local_w

    # ---- C_vwap：均线引力（均值回归连续分）----
    if atr[i] > 0:
        dev = (c[i] - vwap[i]) / (cfg.vwap_k1 * atr[i])
        c_vwap = -float(np.tanh(dev))
    else:
        c_vwap = 0.0

    # ---- C_vol_div：量价背离（价极值 + 缩量）----
    c_vol_div = 0.0
    if i > W:
        w0 = max(0, i - W)
        local_bot = lo[i] <= lo[w0:i].min() + 1e-9
        local_top = h[i] >= h[w0:i].max() - 1e-9
        shrink = 0.0
        if has_vol and vr[i] < cfg.div_vol_ratio:
            shrink = float(np.clip((cfg.div_vol_ratio - vr[i]) / cfg.div_vol_ratio, 0.0, 1.0))
        if shrink > 0:
            if local_bot:
                c_vol_div = +shrink
            elif local_top:
                c_vol_div = -shrink

    # ---- C_macd_div：MACD 背离（价极值 + 柱状不确认）----
    c_macd_div = 0.0
    if i > W:
        w0 = max(0, i - W)
        local_bot = lo[i] <= lo[w0:i].min() + 1e-9
        local_top = h[i] >= h[w0:i].max() - 1e-9
        m_seg = macd_hist_arr[w0:i + 1]
        m_min = float(m_seg.min()); m_max = float(m_seg.max())
        if local_bot and macd_hist_arr[i] > m_min:
            strength = float(np.clip((macd_hist_arr[i] - m_min) / (abs(m_min) + 1e-12), 0.0, 1.0))
            c_macd_div = +strength
        elif local_top and macd_hist_arr[i] < m_max:
            strength = float(np.clip((m_max - macd_hist_arr[i]) / (abs(m_max) + 1e-12), 0.0, 1.0))
            c_macd_div = -strength

    # ---- C_rsi：RSI 超买超卖（线性映射）----
    half = (cfg.rsi_overbought - cfg.rsi_oversold) / 2.0
    if half <= 0:
        half = 15.0
    c_rsi = float(np.clip((cfg.rsi_neutral - rsi_arr[i]) / half, -1.0, 1.0))

    return c_vwap, c_vol_div, c_macd_div, c_rsi


def score_bar(data, i, cfg: CompositeConfig = DEFAULT_CONFIG, rsi_arr=None, macd_hist_arr=None):
    """公开：计算单 bar 的完整评分明细（供 monitor 实时打分 / 解释用）。

    返回 dict: {vwap, vol_div, macd_div, rsi, composite, weight_sum}。
    若未传 rsi_arr / macd_hist_arr 则内部按 cfg 计算（调用方传入可省重复计算）。
    """
    c = data['c']
    if rsi_arr is None:
        rsi_arr = compute_rsi(c, cfg.rsi_period) if cfg.rsi_period != 14 else data['rsi']
    if macd_hist_arr is None:
        macd_hist_arr = _macd_hist(c, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    cv, cvd, cmd, cr = score_components_at(data, i, cfg, rsi_arr, macd_hist_arr)
    ws = cfg.weight_sum()
    composite = (cfg.w_vwap * cv + cfg.w_vol_div * cvd + cfg.w_macd_div * cmd + cfg.w_rsi * cr) / ws if ws > 0 else 0.0
    return {'vwap': cv, 'vol_div': cvd, 'macd_div': cmd, 'rsi': cr,
            'composite': float(composite), 'weight_sum': float(ws)}


# ========== 批量信号检测（与 v2/v3 接口兼容） ==========

def detect_signals_v4(data, pc, cfg: CompositeConfig = DEFAULT_CONFIG,
                      start_idx: int = 2, max_b: int = 12, max_s: int = 12):
    """v4 综合评分信号检测（2026-08-20 引入，v10.3.0）。

    将三大神技 + RSI 整合为连续评分，综合分越阈值出信号。
    返回与 v2/v3 兼容的信号列表（含 type/idx/price/reason），
    额外含 score/strength/strength_band/components/weights/triggers 供评分审计与回测。

    参数：
      data   : compute_indicators(...) 输出（含 vwap/atr/trend/rsi/kdj/vol_ratio）
      pc     : 昨收（<=0 直接返回空）
      cfg    : CompositeConfig（可配）；默认 DEFAULT_CONFIG
      max_b/max_s/start_idx : 节奏参数（也可经 cfg 设）
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

    for i in range(start, n):
        cv, cvd, cmd, cr = score_components_at(data, i, cfg, rsi_arr, macd_hist_arr)
        composite = (cfg.w_vwap * cv + cfg.w_vol_div * cvd + cfg.w_macd_div * cmd + cfg.w_rsi * cr) / ws if ws > 0 else 0.0
        trend_i = int(data['trend'][i])

        # 信号判定（带趋势门控 + 跨型冷却 + 日上限）
        emit = None
        if (composite >= cfg.buy_threshold and bc < max_b
                and (i - b_last) >= gap and (i - s_last) >= gap
                and trend_i in cfg.trend_b_allowed):
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

        # 触发条件明细（有符号贡献）
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
