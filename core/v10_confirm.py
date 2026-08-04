#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""v10_confirm.py — v10.0.0 量能确认过滤器（2026-08-05 · v10.0.0 分支）

来源与依据：
- 借鉴 CSDN MACD+RSI 三重共振的量能腿（B 需缩量回调=抛压衰竭；S 需放量滞涨=动能衰减）
  与掘金量比口径；open_source_survey.md 可借鉴点 #4。
- 0805 消融回测（F盘全历史·无前视尾窗）：v9.3.0 基线 池级 ret 13.08/wr 53.9%/dd 17.88/sharpe 0.71
  → F2 量能确认 ret 18.8(+43.7%)/wr 55.5%/dd 15.73/sharpe 1.18，全面优于基线。
- 逐标的：161129/688111/300058 全面改善；600570 ret -0.22→-4.21 单只劣化>2pp、513310 略降
  → 按 per-symbol 开关灰度：仅 161129/688111/300058 启用（monitor_config `vol_confirm`: true）。
- RSI/KDJ 位置过滤（F1）与裸振荡器触发：0805 消融/验证全部负收益，已否决（证据留档
  output/v10_grid_2026-08-05.json / rb_oscillator_2026-08-05.json）。

规则（信号 bar 即时判定，尾窗 MA20，无前视）：
- B（买入/正T入场）：要求 vol_ratio = 当根量 / MA20量 ≤ 1.2（缩量或平量回调，抛压衰竭）
- S（卖出/反T入场）：要求 vol_ratio ≥ 1.0（至少平量，放量滞涨更优）
- X 出场不受影响（风控通道常开）
"""
import numpy as np

VOL_B_MAX = 1.2
VOL_S_MIN = 1.0


def _trailing_ma(x, w):
    out = np.zeros(len(x))
    s = 0.0
    for i in range(len(x)):
        s += x[i]
        if i >= w:
            s -= x[i - w]
        out[i] = s / min(i + 1, w)
    return out


def vol_ratio_at(df, trade_time_str):
    """返回信号 bar 的量比（当根量/前20根尾窗均量）；找不到 bar 返回 None（不过滤）。"""
    if df is None or 'volume' not in df.columns:
        return None
    tt = df['trade_time'].astype(str).values
    key = str(trade_time_str)[:16]
    idx = None
    for i in range(len(tt) - 1, -1, -1):
        if tt[i][:16] <= key:
            idx = i
            break
    if idx is None:
        return None
    vol = df['volume'].values.astype(float)
    vma = _trailing_ma(vol, 20)
    if vma[idx] <= 0:
        return None
    return float(vol[idx] / vma[idx])


def filter_signals(sym, sigs, df, log=print):
    """量能确认闸门：B 需缩量(≤1.2×)、S 需平量以上(≥1.0×)。返回 (通过列表, 抑制数)。"""
    out, suppressed = [], 0
    for s in sigs:
        typ, bar_tt = s[0], (s[12] if len(s) > 12 else '')
        vr = vol_ratio_at(df, bar_tt)
        if vr is None:
            out.append(s)
            continue
        if typ == 'B' and vr > VOL_B_MAX:
            suppressed += 1
            log(f'  ⛔ 量能确认 {sym}: B 抑制（量比 {vr:.2f} > {VOL_B_MAX}，非缩量回调）')
            continue
        if typ == 'S' and vr < VOL_S_MIN:
            suppressed += 1
            log(f'  ⛔ 量能确认 {sym}: S 抑制（量比 {vr:.2f} < {VOL_S_MIN}，缩量不抛）')
            continue
        out.append(s)
    return out, suppressed
