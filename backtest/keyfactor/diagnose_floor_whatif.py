# -*- coding: utf-8 -*-
# ===================== SUPERSEDED =====================
# 本脚本为 What-If 诊断工具, 其信号评估使用「向前固定 N 根持有」P&L -> 含未来信息, 后视镜。
# 真实前向回测见 d_strategy.forward_backtest。本文件仅作历史参照保留。
# ======================================================
"""floor 失效根因 — What-If 验证:
  A) 波动率归一化 floor 阈值 (dev <= k*ATR%, LOCAL_W 拉长) 应当落在哪些点
  B) 真实 MACD 背离 (价格更低低点 + DIF 更高低点) 而非单 bar 绿柱收缩
验证这两类修复是否能抑制 7/24 的无效信号, 并给出候选点分布。
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
    """返回窗口 [i-w, i) 内最近一个同型 pivot 的索引; 找不到返回 None。"""
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


def real_divergence(c, dif, i, w=MA.LOCAL_W*2):
    """真实背离: 当前创更低低点且 DIF 创更高低点(看多); 或更高高点且 DIF 更低高点(看空)。"""
    if i < w:
        return 0
    start = max(0, i - w)
    if c[i] == c[i]:  # noqa
        pass
    # 看多: 当前是窗口内最低价
    if c[i] < c[start:i].min():
        j = prev_pivot(c, i, w, 'low')
        if j is not None and c[i] < c[j] and dif[i] > dif[j]:
            return 1
    # 看空: 当前是窗口内最高价
    if c[i] > c[start:i].max():
        j = prev_pivot(c, i, w, 'high')
        if j is not None and c[i] > c[j] and dif[i] < dif[j]:
            return -1
    return 0


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    for sym, name in SYMS:
        day, pc = load_day(sym)
        o = day['open'].values.astype(float); h = day['high'].values.astype(float)
        lo = day['low'].values.astype(float); c = day['close'].values.astype(float)
        v = day['volume'].values.astype(float); tt = day['trade_time'].values
        n = len(c)
        data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
        vwap = data['vwap']; atr = data['atr']; dif = data['dif']; dea = data['dea']; hist = data['hist']
        atr_pct = atr / vwap * 100.0

        # WhatIf-A: 波动率归一化 floor (k*ATR, LOCAL_W=30)
        K = 2.5; WL = 30
        a_buy = []; a_sell = []
        for i in range(2, n):
            if atr[i] <= 0 or i < WL:
                continue
            g_dev = (c[i] - vwap[i]) / vwap[i] * 100
            thr = K * atr_pct[i]
            if MA._is_new_low(c, lo, i, w=WL) and g_dev <= -thr:
                a_buy.append(i)
            if MA._is_new_high(c, h, i, w=WL) and g_dev >= thr:
                a_sell.append(i)

        # WhatIf-B: 真实 MACD 背离
        b_buy = []; b_sell = []
        for i in range(2, n):
            if atr[i] <= 0:
                continue
            d = real_divergence(c, dif, i)
            if d == 1: b_buy.append(i)
            elif d == -1: b_sell.append(i)

        # 当前 green_shrinking 触发的 B 次数(噪声基准)
        cur_b = cur_s = 0
        for i in range(2, n):
            if atr[i] <= 0 or i < MA.LOCAL_W:
                continue
            m, _ = MA.macd_divergence_signal(h, lo, c, dif, dea, hist, i)
            if m == 1: cur_b += 1
            elif m == -1: cur_s += 1

        res[sym] = {
            'name': name,
            'A_floor_k_atr_buy': len(a_buy), 'A_floor_k_atr_sell': len(a_sell),
            'A_buy_times': [tt[i] for i in a_buy], 'A_sell_times': [tt[i] for i in a_sell],
            'B_real_div_buy': len(b_buy), 'B_real_div_sell': len(b_sell),
            'B_buy_times': [tt[i] for i in b_buy], 'B_sell_times': [tt[i] for i in b_sell],
            'cur_macd_buy': cur_b, 'cur_macd_sell': cur_s,
            'A_K': K, 'A_WL': WL,
        }
        print(f"\n===== {sym} {name} =====")
        print(f"当前 MACD绿/红柱收缩触发: 买{cur_b} / 卖{cur_s} (噪声基准)")
        print(f"WhatIf-A 波动率归一floor(k={K}*ATR, WL={WL}): 买{len(a_buy)} / 卖{len(a_sell)}")
        print(f"   A买候选: {[tt[i] for i in a_buy]}")
        print(f"   A卖候选: {[tt[i] for i in a_sell]}")
        print(f"WhatIf-B 真实背离: 买{len(b_buy)} / 卖{len(b_sell)}")
        print(f"   B买候选: {[tt[i] for i in b_buy]}")
        print(f"   B卖候选: {[tt[i] for i in b_sell]}")

        # 图: 价格 + 当前信号 + A候选 + B候选
        fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
        x = np.arange(n)
        ax.plot(x, c, color='#555', lw=0.9, zorder=1, label='收盘价')
        ax.scatter([a_buy], [c[a_buy]], marker='^', s=90, zorder=5, facecolors='none',
                   edgecolors='#2ecc71', linewidths=1.6, label=f'A波动率floor买(k={K}ATR)')
        ax.scatter([a_sell], [c[a_sell]], marker='v', s=90, zorder=5, facecolors='none',
                   edgecolors='#e74c3c', linewidths=1.6, label='A波动率floor卖')
        ax.scatter([b_buy], [c[b_buy]], marker='*', s=120, zorder=6, facecolors='#f1c40f',
                   edgecolors='#b7950b', linewidths=1.0, label='B真实背离买')
        ax.scatter([b_sell], [c[b_sell]], marker='*', s=120, zorder=6, facecolors='#f1c40f',
                   edgecolors='#b7950b', linewidths=1.0, label='B真实背离卖')
        wanted = ['09:31', '10:00', '10:30', '11:00', '11:30', '13:01', '13:30', '14:00', '14:30', '15:00']
        ticks = []
        for w_ in wanted:
            for k, t in enumerate(tt):
                if t >= w_:
                    ticks.append(k); break
        ax.set_xticks(ticks); ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9)
        ax.set_title(f'{sym} {name} · 7/24 · floor 修复候选点 vs 价格\n'
                     f'绿空▲=波动率归一floor买  红空▼=floor卖  黄★=真实MACD背离', fontsize=13)
        ax.legend(loc='upper left', ncol=2, fontsize=9, framealpha=0.9)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, f'whatif_{sym.split(".")[0]}.png'), dpi=160)
        plt.close(fig)

    with open(os.path.join(OUT, 'whatif.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print('\nDONE ->', OUT)


if __name__ == '__main__':
    main()
