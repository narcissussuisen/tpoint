"""core/ml_features.py — ML 39 特征单一实现（2026-09-28 重建）

## 重建背景
原模块在 v10.0.0 灾难恢复（2026-08-05）中丢失，**从未入 git**。本文件依据以下
幸存证据重建，特征名单与顺序以 `output/ml_versions.json` 的 feature_names
为**唯一权威**（monitor._load_ml_model 对其做逐字硬校验，错位即拒绝加载）。

## 证据来源与口径分级
✅ 实证口径（有幸存文档/代码直接佐证）：
  - 名单与顺序：`output/ml_versions.json`（两个注册版本 feature_names 完全一致）
  - vwap_dev=(c-vwap)/vwap*100、hist_pct=hist/vwap*100：
    `output/ml_vs_prod_mismatch_report_2026-08-02.html` 维度一表逐字记载
  - is_morning='09:30'<=hhmm<'10:00'：monitor.py compute() 注释
    「口径与 scripts/ml_build_dataset.py 一致（2026-08-01 报告 B_is_morning 0.4278 优势）」
  - is_tail=hhmm>='14:30'：`output/ml_rules.json` B_is_tail 注记「是否尾盘(14:30后)」
  - atr_pct 为百分数口径：`output/ml_rules.json` B_atr_pct 分箱 [0.001,0.120...]
  - pos_in_day=(i+1)/240：`output/ml_rules.json` B/S_pos_in_day 分箱反推——
    max=0.750 恰为 180/240（数据集标签 N<=60 要求 i+60<240 ⇒ i<=179 ⇒ (i+1)/240=0.750），
    min=0.008 恰为 2/240；且 B/S_is_tail 全 0（14:30 后无样本）与 i<=179 互洽。
  - ctx 四列（g/v/m_factor/resonance）：monitor._ml_ctx_from_snapshot 注释
    「训练侧 ctx = s['factors']（gravity/vol_div/macd_div）+ resonance_score」，
    服务侧由 check_miji_trigger snapshot 同源供给。
  - 多周期 MACD 用因果版 compute_multi_period_macd（2026-08-17 前视修复版），
    KDJ(9,3,3) 用 primitives.compute_kdj——train-serve 同源是 2026-08-02 对齐专项
    核心结论，**禁止另起口径**。

⚠️ 推断口径（无逐字幸存者，按命名/量纲/对称性推断，已在下文逐项标注）：
  bar_idx_frac / rsi_dist_30/70 / atr_chan_up1/dn1 / mom_1/5/15 / is_noon。
  重建后 B2/B3 用本模块重建数据集并重训模型，train-serve 天然同源；
  推断列与原丢失实现的逐比特一致不可考，**内部一致性 + 因果性**为验收标准。

## 因果性
全部特征仅依赖 bar i 及以前数据（原语均受 core/leak_guard.perturbation_test
守护；本模块整体在 tests/test_ml_features.py 挂同一栅栏回归）。

## 接口（monitor 消费契约，勿改签名）
  FEAT_ALL: list[str]                      # 39 个特征名，顺序权威
  build_feature_row(data, pc, i, sub, ctx) -> np.ndarray(39,) | None
      data: compute_miji_indicators 的输出 dict（另需 'h'/'lo'/'n' 键）
      pc:   昨收（prev close）
      i:    信号 bar 行号（0 起）
      sub:  当日分时 DataFrame（需 'trade_time' 列；用于时段哑变量，可缺省）
      ctx:  {'g_factor','v_factor','m_factor','resonance'}，可 None（补 0）
      返回 None = 数据不足/异常，服务侧 fail-open 原样放行（monitor score_signal）
"""
import numpy as np

from miji_alpha import compute_multi_period_macd
from primitives import compute_kdj

# ========== 特征名单（39 列，唯一权威 = output/ml_versions.json feature_names） ==========
# ⚠️ 顺序即契约：monitor._load_ml_model 对模型内 feature_names 做逐字比对，
#    任何增删/换序都会让存量模型被拒绝加载；变更必须同步重训并注册新版本。
FEAT_ALL = [
    # --- 基础 14 ---
    'vwap_dev',       # (c-vwap)/vwap*100               ✅ 实证（mismatch 报告）
    'atr_pct',        # atr/c*100                       ✅ 实证（bins 百分数量纲）
    'dif',            # MACD DIF                        ✅ 原语
    'dea',            # MACD DEA                        ✅ 原语
    'hist',           # MACD HIST = 2*(dif-dea)         ✅ 原语（通达信标准）
    'hist_pct',       # hist/vwap*100                   ✅ 实证（mismatch 报告）
    'trend',          # compute_trend                   ✅ 原语
    'trend_strong',   # compute_trend_strength          ✅ 原语
    'rsi',            # RSI(14) Wilder                  ✅ 原语
    'vol_ratio',      # 量比(20)                        ✅ 原语
    'temp',           # 温度复合                        ✅ 原语
    'chg',            # (c-pc)/pc*100                   ✅ 与 monitor chg 一致
    'pos_in_day',     # (i+1)/240                       ✅ bins 反推（max=0.750）
    'bar_idx_frac',   # i/n                             ⚠️ 推断（报告称与 pos_in_day 几乎重复）
    # --- 多周期 MACD 8（因果版重采样） ---
    'macd5_dif', 'macd5_hist',
    'macd15_dif', 'macd15_hist',
    'macd30_dif', 'macd30_hist',
    'macd60_dif', 'macd60_hist',
    # --- 补充 9 ---
    'rsi_dist_30',    # rsi-30                          ⚠️ 推断（距超卖线距离，符号自定）
    'rsi_dist_70',    # rsi-70                          ⚠️ 推断（距超买线距离）
    'kdj_k', 'kdj_d', 'kdj_j',  # KDJ(9,3,3)            ✅ primitives 原语
    'atr_chan_up1',   # (c-(vwap+1*atr))/atr            ⚠️ 推断（上 1×ATR 轨外的 ATR 倍数，>0=出轨）
    'atr_chan_dn1',   # ((vwap-1*atr)-c)/atr            ⚠️ 推断（下 1×ATR 轨外的 ATR 倍数，>0=出轨）
    'mom_1',          # (c[i]/c[i-1]-1)*100             ⚠️ 推断（收益率口径，%）
    'mom_5',          # (c[i]/c[i-5]-1)*100             ⚠️ 推断
    'mom_15',         # (c[i]/c[i-15]-1)*100            ⚠️ 推断
    # --- 时段哑变量 3 ---
    'is_morning',     # '09:30'<=hhmm<'10:00'           ✅ 实证（monitor 口径一致注释）
    'is_noon',        # '11:00'<=hhmm<'13:30'           ⚠️ 推断（午休前后窗口；无幸存者口径）
    'is_tail',        # hhmm>='14:30'                   ✅ 实证（ml_rules 注记）
    # --- 信号上下文 4（服务侧由 check_miji_trigger snapshot 同源供给） ---
    'g_factor', 'v_factor', 'm_factor', 'resonance',
]

TOTAL_BARS = 240  # 标准交易日 1m bar 数（09:31-11:30 共 120 + 13:01-15:00 共 120）

# 时段窗口（hhmm 字符串比较，与 monitor is_morning 同法）
_MORNING_LO, _MORNING_HI = '09:30', '10:00'
_NOON_LO, _NOON_HI = '11:00', '13:30'      # ⚠️ 推断窗口（覆盖午休前后：上午尾段+下午首段）
_TAIL_LO = '14:30'

# 无时间戳时的 bar 序号近似（标准 240 bar 网格：0-119=09:31-11:30，120-239=13:01-15:00）
_IDX_MORNING = (0, 28)        # 09:31-09:59（bar 按收盘时刻标记）
_IDX_NOON = (90, 148)         # 11:00-11:30 ∪ 13:01-13:29
_IDX_TAIL = 210               # 14:30 起


def _hhmm_at(sub, i):
    """从 sub（当日分时 DataFrame）取第 i 根 bar 的 'HH:MM'；失败返回 None。"""
    try:
        col = sub['trade_time']
        s = str(col.iloc[i] if hasattr(col, 'iloc') else col[i])
        return s[11:16] if len(s) >= 16 else None
    except Exception:
        return None


def _time_flags(data, i, sub, n):
    """时段哑变量 (is_morning, is_noon, is_tail)，均为 0.0/1.0。

    优先级：sub 时间戳（权威） > data['is_morning'] 数组（仅 morning） > bar 序号近似。
    """
    hhmm = _hhmm_at(sub, i) if sub is not None else None
    if hhmm:
        return (float(_MORNING_LO <= hhmm < _MORNING_HI),
                float(_NOON_LO <= hhmm < _NOON_HI),
                float(hhmm >= _TAIL_LO))
    # 无时间戳：morning 优先用生产数组（与 monitor 同口径），其余按序号近似
    is_m = None
    try:
        arr = data.get('is_morning')
        if arr is not None:
            is_m = float(arr[i])
    except Exception:
        is_m = None
    if is_m is None:
        is_m = float(_IDX_MORNING[0] <= i <= _IDX_MORNING[1])
    is_n = float(_IDX_NOON[0] <= i <= _IDX_NOON[1])
    is_t = float(i >= _IDX_TAIL)
    return is_m, is_n, is_t


def build_feature_row(data, pc, i, sub=None, ctx=None):
    """构造信号点 39 维特征行。返回 np.ndarray(39,) 或 None（fail-open）。

    仅依赖 bar i 及以前数据（因果）。任何数据不足/除零/非有限值 → None，
    由服务侧（monitor.score_signal）按 keep 原样放行。
    """
    try:
        c = data['c']; h = data['h']; lo = data['lo']
        vwap = data['vwap']; atr = data['atr']
        dif = data['dif']; dea = data['dea']; hist = data['hist']
        trend = data['trend']; trend_strong = data['trend_strong']
        rsi = data['rsi']; vol_ratio = data['vol_ratio']; temp = data['temp']
        n = int(data.get('n') or len(c))
        i = int(i)
        if not (0 <= i < n) or c[i] <= 0 or vwap[i] <= 0:
            return None

        a = float(atr[i])
        feats = {
            'vwap_dev': (float(c[i]) - float(vwap[i])) / float(vwap[i]) * 100.0,
            'atr_pct': a / float(c[i]) * 100.0,
            'dif': float(dif[i]),
            'dea': float(dea[i]),
            'hist': float(hist[i]),
            'hist_pct': float(hist[i]) / float(vwap[i]) * 100.0,
            'trend': float(trend[i]),
            'trend_strong': float(trend_strong[i]),
            'rsi': float(rsi[i]),
            'vol_ratio': float(vol_ratio[i]),
            'temp': float(temp[i]),
            'chg': (float(c[i]) - float(pc)) / float(pc) * 100.0 if pc and pc > 0 else 0.0,
            'pos_in_day': (i + 1) / float(TOTAL_BARS),
            'bar_idx_frac': i / float(n) if n > 0 else 0.0,
        }

        # 多周期 MACD（因果版原语，单次计算四周期；数据不足的周期原语内部补 0）
        mp = compute_multi_period_macd(c, periods=(5, 15, 30, 60))
        for p in (5, 15, 30, 60):
            feats[f'macd{p}_dif'] = float(mp[p]['dif'][i])
            feats[f'macd{p}_hist'] = float(mp[p]['hist'][i])

        feats['rsi_dist_30'] = float(rsi[i]) - 30.0
        feats['rsi_dist_70'] = float(rsi[i]) - 70.0

        k, d, j = compute_kdj(h, lo, c)
        feats['kdj_k'] = float(k[i])
        feats['kdj_d'] = float(d[i])
        feats['kdj_j'] = float(j[i])

        if a > 1e-12:
            feats['atr_chan_up1'] = (float(c[i]) - (float(vwap[i]) + a)) / a
            feats['atr_chan_dn1'] = ((float(vwap[i]) - a) - float(c[i])) / a
        else:
            feats['atr_chan_up1'] = 0.0
            feats['atr_chan_dn1'] = 0.0

        for kk in (1, 5, 15):
            if i >= kk and c[i - kk] > 0:
                feats[f'mom_{kk}'] = (float(c[i]) / float(c[i - kk]) - 1.0) * 100.0
            else:
                feats[f'mom_{kk}'] = 0.0

        is_m, is_n, is_t = _time_flags(data, i, sub, n)
        feats['is_morning'] = is_m
        feats['is_noon'] = is_n
        feats['is_tail'] = is_t

        ctx = ctx or {}
        for key in ('g_factor', 'v_factor', 'm_factor', 'resonance'):
            try:
                feats[key] = float(ctx.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                feats[key] = 0.0

        row = np.array([feats[name] for name in FEAT_ALL], dtype=float)
        if row.shape != (len(FEAT_ALL),) or not np.all(np.isfinite(row)):
            return None
        return row
    except Exception:
        return None


def build_feature_matrix(data, pc, sub=None, ctx_fn=None):
    """逐 bar 构造全特征矩阵（n, 39）。供离线数据集构建 / 因果性栅栏使用。

    ctx_fn(i) -> dict|None：第 i 根 bar 的信号上下文（离线侧由
    check_miji_trigger snapshot 供给）；None 时全部 ctx 列补 0。
    行 i 若 build_feature_row 返回 None → 该行为全 NaN（调用方负责丢弃）。
    """
    n = int(data.get('n') or len(data['c']))
    out = np.full((n, len(FEAT_ALL)), np.nan)
    for i in range(n):
        ctx = ctx_fn(i) if ctx_fn else None
        row = build_feature_row(data, pc, i, sub, ctx)
        if row is not None:
            out[i] = row
    return out
