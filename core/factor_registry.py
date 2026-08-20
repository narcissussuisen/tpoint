"""core/factor_registry.py — 因子注册表（2026-08-18 Phase 2b）

统一因子接口：factor(o, h, lo, c, v) -> np.ndarray（逐 bar、因果、长度=n）。
所有因子必须是因果算子（仅依赖 c[0..i]），由 core/leak_guard.perturbation_test 统一守护。
Phase 3 因子演化引擎以本注册表为候选空间，池级目标函数在 core/pool_eval.py。

[v10.2.0 2026-08-20 新增] KDJ 因子 + 量价背离 + MACD 背离因子
"""
import numpy as np
from primitives import (ema, compute_atr, compute_vwap, compute_rsi,
                         compute_vol_ratio, compute_kdj)


def _macd(c, fast=12, slow=26, signal=9):
    ema_fast = ema(c, fast)
    ema_slow = ema(c, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def f_vwap(o, h, lo, c, v):
    return compute_vwap(h, lo, c, v)


def f_atr_pct(o, h, lo, c, v):
    atr = compute_atr(h, lo, c)
    return np.where(c > 0, atr / c * 100.0, 0.0)


def f_vol_ratio(o, h, lo, c, v):
    return compute_vol_ratio(v if (v is not None and np.sum(v) > 0) else None)


def f_rsi(o, h, lo, c, v):
    return compute_rsi(c)


def f_macd_hist(o, h, lo, c, v):
    _, _, hist = _macd(c)
    return hist


def f_macd_dif(o, h, lo, c, v):
    dif, _, _ = _macd(c)
    return dif


def f_gravity_dev(o, h, lo, c, v):
    vwap = compute_vwap(h, lo, c, v)
    return np.where(vwap > 0, (c - vwap) / vwap * 100.0, 0.0)


def f_trend_ema(o, h, lo, c, v):
    ef = ema(c, 20); es = ema(c, 60)
    return np.where(ef > es, 1.0, np.where(ef < es, -1.0, 0.0))


# ========== [v10.2.0 新增] KDJ 因子 ==========

def f_kdj_k(o, h, lo, c, v):
    k, _, _ = compute_kdj(h, lo, c)
    return k


def f_kdj_d(o, h, lo, c, v):
    _, d, _ = compute_kdj(h, lo, c)
    return d


def f_kdj_j(o, h, lo, c, v):
    _, _, j = compute_kdj(h, lo, c)
    return j


# ========== [v10.2.0 新增] 量价背离 / MACD 背离因子 ==========

def _macd(c, fast=12, slow=26, signal=9):
    ema_fast = ema(c, fast)
    ema_slow = ema(c, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def f_vol_price_div(o, h, lo, c, v):
    """量价顶背离（神技#2，卖出风险信号）：
    当前 bar 创近 LOCAL_W 根新高，但成交量低于近 LOCAL_W 根均量的 DIV_RATIO 倍。
    返回 0/1 数组；v 缺失时全 0（保守不报）。

    用法：
      - S 信号：价新高 + 量缩 → 强卖信号
      - B 信号：价新低 + 量缩 → 反弹信号（底部背离）
    """
    n = len(c)
    LOCAL_W = 15
    DIV_RATIO = 0.7
    out = np.zeros(n, dtype=float)
    if v is None or np.sum(v) <= 0:
        return out
    v = np.asarray(v, dtype=float)
    c_arr = np.asarray(c, dtype=float)
    for i in range(LOCAL_W, n):
        w_start = i - LOCAL_W
        win = c_arr[w_start:i + 1]
        v_win = v[w_start:i + 1]
        local_high = win.max()
        local_low = win.min()
        avg_v = v_win.mean()
        # 顶背离：当前价=近 LOCAL_W 高 + 量 < 均量 * DIV_RATIO
        if c_arr[i] >= local_high and v[i] < avg_v * DIV_RATIO:
            out[i] = 1.0
        # 底背离：当前价=近 LOCAL_W 低 + 量 < 均量 * DIV_RATIO → 用负数标记（双向信号）
        elif c_arr[i] <= local_low and v[i] < avg_v * DIV_RATIO:
            out[i] = -1.0
    return out


def f_macd_div(o, h, lo, c, v):
    """分时 MACD 背离（神技#3，买/卖确认信号）：
    顶背离（S）：近 LOCAL_W 根新高 + MACD 绿柱（hist<0）放大
    底背离（B）：近 LOCAL_W 根新低 + MACD 红柱（hist>0）缩短
    返回 {-1, 0, 1} 数组（-1=底背离=B 候选；+1=顶背离=S 候选）。
    """
    n = len(c)
    LOCAL_W = 15
    _, _, hist = _macd(c)
    out = np.zeros(n, dtype=float)
    c_arr = np.asarray(c, dtype=float)
    for i in range(LOCAL_W, n):
        w_start = i - LOCAL_W
        win = c_arr[w_start:i + 1]
        local_high = win.max()
        local_low = win.min()
        # 顶背离（S 候选）：价新高 + 绿柱放大（hist<0 且 hist 越来越负）
        if c_arr[i] >= local_high and hist[i] < 0 and i >= 2 and hist[i] < hist[i - 1] < hist[i - 2]:
            out[i] = 1.0
        # 底背离（B 候选）：价新低 + 红柱缩短（hist>0 且 hist 在减小）
        elif c_arr[i] <= local_low and hist[i] > 0 and i >= 2 and hist[i] < hist[i - 1] < hist[i - 2]:
            out[i] = -1.0
    return out


# 注册表：候选因子空间（Phase 3 演化引擎在此增删因子）
FACTORS = {
    'vwap': f_vwap,
    'atr_pct': f_atr_pct,
    'vol_ratio': f_vol_ratio,
    'rsi': f_rsi,
    'macd_hist': f_macd_hist,
    'macd_dif': f_macd_dif,
    'gravity_dev': f_gravity_dev,
    'trend_ema': f_trend_ema,
    # [v10.2.0 新增]
    'kdj_k': f_kdj_k,
    'kdj_d': f_kdj_d,
    'kdj_j': f_kdj_j,
    'vol_price_div': f_vol_price_div,
    'macd_div': f_macd_div,
}


def compute_factors(o, h, lo, c, v):
    """计算全部注册因子，返回 {name: array}。"""
    return {name: fn(o, h, lo, c, v) for name, fn in FACTORS.items()}


def factor_feat():
    """返回 (o,h,lo,c,v) -> {name: array} 的 feat_fn，供 leak_guard.perturbation_test 守护。"""
    def _feat(o, h, lo, c, v):
        return compute_factors(o, h, lo, c, v)
    return _feat
