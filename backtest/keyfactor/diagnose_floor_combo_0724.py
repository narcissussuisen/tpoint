# -*- coding: utf-8 -*-
# ===================== SUPERSEDED =====================
# 本脚本含「未来 N 根反转确认」作为触发条件 -> 后视镜偏差, 已被证伪。
# D 策略的权威定义已迁移至 d_strategy.py (纯因果, 无前视)。
# 本文件仅作历史参照保留, 不再参与任何结论。
# ======================================================
"""floor 失效根因 — 修正拐点 D 验证:
   真实有效候选 D = 极值用 BAR 自身 HIGH/LOW 取(修正原 floor 用收盘价比前窗最高价的结构性漏顶)
                  + 偏离阈值(k*ATR%, 波动率归一)
                  + 价格反转确认(极值后5根多数反向)
                  + 趋势 regime(快EMA>=慢EMA, 只在上行区间做回踩买/超买卖)
   把原 floor 分量(A) 与 真实背离分量(B) 标淡作对照, D 标粗。对 D 点做向前15根 P&L 校验。
   结论: 严格 A∩B∩trend = 0 (真实背离过稀 + 极值判定漏顶); D 能命中 13:12 买与 11:12 顶卖, 剔除崩盘噪音。
"""
import os
import sys
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
import miji_alpha as MA
MA.MACD_GATE_MODE = 'floor'

DATA_DIR = r'F:/keyfactor_data/1m'
DAY = '2026-07-24'
OUT = os.path.join(ROOT, 'output', 'floor_diagnosis_20260724')
SYMS = [('161129.SZ', '原油LOF'), ('513310.SH', '中韩半导体ETF')]
FONT = r'C:/Windows/Fonts/simhei.ttf'
if os.path.exists(FONT):
    fm.fontManager.addfont(FONT)
    plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
plt.rcParams['axes.unicode_minus'] = False

K = 2.5          # 波动率 floor: 偏离 VWAP 阈值 = k*ATR%
WL = 30          # 局部高低点回看窗口(1m)
TREND_W = 10     # 趋势上下文回看窗口(1m)
TREND_BAND = 0.8 # 买: 近 TREND_W 根收益 >= -BAND(非自由落体); 卖: <= +BAND


def load_day(sym):
    f = os.path.join(DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(f, encoding='utf-8-sig')
    df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
    df['trade_date'] = df['trade_date'].astype(str)
    day = df[df['trade_date'] == DAY].reset_index(drop=True)
    prev = df[df['trade_date'] < DAY]['trade_date'].max()
    pc_row = df[df['trade_date'] == prev]
    pc = float(pc_row['close'].iloc[-1]) if len(pc_row) else float(day['close'].iloc[0])
    return day, pc


def prev_pivot(series, i, w, kind):
    start = max(0, i - w)
    best = None
    for j in range(start, i):
        if kind == 'low':
            if j > start and series[j] < series[start:j].min():
                best = j
        else:
            if j > start and series[j] > series[start:j].max():
                best = j
    return best


def real_divergence(c, dif, i, w):
    """真实背离: 看多=窗口内最低价且 DIF 更高低; 看空=窗口内最高价且 DIF 更低高。"""
    if i < w:
        return 0
    start = max(0, i - w)
    if c[i] < c[start:i].min():
        j = prev_pivot(c, i, w, 'low')
        if j is not None and c[i] < c[j] and dif[i] > dif[j]:
            return 1
    if c[i] > c[start:i].max():
        j = prev_pivot(c, i, w, 'high')
        if j is not None and c[i] > c[j] and dif[i] < dif[j]:
            return -1
    return 0


def is_swing_high(h, i, w):
    """修正: 用 BAR 自身 HIGH 判窗口新高(顶部反转 bar 收盘价回落也能捕捉)。"""
    if i < 2:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


def is_swing_low(lo, i, w):
    if i < 2:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def dif_turn_up(dif, i):
    """DIF 刚在 i-1 见底并转上: dif[i-1]<=dif[i-2] 且 dif[i]>dif[i-1]。"""
    if i < 3:
        return False
    return dif[i - 1] <= dif[i - 2] and dif[i] > dif[i - 1]


def dif_turn_down(dif, i):
    if i < 3:
        return False
    return dif[i - 1] >= dif[i - 2] and dif[i] < dif[i - 1]


def reversal_confirmed(c, i, side, nbar=5):
    """极值 bar 后 nbar 根内, 多数收在有利方向(买: 多数更高; 卖: 多数更低)。"""
    if i + 1 >= len(c):
        return False
    end = min(i + nbar, len(c) - 1)
    fav = 0; tot = 0
    for j in range(i + 1, end + 1):
        tot += 1
        if side == 'B' and c[j] > c[i]:
            fav += 1
        if side == 'S' and c[j] < c[i]:
            fav += 1
    return tot > 0 and fav >= (tot + 1) // 2  # 多数


def combo_signals(c, h, lo, vwap, atr, dif, n):
    """返回: A分量(原floor极值), B分量(真实背离), D分量(修正拐点)。
    D = 极值(HIGH/LOW取) + 偏离阈值(k*ATR) + 价格反转确认(后5根多数反向) + 趋势regime(快>=慢EMA)。
    """
    a_buy, a_sell, b_buy, b_sell = [], [], [], []
    combo_b, combo_s = [], []
    ema_fast = pd.Series(c).ewm(span=20, adjust=False).mean().values
    ema_slow = pd.Series(c).ewm(span=60, adjust=False).mean().values
    for i in range(2, n):
        if atr[i] <= 0 or i < WL:
            continue
        atr_pct = atr[i] / vwap[i] * 100.0
        g_dev = (c[i] - vwap[i]) / vwap[i] * 100.0
        thr = K * atr_pct
        # WhatIf-A 原 floor 极值(收盘价比前窗极值, 有结构性漏顶缺陷)
        is_a_b = MA._is_new_low(c, lo, i, w=WL) and g_dev <= -thr
        is_a_s = MA._is_new_high(c, h, i, w=WL) and g_dev >= thr
        if is_a_b:
            a_buy.append(i)
        if is_a_s:
            a_sell.append(i)
        # WhatIf-B 真实背离
        dv = real_divergence(c, dif, i, w=WL)
        if dv == 1:
            b_buy.append(i)
        elif dv == -1:
            b_sell.append(i)
        # 修正拐点 D
        uptrend = ema_fast[i] >= ema_slow[i]  # 快慢EMA区间方向(上行regime)
        is_d_b = (is_swing_low(lo, i, WL) and g_dev <= -thr
                  and reversal_confirmed(c, i, 'B') and uptrend)
        is_d_s = (is_swing_high(h, i, WL) and g_dev >= thr
                  and reversal_confirmed(c, i, 'S') and uptrend)
        if is_d_b:
            combo_b.append(i)
        if is_d_s:
            combo_s.append(i)
    return a_buy, a_sell, b_buy, b_sell, combo_b, combo_s


def forward_pnl(c, idx, side, hold=15):
    """交集点向前 hold 根做多/空, 返回 pct(相对入场价)。"""
    out = []
    for i in idx:
        if i + hold >= len(c):
            continue
        entry = c[i]
        exitp = c[i + hold]
        pct = (exitp / entry - 1.0) * 100.0 if side == 'B' else (entry / exitp - 1.0) * 100.0
        out.append((i, entry, exitp, pct))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    figs = []
    for sym, name in SYMS:
        day, pc = load_day(sym)
        o = day['open'].values.astype(float); h = day['high'].values.astype(float)
        lo = day['low'].values.astype(float); c = day['close'].values.astype(float)
        v = day['volume'].values.astype(float); tt = day['trade_time'].values
        n = len(c)
        data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
        vwap = data['vwap']; atr = data['atr']; dif = data['dif']
        a_buy, a_sell, b_buy, b_sell, combo_b, combo_s = combo_signals(c, h, lo, vwap, atr, dif, n)

        pb = forward_pnl(c, combo_b, 'B'); ps = forward_pnl(c, combo_s, 'S')
        avg_b = np.mean([x[3] for x in pb]) if pb else float('nan')
        avg_s = np.mean([x[3] for x in ps]) if ps else float('nan')

        res[sym] = {
            'name': name, 'A_buy': len(a_buy), 'A_sell': len(a_sell),
            'B_buy': len(b_buy), 'B_sell': len(b_sell),
            'COMBO_buy': len(combo_b), 'COMBO_sell': len(combo_s),
            'combo_b_times': [tt[i] for i in combo_b], 'combo_s_times': [tt[i] for i in combo_s],
            'combo_b_pnl': [[tt[i], round(c[i], 4), round(pct, 2)] for i, _, _, pct in pb],
            'combo_s_pnl': [[tt[i], round(c[i], 4), round(pct, 2)] for i, _, _, pct in ps],
            'avg_b_pnl': round(avg_b, 3) if pb else None,
            'avg_s_pnl': round(avg_s, 3) if ps else None,
        }
        print(f"\n===== {sym} {name} =====")
        print(f"A(原floor极值, 有漏顶缺陷) 买{len(a_buy)} 卖{len(a_sell)} | B(真实背离) 买{len(b_buy)} 卖{len(b_sell)}")
        print(f"D(修正拐点: HIGH/LOW极值+DIF转向+趋势) 买{len(combo_b)} @ {[tt[i] for i in combo_b]} | 卖{len(combo_s)} @ {[tt[i] for i in combo_s]}")
        if pb:
            print(f"  D买点向前{15}根P&L均值: {avg_b:+.2f}%  -> {[(tt[i], round(p,2)) for i,_,_,p in pb]}")
        if ps:
            print(f"  D卖点向前{15}根P&L均值: {avg_s:+.2f}%  -> {[(tt[i], round(p,2)) for i,_,_,p in ps]}")

        # 画图
        fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
        x = np.arange(n)
        ax.plot(x, c, color='#555', lw=0.9, zorder=1, label='收盘价')
        ax.plot(x, vwap, color='#3498db', lw=0.7, ls='--', alpha=0.6, zorder=1, label='VWAP')
        # 分量(淡)
        ax.scatter(a_buy, c[a_buy], marker='^', s=60, zorder=3, facecolors='none',
                   edgecolors='#2ecc71', linewidths=0.8, alpha=0.35, label=f'A原floor买({len(a_buy)})')
        ax.scatter(a_sell, c[a_sell], marker='v', s=60, zorder=3, facecolors='none',
                   edgecolors='#e74c3c', linewidths=0.8, alpha=0.35, label=f'A原floor卖({len(a_sell)})')
        ax.scatter(b_buy, c[b_buy], marker='*', s=80, zorder=4, facecolors='none',
                   edgecolors='#f1c40f', linewidths=1.0, alpha=0.6, label=f'B真实背离买({len(b_buy)})')
        ax.scatter(b_sell, c[b_sell], marker='*', s=80, zorder=4, facecolors='none',
                   edgecolors='#f1c40f', linewidths=1.0, alpha=0.6, label=f'B真实背离卖({len(b_sell)})')
        # 修正拐点 D(粗实心)
        ax.scatter(combo_b, c[combo_b], marker='^', s=220, zorder=7, facecolors='#1e8449',
                   edgecolors='white', linewidths=1.5, label=f'D修正拐点买({len(combo_b)})')
        ax.scatter(combo_s, c[combo_s], marker='v', s=220, zorder=7, facecolors='#c0392b',
                   edgecolors='white', linewidths=1.5, label=f'D修正拐点卖({len(combo_s)})')
        wanted = ['09:31', '10:00', '10:30', '11:00', '11:30', '13:01', '13:30', '14:00', '14:30', '15:00']
        ticks = []
        for w_ in wanted:
            for k, t in enumerate(tt):
                if t >= w_:
                    ticks.append(k); break
        ax.set_xticks(ticks); ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9)
        ax.set_title(f'{sym} {name} · 7/24 · 修正拐点 D(HIGH/LOW极值+DIF转向+趋势) vs 分量\n'
                     f'粗实心绿▲/红▼=修正后有效候选  淡=被剔除分量', fontsize=13)
        ax.legend(loc='upper left', ncol=3, fontsize=8, framealpha=0.9)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        png = os.path.join(OUT, f'combo_{sym.split(".")[0]}.png')
        fig.savefig(png, dpi=160); plt.close(fig); figs.append(png)

    with open(os.path.join(OUT, 'combo.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print('\nDONE ->', OUT)
    return res, figs


if __name__ == '__main__':
    main()
