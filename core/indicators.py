"""
v9 纯算法层 — 指标计算 + 信号触发判定
无数据源(tickflow)/状态(STATE)依赖, 可独立单元测试。
monitor / backtest / selftest 共用此模块。
"""
import numpy as np

# ========== 默认参数 ==========
ATR_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 60
ADX_PERIOD = 14
ADX_THRESHOLD = 20
K1 = 1.0                 # 标准轨倍数(1.2实测B T+30边际为负,回退)
K2 = 2.0                 # 极端轨倍数(均值回归)
VOL_LOOKBACK = 20
VOL_THRESHOLD = 2.0     # 1.5→2.0 B量价确认加强(诊断:量比≥2.0胜率56.5% vs 1.5-2.0区间50%)
W_RSI, W_CHG, W_VR, W_DEV = 0.4, 0.2, 0.2, 0.2
TEMP_HOT = 70
TEMP_COLD = 30
MAX_B_DAILY = 12
MAX_S_DAILY = 12

# ========== v2 因子参数 (第一性原理自迭代, 2026-07-13) ==========
K1_V2 = 0.8       # 收窄标准轨(原1.0), 更敏感地捕捉回踩
K2_V2 = 1.8       # 收窄极端轨(原2.0)
M_V2 = 1.2        # 放低量比门槛(原2.0), 避免震荡日完全哑火
S_RSI_GATE = 55   # S信号RSI超买门槛
B_RSI_OVERSOLD = 35  # B信号跌日超卖门槛
DOWN_DAY_THR = -1.0  # 日内跌幅超过此值视为跌日(触发严格B过滤)
LOCAL_W = 15      # 局部极值窗口(分钟)
SIGNAL_GAP = 8    # 同型+跨型信号最小间隔(分钟)


# ========== 基础指标 ==========

def ema(arr, period):
    """指数移动平均"""
    arr = np.asarray(arr, dtype=float)
    out = np.zeros_like(arr)
    if len(arr) == 0:
        return out
    k = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i-1] * (1 - k)
    return out


def compute_atr(h, lo, c, period=ATR_PERIOD):
    """Wilder ATR"""
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


def compute_adx(h, lo, c, period=ADX_PERIOD):
    """Wilder ADX"""
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float); c = np.asarray(c, dtype=float)
    n = len(c)
    if n < period * 2:
        return np.zeros(n)
    up = np.diff(h, prepend=h[0])
    down = np.diff(-lo, prepend=-lo[0])  # lo[i-1]-lo[i]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = np.zeros(n)
    tr[0] = h[0] - lo[0]
    for i in range(1, n):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i-1]), abs(lo[i] - c[i-1]))
    atr_s = np.zeros(n); pdm_s = np.zeros(n); mdm_s = np.zeros(n)
    atr_s[period] = tr[1:period+1].sum()
    pdm_s[period] = plus_dm[1:period+1].sum()
    mdm_s[period] = minus_dm[1:period+1].sum()
    for i in range(period+1, n):
        atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
        pdm_s[i] = pdm_s[i-1] - pdm_s[i-1]/period + plus_dm[i]
        mdm_s[i] = mdm_s[i-1] - mdm_s[i-1]/period + minus_dm[i]
    plus_di = np.where(atr_s > 0, 100 * pdm_s / np.where(atr_s == 0, 1, atr_s), 0)
    minus_di = np.where(atr_s > 0, 100 * mdm_s / np.where(atr_s == 0, 1, atr_s), 0)
    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100 * np.abs(plus_di - minus_di) / np.where(di_sum == 0, 1, di_sum), 0)
    adx = np.zeros(n)
    start = period * 2
    if n > start:
        adx[start] = dx[period+1:start+1].mean()
        for i in range(start+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
        adx[:start] = adx[start]
    return adx


def compute_rsi(c, period=14):
    """RSI(14) Wilder"""
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


def compute_vwap(h, lo, c, v):
    """日内累计VWAP. v为None或全0时退化为等权均价."""
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float)
    c = np.asarray(c, dtype=float); n = len(c)
    if v is not None and np.sum(v) > 0:
        v = np.asarray(v, dtype=float)
        tp = (h + lo + c) / 3.0
        cum_vp = np.cumsum(tp * v)
        cum_v = np.cumsum(v)
        return cum_vp / np.where(cum_v > 0, cum_v, 1.0)
    return np.cumsum(c) / np.arange(1, n + 1)


def compute_vol_ratio(v, lookback=VOL_LOOKBACK):
    """量比 = 当前量 / 过去lookback bar均量"""
    n = len(v) if v is not None else 0
    vr = np.ones(max(n, 1))
    if v is None:
        return vr
    v = np.asarray(v, dtype=float)
    for i in range(lookback, n):
        avg = v[i-lookback:i].mean()
        vr[i] = v[i] / avg if avg > 0 else 1.0
    return vr


# ========== 统一指标计算 ==========

def compute_indicators(o, h, lo, c, v, pc, has_vol=True):
    """计算全部v9指标, 返回data dict. 供 detect_signals / 触发判定使用.
    o/h/lo/c: OHLC数组; v: volume数组或None; pc: 昨收; has_vol: 数据源是否真有量."""
    o = np.asarray(o, dtype=float); h = np.asarray(h, dtype=float)
    lo = np.asarray(lo, dtype=float); c = np.asarray(c, dtype=float)
    n = len(c)
    real_vol = has_vol and v is not None and np.sum(v) > 0

    vwap = compute_vwap(h, lo, c, v if real_vol else None)
    atr = compute_atr(h, lo, c)
    ema_f = ema(c, EMA_FAST)
    ema_s = ema(c, EMA_SLOW)
    adx = compute_adx(h, lo, c)
    trend = np.where((ema_f > ema_s) & (adx > ADX_THRESHOLD), 1,
            np.where((ema_f < ema_s) & (adx > ADX_THRESHOLD), -1, 0))
    vol_ratio = compute_vol_ratio(v if real_vol else None)
    rsi = compute_rsi(c)

    chg_pct = (c - pc) / pc * 100 if pc > 0 else np.zeros(n)
    chg_comp = np.clip((chg_pct + 5) / 10.0 * 100, 0, 100)
    vr_comp = np.clip(vol_ratio / 3.0 * 100, 0, 100)
    dev_pct = np.where(vwap > 0, (c - vwap) / vwap * 100, 0)
    dev_comp = np.clip((dev_pct + 2) / 4.0 * 100, 0, 100)
    temp = np.clip(W_RSI * rsi + W_CHG * chg_comp + W_VR * vr_comp + W_DEV * dev_comp, 0, 100)

    return {
        'o': o, 'h': h, 'lo': lo, 'c': c, 'n': n,
        'vwap': vwap, 'atr': atr, 'trend': trend,
        'vol_ratio': vol_ratio, 'has_vol': real_vol,
        'rsi': rsi, 'temp': temp, 'ema_f': ema_f, 'ema_s': ema_s, 'adx': adx,
    }


# ========== 单bar触发判定 (monitor与detect共用) ==========

def check_b_trigger(data, i):
    """B信号触发判定. 返回 (triggered: bool, reason: str).
    温度不再硬拦截(仅影响星级), 避免上涨趋势整体偏热误抑回踩B."""
    if data['atr'][i] <= 0:
        return False, ''
    c = data['c']; o = data['o']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']
    vol_ratio = data['vol_ratio']; has_vol = data['has_vol']
    lower_std = vwap[i] - K1 * atr[i]
    lower_ext = vwap[i] - K2 * atr[i]
    is_yang = c[i] > o[i]
    lower_shadow = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
    # 趋势过滤: 只上升才发B(震荡回踩易破位,回测B胜率低47%)
    if trend[i] != 1:
        return False, ''
    # 反转形态: 收阳 或 长下影(≥0.5ATR)
    if not (is_yang or lower_shadow >= 0.5 * atr[i]):
        return False, ''
    reason = ''
    triggered = False
    # 标准轨: 前根触及下轨(close或low), 当前收回上方
    if (c[i-1] <= lower_std or lo[i-1] <= lower_std) and c[i] > lower_std:
        triggered = True; reason = '回踩下轨'
    # 极端轨均值回归: 触极端下轨 + 长下影
    elif lo[i] <= lower_ext and lower_shadow >= atr[i]:
        triggered = True; reason = '极端超卖反弹'
    if triggered and has_vol and vol_ratio[i] < VOL_THRESHOLD:
        return False, '量不足'
    return triggered, reason


def check_s_trigger(data, i):
    """S信号触发判定. 返回 (triggered: bool, reason: str).
    温度不再硬拦截(仅影响星级), 避免下跌趋势整体偏冷误抑反弹S."""
    if data['atr'][i] <= 0:
        return False, ''
    c = data['c']; o = data['o']; h = data['h']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']
    vol_ratio = data['vol_ratio']; has_vol = data['has_vol']
    upper_std = vwap[i] + K1 * atr[i]
    upper_ext = vwap[i] + K2 * atr[i]
    is_yin = c[i] < o[i]
    upper_shadow = (h[i] - o[i]) if is_yin else (h[i] - c[i])
    # 趋势过滤: 下降/震荡才发S
    if trend[i] not in (-1, 0):
        return False, ''
    # 反转形态: 收阴 或 长上影(≥0.5ATR)
    if not (is_yin or upper_shadow >= 0.5 * atr[i]):
        return False, ''
    reason = ''
    triggered = False
    # 标准轨: 前根触及上轨(close或high), 当前收回下方
    if (c[i-1] >= upper_std or h[i-1] >= upper_std) and c[i] < upper_std:
        triggered = True; reason = '反弹遇阻'
    # 极端轨均值回归: 触极端上轨 + 长上影
    elif h[i] >= upper_ext and upper_shadow >= atr[i]:
        triggered = True; reason = '极端超买回落'
    if triggered and has_vol and vol_ratio[i] < VOL_THRESHOLD:
        return False, '量不足'
    return triggered, reason


# ========== 批量信号检测 (回测/selftest用, 无冷却) ==========

def detect_signals(data, pc, start_idx=2, max_b=MAX_B_DAILY, max_s=MAX_S_DAILY):
    """从 start_idx 扫描, 返回所有触发信号(不做冷却, 回测用).
    每条信号含 type/idx/price/chg/rsi/temp/trend/reason/vol_ratio."""
    signals = []
    if pc <= 0:
        return signals
    c = data['c']; rsi = data['rsi']; temp = data['temp']; trend = data['trend']
    vol_ratio = data['vol_ratio']; n = data['n']
    b_count = 0; s_count = 0
    for i in range(max(start_idx, 2), n):
        tb, rb = check_b_trigger(data, i)
        if tb and b_count < max_b:
            b_count += 1
            chg = (c[i] - pc) / pc * 100
            signals.append({'type': 'B', 'idx': i, 'price': round(float(c[i]), 2),
                            'chg': round(chg, 2), 'rsi': round(float(rsi[i]), 1),
                            'temp': round(float(temp[i]), 0), 'trend': int(trend[i]),
                            'reason': rb, 'vol_ratio': round(float(vol_ratio[i]), 2)})
        ts, rs = check_s_trigger(data, i)
        if ts and s_count < max_s:
            s_count += 1
            chg = (c[i] - pc) / pc * 100
            signals.append({'type': 'S', 'idx': i, 'price': round(float(c[i]), 2),
                            'chg': round(chg, 2), 'rsi': round(float(rsi[i]), 1),
                            'temp': round(float(temp[i]), 0), 'trend': int(trend[i]),
                            'reason': rs, 'vol_ratio': round(float(vol_ratio[i]), 2)})
    return signals


# ========== v2 信号检测 (第一性原理: 均值回归+量价反转+动量确认+非对称设计) ==========

def detect_signals_v2(data, pc, start_idx=2, max_b=MAX_B_DAILY, max_s=MAX_S_DAILY):
    """v2 信号检测 — 基于第一性原理自迭代(2026-07-13).

    核心改进 vs detect_signals:
    1. B低吸: 删除"必须trend==1才买"的过严约束(原v9在震荡日0信号),
       改为: 超卖区(刺穿VWAP-K1·ATR) + 止跌反转K线 + 动量确认 + 放量 + trend==1
       跌日(day_chg<-1%)额外要求: 阳线+实体≥0.3ATR+RSI<35+RSI回升+收盘>前根+EMA20上升
    2. S高抛: 删除"必须trend∈{-1,0}"约束(上升趋势也有高抛机会),
       改为: 极端超买区(刺穿VWAP+K2·ATR) + 近LOCAL_W分钟新高 + RSI≥55且回落 + 收盘<前根 + 放量
    3. 跨信号冷却: B后SIGNAL_GAP分钟内不发S, 反之亦然(避免同段行情两面抓)
    4. 量比门槛降低(2.0→1.2): 避免震荡日完全哑火

    返回格式与 detect_signals 兼容, 额外含 rsi/day_chg 字段.
    """
    if pc <= 0:
        return []
    n = data['n']; c = data['c']; o = data['o']; lo = data['lo']; h = data['h']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']; vr = data['vol_ratio']
    ema_f = data['ema_f']; rsi = data['rsi']; has_vol = data['has_vol']
    sigs = []; b_last = -999; s_last = -999; bc = 0; sc = 0
    for i in range(max(start_idx, 2), n):
        if atr[i] <= 0:
            continue
        lower_std = vwap[i] - K1_V2 * atr[i]; lower_ext = vwap[i] - K2_V2 * atr[i]
        upper_ext = vwap[i] + K2_V2 * atr[i]
        is_yang = c[i] > o[i]; is_yin = c[i] < o[i]
        ls = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
        us = (h[i] - o[i]) if is_yin else (h[i] - c[i])
        day_chg = (c[i] / pc - 1) * 100
        # ---- B ----
        if bc < max_b and (i - b_last) >= SIGNAL_GAP and (i - s_last) >= SIGNAL_GAP:
            hit = (lo[i-1] <= lower_std) or (lo[i] <= lower_std) or (lo[i] <= lower_ext)
            reversion = (c[i] > lower_std) or (c[i] > lower_ext and ls >= atr[i])
            if day_chg < DOWN_DAY_THR:
                body = abs(c[i] - o[i])
                reversal_k = is_yang and (body >= 0.3 * atr[i])
                momentum = (rsi[i] < B_RSI_OVERSOLD) and (rsi[i] > rsi[i-1]) and (c[i] > c[i-1]) and (ema_f[i] > ema_f[i-1])
            else:
                reversal_k = is_yang or (ls >= 0.5 * atr[i])
                momentum = (c[i] > ema_f[i]) or (rsi[i] > rsi[i-1])
            trend_ok = int(trend[i]) == 1
            vol_ok = (not has_vol) or (vr[i] >= M_V2)
            if hit and reversion and reversal_k and momentum and vol_ok and trend_ok:
                sigs.append({'type': 'B', 'idx': i, 'price': round(float(c[i]), 2),
                             'chg': round(day_chg, 2), 'rsi': round(float(rsi[i]), 1),
                             'trend': int(trend[i]),
                             'reason': '超卖反转' if lo[i] <= lower_ext else '回踩下轨',
                             'vol_ratio': round(float(vr[i]), 2)})
                b_last = i; bc += 1
        # ---- S ----
        if sc < max_s and (i - s_last) >= SIGNAL_GAP and (i - b_last) >= SIGNAL_GAP:
            hit = (h[i-1] >= upper_ext) or (h[i] >= upper_ext)
            w_start = max(0, i - LOCAL_W)
            local_top = h[i] >= h[w_start:i+1].max()
            reversal_k = is_yin or (us >= 0.5 * atr[i])
            momentum = (rsi[i] >= S_RSI_GATE) and (rsi[i] < rsi[i-1]) and (c[i] < c[i-1])
            vol_ok = (not has_vol) or (vr[i] >= M_V2)
            if hit and local_top and reversal_k and momentum and vol_ok:
                sigs.append({'type': 'S', 'idx': i, 'price': round(float(c[i]), 2),
                             'chg': round(day_chg, 2), 'rsi': round(float(rsi[i]), 1),
                             'trend': int(trend[i]),
                             'reason': '超买回落' if h[i] >= (vwap[i] + K2_V2 * atr[i]) else '反弹遇阻',
                             'vol_ratio': round(float(vr[i]), 2)})
                s_last = i; sc += 1
    return sigs


def stars(sig_type, temp_val, vol_ratio_val):
    """星级: B越冷越强, S越热越强; 量比越大越强."""
    if sig_type == 'B':
        t_score = 3 if temp_val < 30 else (2 if temp_val < 45 else 1)
    else:
        t_score = 3 if temp_val > 70 else (2 if temp_val > 55 else 1)
    v_score = 2 if vol_ratio_val >= 1.5 else (1 if vol_ratio_val >= 1.2 else 0)
    total = t_score + v_score
    if total >= 5:
        return '★★★'
    if total >= 3:
        return '★★☆'
    return '★☆☆'
