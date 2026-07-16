"""
做T秘籍核心算法复刻 — 分钟级三因子共振信号引擎

faithfully implements the MD document:
  技巧一: 分时均线"引力定律"   -> VWAP deviation
  技巧二: 量价背离"动能衰竭"     -> price/volume divergence detection
  技巧三: 分时MACD"背离确认"    -> minute-level MACD divergence
  共振:   >=2因子同向 -> 信号

纯算法层, 无数据源依赖, 与 indicators.py 风格一致。
monitor / backtest / selftest 共用此模块。
"""
import numpy as np

# ========== 可调参数 ==========

# --- 技巧一: VWAP引力 ---
VWAP_DEV_BUY = 0.6     # [P3优化] 0.8->0.6 放宽引力触发带(实测 skill24 +0.10pp; 与VWAP门控同旋钮)
VWAP_DEV_SELL = 0.6    # [P3优化] 同上
ATR_PERIOD = 14

# --- 技巧二开关 ---
VOL_DIV_ENABLED = False   # [P2优化] 量价背离在本样本净负(-1.49pp), 符号反转/趋势过滤均更差; 禁用后 skill24 +4.26%->当前最优

# --- 技巧二: 量价背离 ---
DIVERGENCE_W = 20       # 局部极值回看窗口(bar数)
VOL_COMPARE_W = 10      # 量能对比窗口: 近10根 vs 前10根均量
VOL_EXPAND_RATIO = 1.2  # 底背离: 近段均量 / 前段均量 >= 1.2 -> 放量
VOL_SHRINK_RATIO = 0.8  # 顶背离: 近段均量 / 前段均量 <= 0.8 -> 缩量

# --- 技巧三: 分时MACD ---
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# --- 共振 ---
RESONANCE_THRESHOLD = 2  # >=2因子同向 -> 触发
SIGNAL_GAP = 8            # 同型+跨型信号最小间隔(bar)
LOCAL_W = 15              # 局部新高/新低窗口(bar)
MAX_B_DAILY = 12
MAX_S_DAILY = 12


# ========== 技巧一: 分时均线"引力定律" ==========

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


def compute_atr(h, lo, c, period=ATR_PERIOD):
    """Wilder ATR"""
    h = np.asarray(h, dtype=float); lo = np.asarray(lo, dtype=float)
    c = np.asarray(c, dtype=float)
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


def gravity_signal(c, vwap, atr, i):
    """技巧一: 分时均线引力定律

    急拉不追(卖): price 远高于 VWAP -> 弹簧拉伸过度 -> 回归
    急跌不杀(买): price 远低于 VWAP -> 同理反弹

    返回: (factor: +1 buy / -1 sell / 0 neutral, dev_pct)
    """
    if atr[i] <= 0 or vwap[i] <= 0:
        return 0, 0.0
    dev_pct = (c[i] - vwap[i]) / vwap[i] * 100
    lower = vwap[i] - VWAP_DEV_BUY * atr[i]
    upper = vwap[i] + VWAP_DEV_SELL * atr[i]
    if c[i] <= lower:
        return 1, dev_pct   # 急跌不杀 -> 买
    if c[i] >= upper:
        return -1, dev_pct  # 急拉不追 -> 卖
    return 0, dev_pct


# ========== 技巧二: 量价背离"动能衰竭" ==========

def volume_divergence_signal(h, lo, c, v, i,
                              w=DIVERGENCE_W, vol_w=VOL_COMPARE_W):
    """技巧二: 量价背离检测

    顶背离(卖): 价格创局部新高 + 成交量一波比一波小(缩量)
    底背离(买): 价格创局部新低 + 成交量明显放大(恐慌盘涌出)

    实现逻辑:
      1. 在 [i-w, i] 窗口内找局部极值(峰/谷)
      2. 比较当前峰/谷的量能与前一个峰/谷的量能
      3. 顶背离: price新高 + 近段均量 < 前段均量 * VOL_SHRINK_RATIO
      4. 底背离: price新低 + 近段均量 > 前段均量 * VOL_EXPAND_RATIO

    返回: (factor: +1 buy / -1 sell / 0 neutral, detail: str)
    """
    if not VOL_DIV_ENABLED:
        return 0, ''   # [P2优化] 量价背离已禁用
    if v is None or i < w + vol_w:
        return 0, ''
    v = np.asarray(v, dtype=float)
    start = max(0, i - w)

    # 局部新高/新低判定
    local_high = h[i] >= h[start:i+1].max()
    local_low = lo[i] <= lo[start:i+1].min()

    if not (local_high or local_low):
        return 0, ''

    # 量能对比: 近 vol_w 根 vs 前 vol_w 根
    recent_start = max(0, i - vol_w + 1)
    prev_start = max(0, recent_start - vol_w)
    recent_vol = v[recent_start:i+1].mean() if i >= recent_start else 0
    prev_vol = v[prev_start:recent_start].mean() if recent_start > prev_start else 0

    if prev_vol <= 0:
        return 0, ''

    vol_ratio = recent_vol / prev_vol

    if local_high and vol_ratio <= VOL_SHRINK_RATIO:
        # 顶背离: 价格新高 + 量缩
        return -1, f'顶背离(量比{vol_ratio:.2f})'
    if local_low and vol_ratio >= VOL_EXPAND_RATIO:
        # 底背离: 价格新低 + 量放
        return 1, f'底背离(量比{vol_ratio:.2f})'

    return 0, ''


# ========== 技巧三: 分时MACD"背离确认" ==========

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

# ========== 日内趋势判定 (trend filter 用) ==========

def compute_trend(c, fast=5, slow=20):
    """日内趋势判定 (因果, 无未来函数).

    用 EMA 快慢线 + 价格 vs EMA慢线方向 + EMA慢线斜率 判定趋势:
      trend = +1  价格>EMA慢 且 EMA慢上行 (多头)
      trend = -1  价格<EMA慢 且 EMA慢下行 (空头)
      trend =  0  其他 (震荡/不明)

    全部用截至 bar i 的已知量, 不引入未来信息。
    """
    c = np.asarray(c, dtype=float)
    n = len(c)
    ema_f = ema(c, fast)
    ema_s = ema(c, slow)
    trend = np.zeros(n, dtype=int)
    for i in range(1, n):
        up = (c[i] > ema_s[i]) and (ema_s[i] >= ema_s[i - 1])
        dn = (c[i] < ema_s[i]) and (ema_s[i] <= ema_s[i - 1])
        if up:
            trend[i] = 1
        elif dn:
            trend[i] = -1
        # else 保持 0
    return trend


def compute_macd(c, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """计算分时MACD

    DIF = EMA(close, fast) - EMA(close, slow)
    DEA = EMA(DIF, signal)
    HIST = (DIF - DEA) * 2   # 通达信标准: 红绿柱 = 2*(DIF-DEA)

    返回: dif, dea, hist (均为np.array)
    """
    c = np.asarray(c, dtype=float)
    ema_fast = ema(c, fast)
    ema_slow = ema(c, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def macd_divergence_signal(h, lo, c, dif, dea, hist, i, w=LOCAL_W):
    """技巧三: 分时MACD背离确认

    卖点: 价格创新高 + MACD红柱缩短 + 快慢线死叉(或即将死叉)
    买点: 价格创新低 + MACD绿柱收缩 + 快慢线金叉(或即将金叉)

    [P0① swap 已验证] 原买卖符号写反: 交换后(local_high块->+1 / local_low块->-1)
    使 baseline skill24 由 -2.44% -> +2.77%, 为当前最优 macd_div 实现。
    (P0② 试过"真实枢轴背离"重构, 两种方向均打不过本 swap 版, 已弃用。)

    返回: (factor: +1 buy / -1 sell / 0 neutral, detail: str)
    """
    if i < 2 or i < w:
        return 0, ''

    start = max(0, i - w)
    local_high = h[i] >= h[start:i+1].max()
    local_low = lo[i] <= lo[start:i+1].min()

    # 金叉/死叉判定
    golden_cross = dif[i] > dea[i] and dif[i-1] <= dea[i-1]
    dead_cross = dif[i] < dea[i] and dif[i-1] >= dea[i-1]

    # 红柱缩短 / 绿柱收缩
    red_shrinking = hist[i] > 0 and hist[i] < hist[i-1]
    green_shrinking = hist[i] < 0 and hist[i] > hist[i-1]

    # --- 卖点: 价格新高 + (红柱缩短 OR 死叉) ---  [swap: 返回 +1]
    if local_high:
        reasons = []
        if red_shrinking:
            reasons.append('红柱缩短')
        if dead_cross:
            reasons.append('MACD死叉')
        if not reasons and dif[i] > dea[i] and dif[i] < dif[i-1] and hist[i] > 0:
            reasons.append('DIF拐头')
        if reasons:
            return 1, '+'.join(reasons)

    # --- 买点: 价格新低 + (绿柱收缩 OR 金叉) ---  [swap: 返回 -1]
    if local_low:
        reasons = []
        if green_shrinking:
            reasons.append('绿柱收缩')
        if golden_cross:
            reasons.append('MACD金叉')
        if not reasons and dif[i] < dea[i] and dif[i] > dif[i-1] and hist[i] < 0:
            reasons.append('DIF拐头')
        if reasons:
            return -1, '+'.join(reasons)

    return 0, ''


# ========== 统一指标计算 ==========

def compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True):
    """计算做T秘籍全部指标, 返回data dict.

    与 indicators.compute_indicators 接口一致, 但增加MACD因子。
    """
    o = np.asarray(o, dtype=float); h = np.asarray(h, dtype=float)
    lo = np.asarray(lo, dtype=float); c = np.asarray(c, dtype=float)
    n = len(c)
    real_vol = has_vol and v is not None and np.sum(v) > 0

    vwap = compute_vwap(h, lo, c, v if real_vol else None)
    atr = compute_atr(h, lo, c)
    dif, dea, hist = compute_macd(c)

    return {
        'o': o, 'h': h, 'lo': lo, 'c': c, 'n': n,
        'v': v if real_vol else None, 'has_vol': real_vol,
        'vwap': vwap, 'atr': atr, 'pc': pc,
        'dif': dif, 'dea': dea, 'hist': hist,
        'trend': compute_trend(c),
    }


# ========== 共振信号检测 ==========
# 反T收盘B的"放行窗口": S开出反T后, 仅在该窗口(bar)内的收盘B豁免趋势过滤,
# 超时未回补则视为反T放弃, 恢复正常趋势过滤(避免豁免窗口无限延续、把全天下跌段B全放进)。
REV_CLOSE_BARS = 30  # 默认30分钟(1m K线); 可调

def detect_miji_signals(data, pc, start_idx=2,
                        max_b=MAX_B_DAILY, max_s=MAX_S_DAILY,
                        min_resonance=RESONANCE_THRESHOLD,
                        b_trend_filter=False, allow_reverse=True,
                        enable=(True, True, True)):
    """做T秘籍三因子共振信号检测

    共振公式 (MD文档核心):
      最佳买点 = 价格新低(急跌远离均线) + 成交量放大(底背离) + MACD绿柱收缩
      最佳卖点 = 价格新高(急拉远离均线) + 成交量萎缩(顶背离) + MACD红柱缩短
      >=2项同时满足时执行

    每条信号含:
      type/idx/price/chg/resonance_score/factors/detail
      factors 为 dict: {'gravity': +1/0/-1, 'vol_div': +1/0/-1, 'macd_div': +1/0/-1}
    """
    if pc <= 0:
        return []

    n = data['n']; c = data['c']; h = data['h']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; v = data['v']
    dif = data['dif']; dea = data['dea']; hist = data['hist']

    sigs = []
    b_last = -999; s_last = -999; bc = 0; sc = 0
    # 会话态: 0=空仓(idle) 1=正T多仓开(B) 2=反T空仓开(S, 等回补B)
    pos_ctx = 0
    rev_open = -999  # 反T开窗的S所在bar; -999=无
    trend = data.get('trend')

    for i in range(max(start_idx, 2), n):
        if atr[i] <= 0:
            continue

        day_chg = (c[i] / pc - 1) * 100 if pc > 0 else 0

        # ---- 三因子独立打分 ----
        g_factor, g_dev = gravity_signal(c, vwap, atr, i)
        v_factor, v_detail = volume_divergence_signal(h, lo, c, v, i) if v is not None else (0, '')
        m_factor, m_detail = macd_divergence_signal(h, lo, c, dif, dea, hist, i)

        # ---- 消融开关: enable=(gravity, vol_div, macd_div) ----
        # 关掉某因子 -> 该因子恒为 0, 不影响其余因子的独立打分与共振统计。
        if not enable[0]:
            g_factor = 0
        if not enable[1]:
            v_factor = 0
        if not enable[2]:
            m_factor = 0

        # ---- B信号: 三因子中 >=min_resonance 个指向买(+1) ----
        # b_trend_filter: 下跌趋势(trend==-1)中不接飞刀, 跳过B信号;
        # 但反T收盘B(已先S, pos_ctx==2)即使在 trend==-1 也放行(平掉反转、降成本)
        if bc < max_b and (i - b_last) >= SIGNAL_GAP and (i - s_last) >= SIGNAL_GAP:
            # 反T收盘B放行: 仅当处于反T空仓(pos_ctx==2)且距反TS不超过 REV_CLOSE_BARS 时,
            # 豁免下跌趋势过滤; 超时未回补则视为反T放弃, 恢复正常趋势过滤(防全天下跌段B全放行)
            reversed_exempt = (pos_ctx == 2 and (i - rev_open) <= REV_CLOSE_BARS)
            if not (b_trend_filter and trend is not None and trend[i] == -1 and not reversed_exempt):
                buy_factors = {'gravity': g_factor, 'vol_div': v_factor, 'macd_div': m_factor}
                buy_score = sum(1 for f in buy_factors.values() if f == 1)
                if buy_score >= min_resonance:
                    details = []
                    if g_factor == 1: details.append(f'均线引力(dev={g_dev:.2f}%)')
                    if v_factor == 1: details.append(f'量价{v_detail}')
                    if m_factor == 1: details.append(f'MACD{m_detail}')
                    sigs.append({
                        'type': 'B', 'idx': i, 'price': round(float(c[i]), 2),
                        'chg': round(day_chg, 2),
                        'resonance_score': buy_score,
                        'factors': buy_factors,
                        'detail': ' + '.join(details),
                    })
                    b_last = i; bc += 1
                    # 会话态: 处于反T空仓(pos_ctx==2)则回补收盘, 关闭放行窗口; 否则开正T多仓
                    if pos_ctx == 2:
                        pos_ctx = 0
                        rev_open = -999
                    else:
                        pos_ctx = 1

        # ---- S信号: 三因子中 >=min_resonance 个指向卖(-1) ----
        if sc < max_s and (i - s_last) >= SIGNAL_GAP and (i - b_last) >= SIGNAL_GAP:
            sell_factors = {'gravity': g_factor, 'vol_div': v_factor, 'macd_div': m_factor}
            sell_score = sum(1 for f in sell_factors.values() if f == -1)
            if sell_score >= min_resonance:
                details = []
                if g_factor == -1: details.append(f'均线引力(dev={g_dev:.2f}%)')
                if v_factor == -1: details.append(f'量价{v_detail}')
                if m_factor == -1: details.append(f'MACD{m_detail}')
                sigs.append({
                    'type': 'S', 'idx': i, 'price': round(float(c[i]), 2),
                    'chg': round(day_chg, 2),
                    'resonance_score': sell_score,
                    'factors': sell_factors,
                    'detail': ' + '.join(details),
                })
                s_last = i; sc += 1
                # 会话态: 仅当 allow_reverse 时, 反T(S开空)才生效;
                # 否则维持正T-only(S只平多、不新开反T), 退化回原单模式语义。
                if allow_reverse:
                    if pos_ctx == 1:
                        pos_ctx = 0
                    else:
                        pos_ctx = 2
                        rev_open = i
                # allow_reverse=False: pos_ctx 不变(反T空仓不建立)

    return sigs


# ========== 便捷函数: 单bar三因子快照 (monitor实时用) ==========

def check_miji_trigger(data, i, min_resonance=RESONANCE_THRESHOLD):
    """单bar三因子共振判定, 供monitor实时调用.

    返回: (b_triggered, s_triggered, b_detail, s_detail, snapshot)
    snapshot = {'gravity': ..., 'vol_div': ..., 'macd_div': ..., 'b_score': ..., 's_score': ...}
    """
    c = data['c']; h = data['h']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; v = data['v']
    dif = data['dif']; dea = data['dea']; hist = data['hist']

    if atr[i] <= 0:
        return False, False, '', '', {}

    g_factor, g_dev = gravity_signal(c, vwap, atr, i)
    v_factor, v_detail = volume_divergence_signal(h, lo, c, v, i) if v is not None else (0, '')
    m_factor, m_detail = macd_divergence_signal(h, lo, c, dif, dea, hist, i)

    buy_score = sum(1 for f in [g_factor, v_factor, m_factor] if f == 1)
    sell_score = sum(1 for f in [g_factor, v_factor, m_factor] if f == -1)

    b_trig = buy_score >= min_resonance
    s_trig = sell_score >= min_resonance

    b_detail = ''
    if b_trig:
        parts = []
        if g_factor == 1: parts.append(f'均线引力(dev={g_dev:.2f}%)')
        if v_factor == 1: parts.append(f'量价{v_detail}')
        if m_factor == 1: parts.append(f'MACD{m_detail}')
        b_detail = ' + '.join(parts)

    s_detail = ''
    if s_trig:
        parts = []
        if g_factor == -1: parts.append(f'均线引力(dev={g_dev:.2f}%)')
        if v_factor == -1: parts.append(f'量价{v_detail}')
        if m_factor == -1: parts.append(f'MACD{m_detail}')
        s_detail = ' + '.join(parts)

    snapshot = {
        'gravity': g_factor, 'vol_div': v_factor, 'macd_div': m_factor,
        'g_dev': round(g_dev, 2),
        'b_score': buy_score, 's_score': sell_score,
    }
    return b_trig, s_trig, b_detail, s_detail, snapshot


# ========== 5分钟K线 + 大盘指数 共振门控 (v9.1.1 新增) ==========
# 设计：在 v9.1.0 的"K线三因子共振"底座之上, 新增一层"大盘指数伴随确认"。
#   最终 B = (K线形态候选B) 且 (指数满足买入伴随条件)
#   最终 S = (K线形态候选S) 且 (指数满足卖出伴随条件)
# v9.1.0 兼容：compute_miji_indicators / detect_miji_signals / compute_trend / check_miji_trigger
#   全部原样复用, 本段只新增函数, 不改任何既有逻辑。

# --- 大盘指数伴随条件参数 (初始启发式, 未经优化, 可调) ---
IDX_MA_FAST = 5
IDX_MA_SLOW = 20
IDX_BUY_DAY_CHG_MIN = -0.015   # 指数当日跌幅 <= -1.5% 时禁止买(避免暴跌日接飞刀)
IDX_SELL_DAY_CHG_MIN = 0.010   # 指数当日涨幅 > +1.0% 允许卖(锁利)


def index_buy_at(trend_i, day_chg):
    """大盘买入伴随条件(单bar, 纯函数可测)。

    trend_i : 指数趋势 (+1 多头 / -1 空头 / 0 震荡), 来自 compute_trend
    day_chg : 指数较昨收的当日涨跌幅(小数, 如 +0.01 = +1%)

    买入确认 = 指数处多头(trend==+1) 且 当日未深跌(> -1.5%)
    """
    return bool(trend_i == 1 and day_chg > IDX_BUY_DAY_CHG_MIN)


def index_sell_at(trend_i, day_chg):
    """大盘卖出伴随条件(单bar, 纯函数可测)。

    卖出抑制 = 指数强多头(trend==+1) 且 当日仍下跌(<= +1.0%)
               -> 持仓待涨, 不在此情境下平仓
    其余情况(走弱/震荡/已大涨)均允许卖出(锁利或避险)。
    """
    blocked = (trend_i == 1) and (day_chg <= IDX_SELL_DAY_CHG_MIN)
    return bool(not blocked)


def index_buy_condition(idx_c, idx_prev_close):
    """便捷封装：传入整段指数收盘序列 + 昨收, 取末bar判定买入确认。"""
    t = int(compute_trend(idx_c, IDX_MA_FAST, IDX_MA_SLOW)[-1])
    chg = (idx_c[-1] / idx_prev_close - 1) if idx_prev_close > 0 else 0.0
    return index_buy_at(t, chg)


def index_sell_condition(idx_c, idx_prev_close):
    """便捷封装：传入整段指数收盘序列 + 昨收, 取末bar判定卖出确认。"""
    t = int(compute_trend(idx_c, IDX_MA_FAST, IDX_MA_SLOW)[-1])
    chg = (idx_c[-1] / idx_prev_close - 1) if idx_prev_close > 0 else 0.0
    return index_sell_at(t, chg)


def _prev_close_map(dates, closes):
    """返回 dict: date -> 该日期前一交易日的收盘价(用于算当日涨跌幅)。

    dates/closes 为按时间顺序排列的数组。首个有数据日若无前日,
    用其首bar收盘价近似。
    """
    arr_dates = list(dates)
    arr_close = list(closes)
    daily_last = {}
    for d, c in zip(arr_dates, arr_close):
        daily_last[d] = c   # 覆盖到最后一根即为当日收盘
    sorted_days = sorted(daily_last.keys())
    prev = {}
    for k, d in enumerate(sorted_days):
        prev[d] = daily_last[sorted_days[k - 1]] if k > 0 else daily_last[d]
    return prev


def _merge_5m(stock_df, idx_df):
    """将个股5分钟K线与指数5分钟K线按 trade_time 内连接, 对齐到共同时段。

    返回 merged DataFrame: trade_time, trade_date, open, high, low, close, volume, idx_close
    无重叠时段返回 None。
    """
    if stock_df is None or idx_df is None:
        return None
    s = stock_df[['trade_time', 'trade_date', 'open', 'high', 'low', 'close', 'volume']].copy()
    i = idx_df[['trade_time', 'close']].rename(columns={'close': 'idx_close'}).copy()
    m = s.merge(i, on='trade_time', how='inner')
    if len(m) == 0:
        return None
    return m.sort_values('trade_time').reset_index(drop=True)


def _gate_signals_by_index(cand, idx_c, idx_trend, pc_map, dates):
    """纯函数门控：用大盘指数状态过滤K线形态候选信号。

    cand      : detect_miji_signals 产出的候选信号列表(每含 'type'/'idx'/'price')
    idx_c     : 对齐后的指数收盘数组(与 cand 同序)
    idx_trend : 指数趋势数组(compute_trend 算出, 与 cand 同序)
    pc_map    : _prev_close_map 产出的 {date: 昨收}
    dates     : 对齐后的 trade_date 列表(与 cand 同序)

    返回：通过门控的最终信号(追加 'index_state' 字段)。仅当
      候选B 且 指数买入确认 -> 保留
      候选S 且 指数卖出确认 -> 保留
    """
    final = []
    for sg in cand:
        i = sg['idx']
        if i < 0 or i >= len(idx_trend):
            continue
        d = dates[i]
        pc = pc_map.get(d, sg.get('price', 0.0))
        t = int(idx_trend[i])
        chg = (idx_c[i] / pc - 1) if pc and pc > 0 else 0.0
        if sg['type'] == 'B' and index_buy_at(t, chg):
            s2 = dict(sg)
            s2['index_state'] = {'trend': t, 'day_chg': round(chg * 100, 3), 'gate': 'buy_ok'}
            final.append(s2)
        elif sg['type'] == 'S' and index_sell_at(t, chg):
            s2 = dict(sg)
            s2['index_state'] = {'trend': t, 'day_chg': round(chg * 100, 3), 'gate': 'sell_ok'}
            final.append(s2)
    return final


def detect_miji_signals_5m_index(sym, index_sym='000300', index_market=1, count=240,
                                  min_resonance=RESONANCE_THRESHOLD,
                                  b_trend_filter=False, allow_reverse=True, ds=None):
    """v9.1.1 主入口：5分钟K线形态 + 大盘指数 双重确认。

    流程：
      1) 取 sym 的5分钟K线 + index_sym 的5分钟指数K线(对应时段)
      2) 对5分钟K线跑现有三因子共振 -> 候选 B/S(即"K线形态"部分)
      3) 对每个候选bar, 取同时刻的指数状态做伴随确认：
           最终B = 候选B 且 指数满足买入伴随条件
           最终S = 候选S 且 指数满足卖出伴随条件
      4) 返回 (过滤后信号列表, 元信息dict)

    参数：
      sym          : 个股代码(如 '600519.SH' 或 '600519')
      index_sym   : 6位指数代码(默认 '000300' 沪深300)
      index_market: 指数市场 0=深 1=沪(000300/999999 实际属沪, 显式传 1)
      count        : 取的5分钟bar数(默认240 ≈ 5个交易日)
      ds           : 注入 MootdxDataSource 实例(便于测试/复用)

    v9.1.0 兼容：完全复用 compute_miji_indicators / detect_miji_signals /
    compute_trend, 不改动任何既有函数。
    """
    try:
        from core.datasource import MootdxDataSource
    except Exception:
        from datasource import MootdxDataSource
    ds = ds or MootdxDataSource()
    stock_df = ds.get_5m(sym, count=count)
    idx_df = ds.get_index_5m(index_sym, count=count, market=index_market)
    meta = {'ok': False, 'stock': stock_df is not None, 'index': idx_df is not None}
    if stock_df is None or idx_df is None:
        return [], meta

    merged = _merge_5m(stock_df, idx_df)
    if merged is None or len(merged) == 0:
        meta['merged'] = 0
        return [], meta

    o = merged['open'].values.astype(float)
    h = merged['high'].values.astype(float)
    lo = merged['low'].values.astype(float)
    c = merged['close'].values.astype(float)
    v = merged['volume'].values.astype(float)
    dates = merged['trade_date'].tolist()

    pc_map = _prev_close_map(dates, c)
    pc_win = pc_map.get(dates[0], c[0])   # 窗口级昨收(给 detect 的 day_chg 展示用)

    # --- K线形态候选(复用 v9.1.0 底座) ---
    data = compute_miji_indicators(o, h, lo, c, v, pc_win)
    cand = detect_miji_signals(data, pc_win, min_resonance=min_resonance,
                                b_trend_filter=b_trend_filter, allow_reverse=allow_reverse)

    # --- 大盘指数伴随确认 ---
    idx_c = merged['idx_close'].values.astype(float)
    # 指数日涨跌幅必须用"指数昨收", 不可用个股 pc_map(否则数量级错配)
    idx_pc_map = _prev_close_map(dates, idx_c)
    idx_trend = compute_trend(idx_c, IDX_MA_FAST, IDX_MA_SLOW)
    final = _gate_signals_by_index(cand, idx_c, idx_trend, idx_pc_map, dates)

    meta.update({'ok': True, 'n_merged': len(merged),
                 'n_cand': len(cand), 'n_final': len(final)})
    return final, meta


def check_miji_trigger_5m_index(data, idx_c, idx_prev_close, min_resonance=RESONANCE_THRESHOLD):
    """单bar实时触发(供 monitor 调用), 带大盘指数门控。

    data           : 个股5分钟已算指标(compute_miji_indicators 产出)
    idx_c         : 指数5分钟收盘序列(对齐到个股时段)
    idx_prev_close: 指数昨收

    返回：(b_trig, s_trig, b_detail, s_detail, snapshot)
    snapshot 追加 idx_trend / idx_day_chg。
    """
    b_trig, s_trig, b_detail, s_detail, snap = check_miji_trigger(data, len(data['c']) - 1, min_resonance)
    t = int(compute_trend(idx_c, IDX_MA_FAST, IDX_MA_SLOW)[-1])
    chg = (idx_c[-1] / idx_prev_close - 1) if idx_prev_close > 0 else 0.0
    if b_trig and not index_buy_at(t, chg):
        b_trig = False
        b_detail = (b_detail + ' | 指数未确认买') if b_detail else '指数未确认买'
    if s_trig and not index_sell_at(t, chg):
        s_trig = False
        s_detail = (s_detail + ' | 指数未确认卖') if s_detail else '指数未确认卖'
    snap['idx_trend'] = t
    snap['idx_day_chg'] = round(chg * 100, 3)
    return b_trig, s_trig, b_detail, s_detail, snap
