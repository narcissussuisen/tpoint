"""core/primitives.py — 因果技术指标原语（2026-08-18 Phase 2 单一因子源）

把 indicators.py(v9) 与 miji_alpha.py(miji) 中完全重复的因果原语收敛为唯一实现，
消除双栈复制粘贴导致的长期漂移风险（同源不分叉铁律）。

全部为前向因果算子（仅依赖 c[0..i]），受 core/leak_guard.perturbation_test 守护。

[v10.2.0 2026-08-20 新增] KDJ 原语（factor_registry / 量价+MACD 背离检测的输入）
"""
import numpy as np

ATR_PERIOD = 14
RSI_PERIOD = 14
VOL_LOOKBACK = 20
KDJ_N = 9       # RSV 回望窗口
KDJ_K_PERIOD = 3  # K 平滑周期
KDJ_D_PERIOD = 3  # D 平滑周期（用 K 的 SMA）


def ema(arr, period):
    """指数移动平均（因果：out[i] 仅依赖 arr[0..i]）。"""
    arr = np.asarray(arr, dtype=float)
    out = np.zeros_like(arr)
    if len(arr) == 0:
        return out
    k = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def compute_atr(h, lo, c, period=ATR_PERIOD):
    """Wilder ATR（因果：tr[i] 用 c[i-1]，非 c[i+1]）。"""
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float); c = np.asarray(c, dtype=float)
    n = len(c)
    tr = np.zeros(n)
    tr[0] = h[0] - lo[0]
    for i in range(1, n):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i-1]), abs(lo[i] - c[i-1]))
    atr = np.zeros(n)
    if n > period:
        atr[period] = tr[1:period+1].mean()
        for i in range(period+1, n):
            atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
        atr[:period] = atr[period]
    else:
        atr[:] = tr.mean() if tr.mean() > 0 else 0.0
    return atr


def compute_vwap(h, lo, c, v):
    """日内累计 VWAP（因果：cumsum 截至 i）。v 为 None 或全 0 时退化为等权均价。"""
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float)
    c = np.asarray(c, dtype=float); n = len(c)
    if v is not None and np.sum(v) > 0:
        v = np.asarray(v, dtype=float)
        tp = (h + lo + c) / 3.0
        cum_vp = np.cumsum(tp * v)
        cum_v = np.cumsum(v)
        return cum_vp / np.where(cum_v > 0, cum_v, 1.0)
    return np.cumsum(c) / np.arange(1, n + 1)


def compute_rsi(c, period=RSI_PERIOD):
    """RSI(period) Wilder（因果）。"""
    c = np.asarray(c, dtype=float)
    n = len(c)
    dlt = np.diff(c, prepend=c[0])
    g = np.where(dlt > 0, dlt, 0.0)
    l_arr = np.where(dlt < 0, -dlt, 0.0)
    ag = np.zeros(n); al = np.zeros(n)
    if n > period:
        ag[period] = g[1:period+1].mean()
        al[period] = l_arr[1:period+1].mean()
        for i in range(period+1, n):
            ag[i] = (ag[i-1] * (period-1) + g[i]) / period
            al[i] = (al[i-1] * (period-1) + l_arr[i]) / period
    rsi = np.where(al > 0, 100 - 100 / (1 + ag / np.where(al == 0, 1, al)), 50)
    return rsi


def compute_vol_ratio(v, lookback=VOL_LOOKBACK):
    """量比 = 当前量 / 过去 lookback bar 均量（因果：v[i-lookback:i]）。"""
    n = len(v) if v is not None else 0
    vr = np.ones(max(n, 1))
    if v is None:
        return vr
    v = np.asarray(v, dtype=float)
    for i in range(lookback, n):
        avg = v[i-lookback:i].mean()
        vr[i] = v[i] / avg if avg > 0 else 1.0
    return vr


# ========== [v10.2.0] KDJ 原语（因果；SSE 经典定义） ==========

def compute_kdj(h, lo, c, n=KDJ_N, k_period=KDJ_K_PERIOD, d_period=KDJ_D_PERIOD):
    """KDJ(N, K, D) — Stochastic 改版（因果前向；不读 i 之后数据）。

    定义（SSE 经典）：
      RSV[i] = (c[i] - min(lo[i-N+1:i+1])) / (max(h[i-N+1:i+1]) - min(lo[i-N+1:i+1])) * 100
      K[i]   = SMA(RSV, K_period)
      D[i]   = SMA(K,   D_period)
      J[i]   = 3*K - 2*D

    边界处理：前 N-1 根 N 不够 → 用 [0..i] 自身 max/min 兜底（不退化为 NaN）。
    边界处 max==min 时 RSV 设为 50（中性）；K/D 初始用 RSV[0] 起步。

    返回 (k, d, j)，均为长度 n 的 np.ndarray。
    """
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float); c = np.asarray(c, dtype=float)
    N = len(c)
    rsv = np.zeros(N)
    for i in range(N):
        s = max(0, i - n + 1)
        hi = h[s:i + 1].max()
        lo_ = lo[s:i + 1].min()
        rng = hi - lo_
        if rng > 1e-12:
            rsv[i] = (c[i] - lo_) / rng * 100.0
        else:
            rsv[i] = 50.0  # 价无波动时中性
    # K = SMA(rsv, k_period) — 与 SMA 等价的递推（SSE 习惯用前一根 K 起步）
    k = np.zeros(N)
    k[0] = rsv[0]
    for i in range(1, N):
        k[i] = (k[i - 1] * (k_period - 1) + rsv[i]) / k_period
    # D = SMA(k, d_period)
    d = np.zeros(N)
    d[0] = k[0]
    for i in range(1, N):
        d[i] = (d[i - 1] * (d_period - 1) + k[i]) / d_period
    j = 3.0 * k - 2.0 * d
    return k, d, j
