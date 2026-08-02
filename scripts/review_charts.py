#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review_charts.py — 绘当日行情图并标注 tpoint 信号（复盘铁律：B▲红 / S▼绿 / X✕橙）
用法: python review_charts.py [YYYY-MM-DD]
- 信号标注来源: output/review_{date}.json 的 symbols[].rows（floor 引擎在真实 1m 上复现的 B/S/X）
- 蜡烛: 1m 聚合 5m；红=涨(close>=open) 绿=跌（中国习惯）
"""
import sys, os, json, datetime
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import daily_signal_review as R

ROOT = R.ROOT
OUT = os.path.join(ROOT, 'output')
os.makedirs(OUT, exist_ok=True)

TARGET = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
D8 = TARGET.replace('-', '')

for f in ['Microsoft YaHei', 'SimHei', 'PingFang SC', 'Arial Unicode MS']:
    try:
        plt.rcParams['font.sans-serif'] = [f]
        break
    except Exception:
        pass
plt.rcParams['axes.unicode_minus'] = False

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
ds = R.MootdxDataSource()
sigdoc = json.load(open(os.path.join(OUT, 'review_%s.json' % TARGET), encoding='utf-8'))

charts = []
for sym in wl:
    name = wl[sym]
    df = R.fetch_1m(ds, sym, TARGET)
    if df is None:
        print('[%s] 无1m数据' % sym, flush=True)
        continue
    d2 = df.copy()
    d2['tt'] = pd.to_datetime(d2['trade_time'])
    d2 = d2.set_index('tt')
    agg = d2.resample('5min').agg({'open': 'first', 'close': 'last',
                                   'high': 'max', 'low': 'min', 'volume': 'sum'}).dropna()
    xlab = [t.strftime('%H:%M') for t in agg.index]
    ai = agg.index

    rows = sigdoc['symbols'].get(sym, {}).get('rows', [])
    sig_pts = []
    for r in rows:
        tt = pd.to_datetime(r['time'])
        idx = int(ai.get_indexer([tt], method='nearest')[0]) if len(ai) else 0
        sig_pts.append({'xi': idx, 'price': float(r['price']), 'type': r['type']})

    fig, ax = plt.subplots(figsize=(13, 4.6))
    for i, (t, row) in enumerate(agg.iterrows()):
        col = '#ef5350' if row['close'] >= row['open'] else '#26a69a'
        ax.plot([i, i], [row['low'], row['high']], color=col, lw=0.7)
        ax.add_patch(Rectangle((i - 0.32, row['open']), 0.64,
                     (row['close'] - row['open']) or 1e-6, color=col, zorder=2))
    for sp in sig_pts:
        if sp['type'] == 'B':
            ax.scatter(sp['xi'], sp['price'], marker='^', s=160, color='#ef5350',
                       zorder=5, edgecolors='white', linewidths=0.9)
        elif sp['type'] == 'S':
            ax.scatter(sp['xi'], sp['price'], marker='v', s=160, color='#26a69a',
                       zorder=5, edgecolors='white', linewidths=0.9)
        else:
            ax.scatter(sp['xi'], sp['price'], marker='X', s=130, color='#ffa726',
                       zorder=5, linewidths=2.0)
    step = max(1, len(xlab) // 12)
    ax.set_xticks(range(0, len(xlab), step))
    ax.set_xticklabels([xlab[i] for i in range(0, len(xlab), step)], rotation=45, fontsize=8)
    ax.set_ylabel('价格', fontsize=9)
    nb = sum(1 for r in rows if r['type'] == 'B')
    ns = sum(1 for r in rows if r['type'] == 'S')
    nx = sum(1 for r in rows if r['type'] == 'X')
    ax.set_title('%s %s  %s  行情 + tpoint 信号标注  [B%d/S%d/X%d]'
                 % (sym, name, TARGET, nb, ns, nx), fontsize=11)
    leg = [Line2D([0], [0], marker='^', color='w', markerfacecolor='#ef5350', markersize=10, label='B 买入'),
           Line2D([0], [0], marker='v', color='w', markerfacecolor='#26a69a', markersize=10, label='S 卖出/反T空'),
           Line2D([0], [0], marker='X', color='w', markerfacecolor='#ffa726', markersize=10, label='X 出场')]
    ax.legend(handles=leg, loc='best', fontsize=8)
    ax.grid(alpha=0.22)
    fig.tight_layout()
    fn = 'chart_%s_%s.png' % (TARGET, sym.replace('.', '_'))
    fig.savefig(os.path.join(OUT, fn), dpi=110)
    plt.close(fig)
    charts.append((sym, name, fn, nb, ns, nx))
    print('[%s] chart %s (B%d/S%d/X%d)' % (sym, fn, nb, ns, nx), flush=True)

print('[done]', charts)
