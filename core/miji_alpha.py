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
import os
import sys
import numpy as np

# 确保能导入 backtest/keyfactor 下的共享模块（消除 miji_alpha/miji_engine 双重维护）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from backtest.keyfactor._gate_floor import gate_buy, gate_sell  # noqa: E402
# [2026-08-18 Phase 2 单一因子源] 因果原语统一从 primitives 导入，删除本文件重复实现
from primitives import ema, compute_atr, compute_vwap, compute_rsi, compute_vol_ratio  # noqa: E402

# ========== 可调参数 ==========

# --- 技巧一: VWAP引力 ---
# [P2-2026-08-02 调参落地] 0.6->0.65: 40只调参池固定口径盈亏比 0.86->0.89、胜率 47.40->47.55%，
#   样本保留（笔数-2%）；watchlist 5只独立验证无单只>1.2pp退化（300058 盈亏比 0.89->0.95）。
#   方向与 259万样本报告「深负偏离 0.4389 优势」一致。回滚：改回 0.6。
VWAP_DEV_BUY = 0.65    # [P3优化] 0.8->0.6->0.65 引力触发带（与VWAP门控同旋钮）
VWAP_DEV_SELL = 0.6    # [P3优化] 同上（S侧未调，保持 0.6）
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
RESONANCE_THRESHOLD = 2  # >=2因子同向 -> 触发信号
# --- MACD 门控（分级，可切换）---
# strict : B需MACD底背离(m_factor==1)/S需顶背离(m_factor==-1)，排除gravity-only（生产默认，OOS证优）
# off    : 纯引力(gravity=1即B / gravity=-1即S，方案1激进抓底，无视MACD)
# floor  : strict基础 + 价格地板B(创session新低+偏离VWAP超阈)/天花板S，捕杀跌精确底
MACD_GATE_MODE = os.environ.get('MACD_GATE_MODE', 'strict').lower()
FLOOR_DEV_PCT = 1.5   # 地板/天花板偏离VWAP阈值(%)：价格新低/新高且偏离超此值即触发

# --- MACD 背离强度阈值（2026-08-01 报告落地；B/S 通用，单位=hist 点数） ---
# 259 万样本实证：hist≈0 弱背离胜率最低（B 35.2%），强柱区 43.5% 单调上升。
# 默认 0.15；可用环境变量 TP_MHD_THRESHOLD 覆盖（回滚路径：设 0 或删除变量恢复原生产行为）。
MHD_THRESHOLD = float(os.environ.get('TP_MHD_THRESHOLD', '0.15'))

# --- 早盘 B 放宽阈值（2026-08-01 报告研究项，2026-08-02 实证后默认关闭） ---
# 报告 B_is_morning 0.4278 vs 非早盘 0.3653（早盘时段胜率优势）。
# ⚠️ 2026-08-02 实证纠偏：hist 强度差分布 <0.05 占 78.3%、0.05~0.15 仅 12.4%，
#   早盘放宽到 0.05 会把 P0(0.15) 过滤掉的弱背离噪音放回 → 与 P0 结论冲突。
#   故「早盘放宽 hist 门槛」默认关闭（MORNING_MHD_THRESHOLD=0 = 全天统一 0.15）。
#   若要落地"早盘优先 B"，方向应是早盘加权/时段因子（非降质量门槛），待 P2 调参验证。
MORNING_MHD_THRESHOLD = float(os.environ.get('TP_MORNING_B_MHD', '0'))
# [P2-1 迭代] 高波动保护（接通 _gate_floor 已预留的 trend_state 通道）：
#   floor 门控在强趋势(单边)行情下收紧地板/天花板阈值 ×1.5，避免"均线引力被反向突破"类失效。
#   保守默认：开。生产 behavior 仅在 MACD_GATE_MODE=floor 时生效（strict 不受影响）。
HIGH_VOL_GUARD = os.environ.get('TP_HIGH_VOL_GUARD', '1').lower() in ('1', 'true', 'yes')
SIGNAL_GAP = 8            # 同型+跨型信号最小间隔(bar)
LOCAL_W = 15              # 局部新高/新低窗口(bar)
MAX_B_DAILY = 12
MAX_S_DAILY = 12

# --- 多周期 MACD 方向过滤（P3-1，2026-08-02 新增，默认关闭） ---
# 259 万样本报告：S 侧 macd60_dif perm 0.1007（第 1 特征）、B 侧 macd60_dif perm 0.0475（第 2）
#   → 多周期方向是 S/B 两侧最强结构性特征。落地方式 = 方向一致过滤：
#     B 信号要求大周期（60m/15m）hist 均为负（大周期在下方，1m 抄底顺大势）
#     S 信号要求大周期（60m/15m）hist 均为正（大周期在上方，1m 逃顶顺大势）
# 默认关闭（mpr_enable=False），不改变生产行为；验证通过才接入。
# env TP_MPR_ENABLE=1 可开启（monitor 侧）；回测走 detect_miji_signals 的 mpr_enable 参数。
MPR_ENABLE = os.environ.get('TP_MPR_ENABLE', '0').lower() in ('1', 'true', 'yes')
MPR_PERIODS = (60, 15)   # 大周期组合：B 需两者 hist 均<0、S 需均>0


def _is_new_low(c, lo, i, w=LOCAL_W):
    """lo[i] 是否创窗口内新低(严格 < 前窗口最低价)。floor 档价格地板B用。

    2026-07-26 修正(移植自 D 分支 d_strategy.is_swing_low):
    旧实现用 c[i](收盘) 比前窗 lo.min(), 顶部/底部反转 bar 收盘回落即漏判真实极值
    -> 结构性漏底。改为用 BAR 自身 lo[i] 比前窗 lo.min(), 即真正的 swing-low 判定。
    """
    if i < 1:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def _is_new_high(c, h, i, w=LOCAL_W):
    """h[i] 是否创窗口内新高(严格 > 前窗口最高价)。floor 档价格天花板S用。

    2026-07-26 修正(移植自 D 分支 d_strategy.is_swing_high):
    旧实现用 c[i](收盘) 比前窗 h.max(), 顶部反转 bar 收盘回落即漏判真实极值
    -> 结构性漏顶。改为用 BAR 自身 h[i] 比前窗 h.max(), 即真正的 swing-high 判定。
    """
    if i < 1:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


# ========== 技巧一: 分时均线"引力定律" ==========

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

    # 走平封板/一字/停牌：OHLC 全等，无有效极值，跳过
    if h[i] == lo[i]:
        return 0, ''
    # 严格极值判定：价格须严格超越【前序】窗口极值(不含自身)才算创新高/新低
    # 切片用 [start:i] 排除自身；若含自身则 h[i] 恒等于窗口max，严格 > 永远为 False
    # 局部新高/新低判定
    local_high = h[i] > h[start:i].max()
    local_low = lo[i] < lo[start:i].min()

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


def compute_trend_strength(c, fast=5, slow=20, confirm_bars=8):
    """[轮次2-5 迭代] 趋势强度确认：仅当趋势方向已持续 confirm_bars 根才认定"强趋势"。

    守卫（P2-1 的 ×1.5 地板/天花板收紧）只应在"确认强趋势"时生效——
    A/B 实证发现：EMA 快慢线在 V 型反转日滞后 10+ 根，导致 07-31 588000
    早盘 V 反弹的浅层地板（dev -1.5%~-2.1%，4 条全有效）被 ×1.5 误滤，
    而尾盘真下跌的深层地板（dev -2.3%/-3.1%，全失效）因趋势滞后反而放行。

    返回与 compute_trend 同形状的 int 数组：0=非强趋势，±1=确认强趋势。
    依赖 compute_trend 的方向判定 + 持续性确认（连续 confirm_bars 根同向）。
    """
    c = np.asarray(c, dtype=float)
    n = len(c)
    trend = compute_trend(c, fast, slow)
    strong = np.zeros(n, dtype=int)
    run = 0  # 当前同向持续根数
    for i in range(1, n):
        if trend[i] == trend[i - 1] and trend[i] != 0:
            run += 1
        else:
            run = 1 if trend[i] != 0 else 0
        if run >= confirm_bars:
            strong[i] = trend[i]
    return strong


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


# --- 多周期 MACD（P3-1，2026-08-02 从 ml_build_dataset L128-143 抽取） ---
# 用途：大周期（60m/15m）MACD hist 方向做"方向一致"过滤——B 信号要求大周期 hist 为负
#   （大周期在下方，1m 抄底顺大势）、S 信号要求大周期 hist 为正（大周期在上方，1m 逃顶顺大势）。
# 语义（[2026-08-17 前视偏差修复] 已改为因果运行最大值，不再"严格等同"泄漏旧实现）：
#   - 周期边界 bar（idx%p==0）：rc = 该段截至当前 bar 的**因果运行 max**（不含未来 bar）
#   - 非边界 bar：rc = 段首 close（前向填充到最近周期边界）
#   然后对 rc 做标准 MACD(12,26,9)。

def compute_multi_period_macd(c, periods=(5, 15, 30, 60),
                              fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """多周期 MACD：对 1m close 按周期重采样（边界=段内截至当前 bar 的因果运行 max + 前向填充段首）后分别计算 MACD。

    返回 dict: {p: {'dif': np.array, 'dea': np.array, 'hist': np.array}}，数组长度与输入 c 一致。
    数据不足（n < p*2）的周期返回全 0 数组（与 ml_build_dataset 一致）。
    """
    c = np.asarray(c, dtype=float)
    n = len(c)
    out = {}
    idx = np.arange(n)
    for p in periods:
        if n >= p * 2:
            boundary = idx - (idx % p)          # 每根 bar 所属周期边界（段起始）
            # [2026-08-17 前视偏差修复] 原实现用 np.maximum.at(seg_max, boundary, c)
            # 求"段内 max close"，但段 [boundary(b), boundary(b)+p) 包含 b 之后的未来
            # bar —— 即 bar b 的 60m 重采样值 rc[b] 实际用到了同周期内未来分钟(如
            # 9:31 的 rc 含 10:30 收盘)，属周期内前视泄漏(AQuA 论文同款坑)。
            # 改为段内截至当前 bar 的**因果运行最大值**：rc[b] 仅依赖 c[0..b]。
            # 边界 bar(boundary=b)运行值= c[b]；非边界 bar 前向填充段首 close，
            # 二者均不触及未来。受 core/leak_guard.perturbation_test 栅栏守护。
            runmax = np.empty(n)
            prev_b = None
            run = 0.0
            for b in range(n):
                if boundary[b] != prev_b:
                    run = c[b]
                else:
                    run = run if c[b] <= run else c[b]
                runmax[b] = run
                prev_b = boundary[b]
            seg_max = runmax
            # 边界 bar 用段内(截至当前)running max，非边界 bar 前向填充段首 close
            rc = np.where(idx % p == 0, seg_max, c[boundary])
            p_dif, p_dea, p_hist = compute_macd(rc, fast=fast, slow=slow, signal=signal)
            out[p] = {'dif': p_dif, 'dea': p_dea, 'hist': p_hist}
        else:
            out[p] = {'dif': np.zeros(n), 'dea': np.zeros(n), 'hist': np.zeros(n)}
    return out


def macd_divergence_signal(h, lo, c, dif, dea, hist, i, w=LOCAL_W, min_hist_diff=0.0):
    """技巧三: 分时MACD背离确认

    卖点: 价格创新高 + MACD红柱缩短 + 快慢线死叉(或即将死叉)
    买点: 价格创新低 + MACD绿柱收缩 + 快慢线金叉(或即将金叉)

    "红柱缩短": hist > 0 但 hist[i] < hist[i-1] (多头动能衰减)
    "绿柱收缩": hist < 0 但 hist[i] > hist[i-1] (空头动能减弱)
    "死叉": DIF从上穿DEA -> DIF[i] < DEA[i] 且 DIF[i-1] >= DEA[i-1]
    "金叉": DIF从下穿DEA -> DIF[i] > DEA[i] 且 DIF[i-1] <= DEA[i-1]

    min_hist_diff: 背离强度阈值（2026-08-01 消融审查新增）。
      买点: hist[i] - 前窗hist最低 >= min_hist_diff 才放行（弱背离=噪音，实证负alpha）
      卖点: 前窗hist最高 - hist[i] >= min_hist_diff 才放行
      默认 0.0 = 生产行为（任何背离强度都放行）。

    返回: (factor: +1 buy / -1 sell / 0 neutral, detail: str)
    """
    if i < 2 or i < w:
        return 0, ''

    start = max(0, i - w)
    # 走平封板/一字/停牌：OHLC 全等，无有效极值，跳过（避免涨停顶平盘 bar 误判局部新高/新低→000938 虚假买点）
    if h[i] == lo[i]:
        return 0, ''
    # 严格极值判定：价格须严格超越【前序】窗口极值(不含自身)才算创新高/新低
    # 切片用 [start:i] 排除自身；若含自身则 h[i] 恒等于窗口max，严格 > 永远为 False
    local_high = h[i] > h[start:i].max()
    local_low = lo[i] < lo[start:i].min()

    # 金叉/死叉判定
    golden_cross = dif[i] > dea[i] and dif[i-1] <= dea[i-1]
    dead_cross = dif[i] < dea[i] and dif[i-1] >= dea[i-1]

    # 红柱缩短 / 绿柱收缩
    red_shrinking = hist[i] > 0 and hist[i] < hist[i-1]
    green_shrinking = hist[i] < 0 and hist[i] > hist[i-1]

    # --- 卖点: 价格新高 + (红柱缩短 OR 死叉) ---
    if local_high:
        reasons = []
        if red_shrinking:
            reasons.append('红柱缩短')
        if dead_cross:
            reasons.append('MACD死叉')
        # DIF>DEA但DIF拐头向下 -> "即将死叉"
        if not reasons and dif[i] > dea[i] and dif[i] < dif[i-1] and hist[i] > 0:
            reasons.append('DIF拐头')
        if reasons:
            # 背离强度门槛: hist 相对前窗最高点降低幅度
            if min_hist_diff > 0:
                prev_hist_max = hist[max(0, i - w):i].max()
                if (prev_hist_max - hist[i]) < min_hist_diff:
                    return 0, ''
            return -1, '+'.join(reasons)   # [v9.1.2] 卖点(价格新高)→-1 触发S

    # --- 买点: 价格新低 + (绿柱收缩 OR 金叉) ---
    if local_low:
        reasons = []
        if green_shrinking:
            reasons.append('绿柱收缩')
        if golden_cross:
            reasons.append('MACD金叉')
        # DIF<DEA但DIF拐头向上 -> "即将金叉"
        if not reasons and dif[i] < dea[i] and dif[i] > dif[i-1] and hist[i] < 0:
            reasons.append('DIF拐头')
        if reasons:
            # 背离强度门槛: hist 相对前窗最低点抬高幅度
            if min_hist_diff > 0:
                prev_hist_min = hist[max(0, i - w):i].min()
                if (hist[i] - prev_hist_min) < min_hist_diff:
                    return 0, ''
            return 1, '+'.join(reasons)    # [v9.1.2] 买点(价格新低)→+1 触发B

    return 0, ''


# ========== 统一指标计算 ==========

# 温度(temp)复合分量权重 + 周期 (与 indicators.py 一致, 供 monitor stars/level_val 用)
W_RSI, W_CHG, W_VR, W_DEV = 0.4, 0.2, 0.2, 0.2
RSI_PERIOD = 14
VOL_LOOKBACK = 20


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
    trend = compute_trend(c)
    # [轮次2-5 迭代] 强趋势确认数组：守卫（P2-1 ×1.5 地板收紧）改用确认趋势，
    # 避免 V 型反转日 EMA 滞后导致"误滤浅层有效抄底、放过深层接飞刀"（07-31 588000 实证）。
    trend_strong = compute_trend_strength(c)
    # 多周期 MACD（P3-1，2026-08-02）：仅算 60/15 两个周期（够用，避免全周期 O(n) 浪费）。
    # 大周期 hist 方向用于 mpr 方向过滤（默认关，见 detect_miji_signals mpr_enable 参数）。
    mp = compute_multi_period_macd(c, periods=(15, 60)) if n >= 30 else None
    # monitor 兼容字段 (与 indicators.py 口径一致): rsi / vol_ratio / temp
    rsi = compute_rsi(c)
    vol_ratio = compute_vol_ratio(v if real_vol else None)
    chg_pct = (c - pc) / pc * 100 if pc > 0 else np.zeros(n)
    chg_comp = np.clip((chg_pct + 5) / 10.0 * 100, 0, 100)
    vr_comp = np.clip(vol_ratio / 3.0 * 100, 0, 100)
    dev_pct = np.where(vwap > 0, (c - vwap) / vwap * 100, 0)
    dev_comp = np.clip((dev_pct + 2) / 4.0 * 100, 0, 100)
    temp = np.clip(W_RSI * rsi + W_CHG * chg_comp + W_VR * vr_comp + W_DEV * dev_comp, 0, 100)

    data = {
        'o': o, 'h': h, 'lo': lo, 'c': c, 'n': n,
        'v': v if real_vol else None, 'has_vol': real_vol,
        'vwap': vwap, 'atr': atr, 'pc': pc,
        'dif': dif, 'dea': dea, 'hist': hist,
        'trend': trend, 'trend_strong': trend_strong,
        'rsi': rsi, 'vol_ratio': vol_ratio, 'temp': temp,
    }
    if mp is not None:
        data['macd60_hist'] = mp[60]['hist']
        data['macd60_dif'] = mp[60]['dif']
        data['macd15_hist'] = mp[15]['hist']
        data['macd15_dif'] = mp[15]['dif']
    else:
        data['macd60_hist'] = np.zeros(n)
        data['macd60_dif'] = np.zeros(n)
        data['macd15_hist'] = np.zeros(n)
        data['macd15_dif'] = np.zeros(n)
    return data


# ========== 共振信号检测 ==========
# 反T收盘B的"放行窗口": S开出反T后, 仅在该窗口(bar)内的收盘B豁免趋势过滤,
# 超时未回补则视为反T放弃, 恢复正常趋势过滤(避免豁免窗口无限延续、把全天下跌段B全放进)。
REV_CLOSE_BARS = 30  # 默认30分钟(1m K线); 可调


def _mpr_hist_vals(data, i, periods):
    """取当前 bar 的多周期 hist 值数组；数据不足（全 0 / 缺失）返回 None（自动降级跳过）。"""
    vals = []
    for p in periods:
        key = f'macd{p}_hist'
        arr = data.get(key)
        if arr is None or i >= len(arr):
            return None
        h = float(arr[i])
        if h == 0.0:
            return None   # 0 = 数据不足（前 60 根前向填充未成形），降级跳过
        vals.append(h)
    return vals


def _mpr_buy_ok(data, i, periods=MPR_PERIODS):
    """B 侧多周期方向过滤：大周期 hist 均 <0（大周期在下方，顺大势抄底）。"""
    vals = _mpr_hist_vals(data, i, periods)
    return vals is not None and all(v < 0 for v in vals)


def _mpr_sell_ok(data, i, periods=MPR_PERIODS):
    """S 侧多周期方向过滤：大周期 hist 均 >0（大周期在上方，顺大势逃顶）。"""
    vals = _mpr_hist_vals(data, i, periods)
    return vals is not None and all(v > 0 for v in vals)

def detect_miji_signals(data, pc, start_idx=2,
                        max_b=MAX_B_DAILY, max_s=MAX_S_DAILY,
                        min_resonance=RESONANCE_THRESHOLD,
                        b_trend_filter=False, allow_reverse=True,
                        macd_gate_mode=MACD_GATE_MODE, require_macd=None,
                        enable=(True, True, True), vol_in_gate=False,
                        macd_min_hist_diff=0.0, atr_min_pct=None,
                        is_morning=None, morning_min_hist_diff=0.0,
                        mpr_enable=False, mpr_periods=MPR_PERIODS,
                        vwap_dev_ceil=None, atr_min_pct_s=None):
    """做T秘籍三因子共振信号检测

    共振公式 (MD文档核心):
      最佳买点 = 价格新低(急跌远离均线) + 成交量放大(底背离) + MACD绿柱收缩
      最佳卖点 = 价格新高(急拉远离均线) + 成交量萎缩(顶背离) + MACD红柱缩短
      >=2项同时满足时执行

    每条信号含:
      type/idx/price/chg/resonance_score/factors/detail
      factors 为 dict: {'gravity': +1/0/-1, 'vol_div': +1/0/-1, 'macd_div': +1/0/-1}

    enable: 消融开关三元组 (gravity, vol_div, macd_div)，默认全开 = 生产行为。
            关掉某因子 -> 该因子恒为 0（不参与共振/门控打分，但其余因子独立打分不受影响）。
            与 backtest/keyfactor/miji_engine.py 消融口径一致。
    vol_in_gate: 生产架构里 vol 因子只记录共振分数、不参与门控放行（gate 只看 g/m）。
                 True 时 vol 因子也参与放行（m_only 时 floor 通道需 g 因子仍在，故 g 不能关）。
                 默认 False = 生产行为。
    macd_min_hist_diff: MACD 背离强度阈值（默认 0=研究态不过滤；生产走 check_miji_trigger 0.15）。
    atr_min_pct: ATR 波动率下限门槛 %（None=关闭；B 侧生效，与 _gate_floor.gate_buy 语义一致）。
    is_morning: 早盘标记数组（09:30-10:00=1），None=不启用早盘放宽。
    morning_min_hist_diff: 早盘时段 B 的 hist 强度门槛（默认 0=关闭，2026-08-02 实证后默认关；
                详见 MORNING_MHD_THRESHOLD 注释——hist 强度差<0.05 占 78%，放宽会放回噪音）。
    mpr_enable: 多周期 MACD 方向过滤（P3-1，默认 False=关闭=生产行为）。支持：
                False/None = 关闭；
                'B' = 仅 B 侧过滤（B 需 data['macd60_hist'][i]<0 且 data['macd15_hist'][i]<0，顺大势抄底）；
                'S' = 仅 S 侧过滤（S 需 data['macd60_hist'][i]>0 且 data['macd15_hist'][i]>0，顺大势逃顶）；
                True/'both' = B/S 双侧。
                数据不足（数组全 0/缺失）自动跳过该 bar（早盘降级）。
    mpr_periods: 参与方向过滤的大周期（默认 (60,15)）。
    vwap_dev_ceil: S 侧 VWAP 偏离上限 %（None=关闭；P3-2 S 信号专项，与 _gate_floor.gate_sell
                语义一致——极端偏离追高过远时 S 胜率回落，> 上限禁止 S）。
    atr_min_pct_s: S 侧 ATR 波动率下限门槛 %（None=关闭；与 B 侧 atr_min_pct 对称）。
    """
    if require_macd is not None:
        macd_gate_mode = 'strict' if require_macd else 'off'
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
        # 早盘放宽（2026-08-01 报告落地）：09:30-10:00 内 B 的 hist 强度门槛降低
        # 报告 B_is_morning 0.4278 vs 非早盘 0.3653；早盘更活跃、弱背离容忍度更高。
        eff_mhd = macd_min_hist_diff
        if is_morning is not None and i < len(is_morning) and is_morning[i]:
            eff_mhd = min(eff_mhd, morning_min_hist_diff) if macd_min_hist_diff > 0 else macd_min_hist_diff
        m_factor, m_detail = macd_divergence_signal(h, lo, c, dif, dea, hist, i,
                                                    min_hist_diff=eff_mhd)

        # ---- 消融开关: enable=(gravity, vol_div, macd_div) ----
        # 关掉某因子 -> 该因子恒为 0, 不影响其余因子的独立打分与共振/门控统计。
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
                # ---- MACD 门控（分级, 与 check_miji_trigger 同构）----
                if macd_gate_mode == 'off':
                    buy_pass = (g_factor == 1)   # 方案1: 纯引力B(价格超跌即买, 无视MACD)
                elif macd_gate_mode in ('strict', 'floor'):
                    if i < LOCAL_W:
                        buy_pass = (g_factor == 1)   # [v9.1.2] 早盘降级 gravity-only
                    else:
                        # 生产: 仅 m_factor 决定基础放行; vol_in_gate=True 时 vol 也参与
                        if vol_in_gate:
                            buy_pass = (m_factor == 1 or v_factor == 1)
                        else:
                            buy_pass = (m_factor == 1)
                else:
                    buy_pass = False
                if macd_gate_mode == 'floor':
                    # [P2-1 迭代] 单边行情守卫：强下跌(trend==-1)时地板阈值×1.5
                    # [轮次2-5 迭代] 改用 trend_strong + 当日跌幅门控（day_chg < -3%）：
                    #   实证发现 EMA 趋势在 V 型反转日滞后（07-31 588000 盘中下探但收涨+3.5%，
                    #   trend 全天-1 导致浅层有效抄底被 ×1.5 误滤）；
                    #   只有"当日整体真下跌"才确认强下跌，收紧地板。
                    eff_floor = FLOOR_DEV_PCT
                    strong_dn = (HIGH_VOL_GUARD and data.get('trend_strong') is not None
                                 and data['trend_strong'][i] == -1 and day_chg < -3.0)
                    if strong_dn:
                        eff_floor = FLOOR_DEV_PCT * 1.5
                    buy_floor = _is_new_low(c, lo, i) and (g_dev <= -eff_floor)
                    buy_pass = bool(buy_pass or buy_floor)
                else:
                    buy_floor = False
                # ---- ATR 波动率门控（2026-08-01 报告落地；B 侧，与 _gate_floor.gate_buy 语义一致） ----
                # 低波动区（atr/c*100 < atr_min_pct）= 无肉可做，禁止 B（含 floor 地板通道）
                if atr_min_pct is not None:
                    atr_ok = ((atr[i] / c[i] * 100.0) >= atr_min_pct)
                    buy_pass = bool(buy_pass and atr_ok)
                # ---- 多周期 MACD 方向过滤（P3-1，2026-08-02；默认关） ----
                # B 信号要求大周期 hist 为负（大周期在下方，1m 抄底顺大势）。
                # 早盘/数据不足（hist 全 0）自动跳过该 bar（mpr 降级）。
                # mpr_enable 支持 'B'/'S'/'both' 分侧。
                if mpr_enable in (True, 'both', 'B'):
                    mpr_ok = _mpr_buy_ok(data, i, mpr_periods)
                    buy_pass = bool(buy_pass and mpr_ok)
                if buy_pass:
                    details = []
                    if g_factor == 1: details.append(f'均线引力(dev={g_dev:.2f}%)')
                    if v_factor == 1: details.append(f'量价{v_detail}')
                    if m_factor == 1: details.append(f'MACD{m_detail}')
                    if buy_floor: details.append(f'价格地板(新低dev={g_dev:.2f}%)')
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
            # ---- MACD 门控（分级, 与 check_miji_trigger 同构）----
            if macd_gate_mode == 'off':
                sell_pass = (g_factor == -1)   # 方案1: 纯引力S(价格超买即卖, 无视MACD)
            elif macd_gate_mode in ('strict', 'floor'):
                if i < LOCAL_W:
                    sell_pass = (g_factor == -1)
                else:
                    # 生产: 仅 m_factor 决定基础放行; vol_in_gate=True 时 vol 也参与
                    if vol_in_gate:
                        sell_pass = (m_factor == -1 or v_factor == -1)
                    else:
                        sell_pass = (m_factor == -1)
            else:
                sell_pass = False
            if macd_gate_mode == 'floor':
                # [P2-1 迭代] 强上涨(trend==1)时天花板阈值×1.5（防过早卖飞）
                # [轮次2-5 迭代] 与 B 侧对称：trend_strong + 当日涨幅门控（day_chg > +3%）。
                eff_ceil = FLOOR_DEV_PCT
                strong_up = (HIGH_VOL_GUARD and data.get('trend_strong') is not None
                             and data['trend_strong'][i] == 1 and day_chg > 3.0)
                if strong_up:
                    eff_ceil = FLOOR_DEV_PCT * 1.5
                sell_ceil = _is_new_high(c, h, i) and (g_dev >= eff_ceil)
                sell_pass = bool(sell_pass or sell_ceil)
            else:
                sell_ceil = False
            # ---- 多周期 MACD 方向过滤（P3-1，2026-08-02；默认关） ----
            # S 信号要求大周期 hist 为正（大周期在上方，1m 逃顶顺大势）。
            if mpr_enable in (True, 'both', 'S'):
                mpr_ok = _mpr_sell_ok(data, i, mpr_periods)
                sell_pass = bool(sell_pass and mpr_ok)
            # ---- S 侧偏离上限 + ATR 门控（P3-2，2026-08-02；默认关） ----
            # vwap_dev_ceil: 极端偏离（追高过远）= 弹簧拉伸过度，S 胜率回落 → 上限过滤
            if vwap_dev_ceil is not None:
                dev_ok = (vwap[i] <= 0) or (((c[i] - vwap[i]) / vwap[i] * 100.0) <= vwap_dev_ceil)
                sell_pass = bool(sell_pass and dev_ok)
            # atr_min_pct_s: 低波动区（atr/c*100 < 阈值）= 无肉可做，禁止 S（与 B 侧对称）
            if atr_min_pct_s is not None:
                atr_ok = ((atr[i] / c[i] * 100.0) >= atr_min_pct_s)
                sell_pass = bool(sell_pass and atr_ok)
            if sell_pass:
                details = []
                if g_factor == -1: details.append(f'均线引力(dev={g_dev:.2f}%)')
                if v_factor == -1: details.append(f'量价{v_detail}')
                if m_factor == -1: details.append(f'MACD{m_detail}')
                if sell_ceil: details.append(f'价格天花板(新高dev={g_dev:.2f}%)')
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

def check_miji_trigger(data, i, min_resonance=RESONANCE_THRESHOLD,
                       macd_gate_mode=MACD_GATE_MODE, min_hist_diff=MHD_THRESHOLD,
                       atr_min_pct=None, mpr_enable=None, mpr_periods=MPR_PERIODS,
                       vwap_dev_ceil=None, atr_min_pct_s=None):
    """单bar三因子共振判定, 供monitor实时调用.

    返回: (b_triggered, s_triggered, b_detail, s_detail, snapshot)
    snapshot = {'gravity': ..., 'vol_div': ..., 'macd_div': ..., 'b_score': ..., 's_score': ...}

    min_hist_diff: MACD 背离强度阈值（默认 MHD_THRESHOLD=0.15，生产已接入）。
    atr_min_pct: ATR 波动率下限门槛 %（None=关闭=现状；0.20~0.30 候选，P1 验证后接入）。
    mpr_enable: 多周期 MACD 方向过滤（P3-1，默认 None=取模块级 MPR_ENABLE；None 且 MPR_ENABLE=False
                则关闭=生产行为）。B 需大周期 hist<0、S 需大周期 hist>0。
    mpr_periods: 参与方向过滤的大周期（默认 (60,15)）。
    vwap_dev_ceil: S 侧 VWAP 偏离上限 %（None=关闭=现状；P3-2 S 信号专项，
                极端偏离追高过远时 S 胜率回落，> 上限禁止 S）。
    atr_min_pct_s: S 侧 ATR 波动率下限门槛 %（None=关闭=现状；与 B 侧 atr_min_pct 对称）。
    """
    c = data['c']; h = data['h']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; v = data['v']
    dif = data['dif']; dea = data['dea']; hist = data['hist']

    if atr[i] <= 0:
        return False, False, '', '', {}

    g_factor, g_dev = gravity_signal(c, vwap, atr, i)
    v_factor, v_detail = volume_divergence_signal(h, lo, c, v, i) if v is not None else (0, '')
    # 早盘放宽（研究项，2026-08-02 实证后默认关闭）：09:30-10:00 内 B 的 hist 强度门槛降低
    # ⚠️ MORNING_MHD_THRESHOLD=0（默认）时关闭；>0 时才启用。
    # 实证：hist 强度差<0.05 占 78.3%，放宽会把 P0(0.15) 过滤的弱背离噪音放回，与 P0 冲突。
    eff_mhd = min_hist_diff
    if MORNING_MHD_THRESHOLD > 0 and data.get('is_morning') is not None \
            and i < len(data['is_morning']) and data['is_morning'][i]:
        eff_mhd = min(min_hist_diff, MORNING_MHD_THRESHOLD) if min_hist_diff > 0 else min_hist_diff
    m_factor, m_detail = macd_divergence_signal(h, lo, c, dif, dea, hist, i,
                                                min_hist_diff=eff_mhd)

    buy_score = sum(1 for f in [g_factor, v_factor, m_factor] if f == 1)
    sell_score = sum(1 for f in [g_factor, v_factor, m_factor] if f == -1)

    # day_chg: 当日涨跌幅(%), 供 floor 门控的涨停日天花板S抑制使用。
    # 注意：check_miji_trigger 不接收 pc 参数，需从 data['pc'] 取昨收；
    # 缺失时降级为 0.0（抑制不触发，等效于不限制天花板S）。
    # 2026-07-22 修复：原代码引用未定义的 day_chg → 每个 S 判定 NameError 崩溃。
    pc = data.get('pc')
    day_chg = (c[i] / pc - 1) * 100 if (pc and pc > 0) else 0.0

    # ---- MACD 门控（分级，委托 _gate_floor 共享模块）----
    b_base = s_base = False
    b_floor = s_ceil = False

    # [P2-1 迭代] 单边行情守卫：接通 _gate_floor 的 trend_state/floor_trend_threshold。
    #   强下跌(trend==-1)时地板 B 阈值 ×1.5 加深（避免接飞刀）；强上涨(trend==1)时
    #   天花板 S 阈值 ×1.5 抬高（避免过早卖飞）。strict 模式不受影响（无 floor 通道）。
    # [轮次2-5 迭代] 改用 trend_strong（连续 8 根确认）+ 当日涨跌门控：
    #   - 原始 trend 在 V 型反转日滞后（07-31 588000 盘中下探但收涨 +3.5%，trend 全天 -1），
    #     把有效浅层地板误滤、深层失效接飞刀放行；
    #   - 只有"当日整体真下跌(day_chg<-3%) 且趋势确认"才收紧地板；
    #     "当日整体真上涨(day_chg>+3%) 且趋势确认"才抬高天花板。
    #   monitor 生产路径与回测 detect_miji_signals 同步。
    trend_state = int(data['trend_strong'][i]) if ('trend_strong' in data and data['trend_strong'] is not None
                                                  and i < len(data['trend_strong'])) else 0
    # 当日涨跌门控：非单边日（|day_chg|<=3%）视为震荡，守卫不收紧
    if trend_state == -1 and day_chg >= -3.0:
        trend_state = 0
    elif trend_state == 1 and day_chg <= 3.0:
        trend_state = 0
    guard_active = HIGH_VOL_GUARD and macd_gate_mode == 'floor' and trend_state != 0

    b_trig, b_base, b_floor = gate_buy(
        g_factor, m_factor, g_dev, i, macd_gate_mode=macd_gate_mode,
        c=c, lo=lo, last_buy_floor_bar=-999,
        trend_state=trend_state if guard_active else 0,
        floor_trend_threshold=2.0 if guard_active else 0.0,
        atr_min_pct=atr_min_pct, atr=atr,
    )
    # gate_buy 返回 (buy_pass, buy_base, buy_floor)
    
    s_trig, s_base, s_ceil = gate_sell(
        g_factor, m_factor, g_dev, i, macd_gate_mode=macd_gate_mode,
        c=c, h=h, vwap=vwap, atr=atr, day_chg=day_chg, last_sell_ceil_bar=-999,
        trend_state=trend_state if guard_active else 0,
        floor_trend_threshold=2.0 if guard_active else 0.0,
        vwap_dev_ceil=vwap_dev_ceil, atr_min_pct_s=atr_min_pct_s,
    )

    # ---- 多周期 MACD 方向过滤（P3-1，2026-08-02；默认关=生产行为） ----
    # mpr_enable 参数 > 模块级 MPR_ENABLE（None=跟随模块级，默认 False）。
    # 支持 'B'/'S'/'both' 分侧。
    mpr_on = MPR_ENABLE if mpr_enable is None else mpr_enable
    if mpr_on in (True, 'both', 'B') and b_trig and not _mpr_buy_ok(data, i, mpr_periods):
        b_trig = False
    if mpr_on in (True, 'both', 'S') and s_trig and not _mpr_sell_ok(data, i, mpr_periods):
        s_trig = False

    b_detail = ''
    if b_trig:
        parts = []
        if g_factor == 1: parts.append(f'均线引力(dev={g_dev:.2f}%)')
        if v_factor == 1: parts.append(f'量价{v_detail}')
        if m_factor == 1: parts.append(f'MACD{m_detail}')
        if b_floor: parts.append(f'价格地板(新低dev={g_dev:.2f}%)')
        b_detail = ' + '.join(parts)

    s_detail = ''
    if s_trig:
        parts = []
        if g_factor == -1: parts.append(f'均线引力(dev={g_dev:.2f}%)')
        if v_factor == -1: parts.append(f'量价{v_detail}')
        if m_factor == -1: parts.append(f'MACD{m_detail}')
        if s_ceil: parts.append(f'价格天花板(新高dev={g_dev:.2f}%)')
        s_detail = ' + '.join(parts)

    snapshot = {
        'gravity': g_factor, 'vol_div': v_factor, 'macd_div': m_factor,
        'g_dev': round(g_dev, 2),
        'b_score': buy_score, 's_score': sell_score,
    }
    return b_trig, s_trig, b_detail, s_detail, snapshot




# ========== monitor 适配器 (T2.3): 沿用 indicators 函数名, monitor 最小改动 ==========
# monitor 调 check_b_trigger(data,i)->(bool,reason) / check_s_trigger(data,i)->(bool,reason);
# 这里把 check_miji_trigger(合一返回) 拆为分立 B/S, 默认走生产 require_macd=REQUIRE_MACD(macd-required 门控)。
def check_b_trigger(data, i, min_resonance=RESONANCE_THRESHOLD,
                    macd_gate_mode=MACD_GATE_MODE, min_hist_diff=MHD_THRESHOLD,
                    atr_min_pct=None, mpr_enable=None, mpr_periods=MPR_PERIODS):
    """B 信号触发判定 (monitor 兼容). 返回 (triggered: bool, reason: str).

    mpr_enable/mpr_periods: 多周期 MACD 方向过滤（P3-1）透传 check_miji_trigger。
    None=取模块级 MPR_ENABLE（默认关=生产行为）；'B'/'both' 时 B 需大周期 hist<0。
    """
    b, _, bd, _, _ = check_miji_trigger(data, i, min_resonance,
                                        macd_gate_mode=macd_gate_mode,
                                        min_hist_diff=min_hist_diff,
                                        atr_min_pct=atr_min_pct,
                                        mpr_enable=mpr_enable, mpr_periods=mpr_periods)
    return (b, bd)


def check_s_trigger(data, i, min_resonance=RESONANCE_THRESHOLD,
                    macd_gate_mode=MACD_GATE_MODE, min_hist_diff=MHD_THRESHOLD,
                    atr_min_pct=None, mpr_enable=None, mpr_periods=MPR_PERIODS,
                    vwap_dev_ceil=None, atr_min_pct_s=None):
    """S 信号触发判定 (monitor 兼容). 返回 (triggered: bool, reason: str).

    mpr_enable/mpr_periods: 多周期 MACD 方向过滤（P3-1）透传 check_miji_trigger。
    None=取模块级 MPR_ENABLE（默认关=生产行为）；'S'/'both' 时 S 需大周期 hist>0。
    vwap_dev_ceil/atr_min_pct_s: S 侧专项参数（P3-2）透传 check_miji_trigger。
    """
    _, s, _, sd, _ = check_miji_trigger(data, i, min_resonance,
                                        macd_gate_mode=macd_gate_mode,
                                        min_hist_diff=min_hist_diff,
                                        atr_min_pct=atr_min_pct,
                                        mpr_enable=mpr_enable, mpr_periods=mpr_periods,
                                        vwap_dev_ceil=vwap_dev_ceil,
                                        atr_min_pct_s=atr_min_pct_s)
    return (s, sd)
