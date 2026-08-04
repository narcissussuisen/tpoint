#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review_charts.py — 绘当日 1m 分时图并标注实盘推送信号（2026-08-04 实盘化重构）
用法: python review_charts.py [YYYY-MM-DD]
- 只画「当天有实盘推送信号」的标的（无推送不画，并清理当日残留旧图）
- 信号标注来源: data/push_audit.jsonl 当日 ok 记录（真实推送，非复算）
- 图: 1m 收盘价分时线；B▲红 / S▼绿 / X✕橙（中国习惯红涨绿跌）
"""
import sys, os, json, datetime, glob
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import daily_signal_review as R

ROOT = R.ROOT
OUT = os.path.join(ROOT, 'output')
os.makedirs(OUT, exist_ok=True)

TARGET = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')

for f in ['Microsoft YaHei', 'SimHei', 'PingFang SC', 'Arial Unicode MS']:
    try:
        plt.rcParams['font.sans-serif'] = [f]
        break
    except Exception:
        pass
plt.rcParams['axes.unicode_minus'] = False

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
ds = R.MootdxDataSource()

# 当日实盘推送（唯一信号源）
pushes = {}
with open(os.path.join(ROOT, 'data', 'push_audit.jsonl'), encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get('ts', '')).startswith(TARGET) and r.get('ok') and r.get('sym') in wl:
            pushes.setdefault(r['sym'], []).append(r)

# 清理当日无推送标的的残留旧图（防 HTML 嵌入过期图）
for fp in glob.glob(os.path.join(OUT, 'chart_%s_*.png' % TARGET)):
    sym_in_name = os.path.basename(fp)[len('chart_%s_' % TARGET):-4].replace('_', '.')
    if sym_in_name not in pushes:
        os.remove(fp)
        print('[clean] %s（当日无实盘推送）' % os.path.basename(fp), flush=True)

charts = []
for sym, plist in pushes.items():
    name = wl[sym]
    df = R.fetch_1m(ds, sym, TARGET)
    if df is None:
        print('[%s] 无1m数据' % sym, flush=True)
        continue
    d2 = df.copy()
    d2['tt'] = pd.to_datetime(d2['trade_time'])
    d2 = d2.set_index('tt')
    ai = d2.index
    xlab = [t.strftime('%H:%M') for t in ai]

    fig, ax = plt.subplots(figsize=(13, 4.4))
    ax.plot(range(len(ai)), d2['close'].values, color='#2d6cdf', lw=1.1, zorder=3)
    for r in sorted(plist, key=lambda x: x['ts']):
        tt = pd.to_datetime(r['ts'])
        idx = int(ai.get_indexer([tt], method='nearest')[0]) if len(ai) else 0
        price = float(r['price']) if r.get('price') else float(d2['close'].values[idx])
        typ = r['type']
        if typ == 'B':
            ax.scatter(idx, price, marker='^', s=170, color='#ef5350', zorder=5, edgecolors='white', linewidths=0.9)
        elif typ == 'S':
            ax.scatter(idx, price, marker='v', s=170, color='#26a69a', zorder=5, edgecolors='white', linewidths=0.9)
        else:
            ax.scatter(idx, price, marker='X', s=140, color='#ffa726', zorder=5, linewidths=2.0)
        ax.annotate('%s %s' % (typ, r['ts'][11:16]), (idx, price), textcoords='offset points',
                    xytext=(6, 8 if typ == 'B' else -14), fontsize=8, color='#555')
    step = max(1, len(xlab) // 12)
    ax.set_xticks(range(0, len(xlab), step))
    ax.set_xticklabels([xlab[i] for i in range(0, len(xlab), step)], rotation=45, fontsize=8)
    ax.set_ylabel('价格', fontsize=9)
    nb = sum(1 for r in plist if r['type'] == 'B')
    ns = sum(1 for r in plist if r['type'] == 'S')
    nx = sum(1 for r in plist if r['type'] == 'X')
    ax.set_title('%s %s  %s  1m 分时 + 实盘推送标注  [B%d/S%d/X%d]'
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
    print('[%s] chart %s (B%d/S%d/X%d 实盘推送)' % (sym, fn, nb, ns, nx), flush=True)

if not charts:
    print('[done] 当日无实盘推送标的，不出图')
else:
    print('[done]', charts)
