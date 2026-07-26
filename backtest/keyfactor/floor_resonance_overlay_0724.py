# -*- coding: utf-8 -*-
"""floor vs resonance 算法信号叠加对比图 (2026-07-24, 603659/513310/161129).

展示方法对齐 @image#1:161129.png（即 output/floor_live_reconcile_20260724/161129.png）:
  - 收盘价折线 + 算法买卖点叠加
  - 连续交易时段、午间无空白
  - 紫空★=实盘推送审计点(参考)

与旧图的不同:
  - 同一张图叠加 floor(v9.2.2) 与 resonance 两类信号
  - floor: 绿▲买 / 橙▼卖空 / 红▼出场
  - resonance: 青●买 / 黄■卖空 / 品红×出场

输出:
  output/floor_resonance_overlay_20260724/{603659.png,513310.png,161129.png,
                                             dashboard.html, summary.json,
                                             floor_signals.csv, resonance_signals.csv}
"""
import os
import sys
import json
import csv

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.lines import Line2D

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import miji_alpha as MA

DATA_DIR = r'F:/keyfactor_data/1m'
DAY = '2026-07-24'
OUT = os.path.join(ROOT, 'output', 'floor_resonance_overlay_20260724')
SYMS = [
    ('603659.SH', '璞泰来'),
    ('513310.SH', '中韩半导体ETF'),
    ('161129.SZ', '原油LOF'),
]

# ---- matplotlib 中文字体 ----
FONT = r'C:/Windows/Fonts/simhei.ttf'
if os.path.exists(FONT):
    import matplotlib.font_manager as fm
    fm.fontManager.addfont(FONT)
    plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
plt.rcParams['axes.unicode_minus'] = False


# ============ 数据加载 ============
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


# ============ 复刻 detect_for 的入场+出场状态机(参数化 mode) ============
def run_algo(day, pc, sym, mode='floor'):
    """mode: 'floor' | 'resonance'"""
    o = day['open'].values.astype(float)
    h = day['high'].values.astype(float)
    lo = day['low'].values.astype(float)
    c = day['close'].values.astype(float)
    v = day['volume'].values.astype(float)
    tt = day['trade_time'].values

    data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    vwap = data['vwap']; atr = data['atr']; n = data['n']

    COLDOWN_BARS = 3
    MAX_B = MA.MAX_B_DAILY
    MAX_S = MA.MAX_S_DAILY

    def strength_size(g_dev_pct, m_present):
        strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
        return 4 if strong else 2

    def limit_up(sym):
        code = sym.split('.')[0]
        if code.startswith(('300', '301', '688')):
            return 0.20
        if code.startswith(('8', '4', '92')):
            return 0.30
        return 0.10

    # 对齐 reconcile_floor_live_0724.py 的出场配置
    EXIT = {'use_stop': False, 'use_time': False, 'use_trailing': True,
            'trail_activate_pct': 0.4, 'trail_pct': 0.6, 's_signal_exit': True}

    events = []
    pos = None
    b_last = -9999
    s_last = -9999
    b_count = 0
    s_count = 0
    run_hi_max = -1e9

    for i in range(2, n):
        if atr[i] <= 0:
            continue
        run_hi_max = max(run_hi_max, h[i])
        near_limit_up = ((run_hi_max - pc) / pc >= limit_up(sym)) if pc > 0 else False

        # ---- 持仓中：出场管理 ----
        if pos is not None:
            if pos['side'] == 'long':
                if c[i] > pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            else:
                if c[i] < pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            exited = False

            # 反向信号自然平仓
            if not exited and EXIT['s_signal_exit']:
                if pos['side'] == 'long':
                    ts, rs = MA.check_s_trigger(data, i, macd_gate_mode=mode)
                    if ts:
                        events.append(('X', float(c[i]), i, f'{mode}:S', pos['entry_price'], pos['side']))
                        pos = None
                        exited = True
                else:
                    tb, rb = MA.check_b_trigger(data, i, macd_gate_mode=mode)
                    if tb:
                        events.append(('X', float(c[i]), i, f'{mode}:B', pos['entry_price'], pos['side']))
                        pos = None
                        exited = True
            # 移动止损
            if not exited and EXIT['use_trailing']:
                if pos['side'] == 'long':
                    fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 - EXIT['trail_pct'] / 100.0)
                        if c[i] <= trail_stop:
                            events.append(('X', float(c[i]), i, f'{mode}:TRAIL', pos['entry_price'], pos['side']))
                            pos = None
                            exited = True
                else:
                    fav_ret = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 + EXIT['trail_pct'] / 100.0)
                        if c[i] >= trail_stop:
                            events.append(('X', float(c[i]), i, f'{mode}:TRAIL', pos['entry_price'], pos['side']))
                            pos = None
                            exited = True
            continue

        # ---- 空仓：双向入场 ----
        tb, rb = MA.check_b_trigger(data, i, macd_gate_mode=mode)
        ts, rs = MA.check_s_trigger(data, i, macd_gate_mode=mode)
        if not (tb or ts):
            continue
        if tb:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rb or ''))
            if s_pct > 0 and (i - b_last) >= COLDOWN_BARS and b_count < MAX_B:
                b_last = i
                b_count += 1
                events.append(('B', float(c[i]), i, f'{mode}:{rb or ""}', None, 'long'))
                pos = {'side': 'long', 'entry_price': float(c[i]), 'entry_idx': i,
                       'max_fav': float(c[i])}
        if ts:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rs or ''))
            if s_pct > 0 and (i - s_last) >= COLDOWN_BARS and s_count < MAX_S and not near_limit_up:
                s_last = i
                s_count += 1
                events.append(('S', float(c[i]), i, f'{mode}:{rs or ""}', None, 'short'))
                pos = {'side': 'short', 'entry_price': float(c[i]), 'entry_idx': i,
                       'max_fav': float(c[i])}

    out = []
    for typ, price, i, reason, entry, side in events:
        out.append({'type': typ, 'time': tt[i], 'price': price,
                    'reason': reason, 'entry_price': entry, 'side': side,
                    'idx': int(i)})
    return out


# ============ 读取实盘推送审计 ============
def load_audit(sym):
    rows = []
    p = os.path.join(ROOT, 'data', 'push_audit.jsonl')
    if not os.path.exists(p):
        return rows
    for line in open(p, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('sym') != sym:
            continue
        if not d.get('ts', '').startswith(DAY):
            continue
        rows.append({'type': d['type'], 'time': d['ts'].split(' ')[1],
                     'price': float(d['price'])})
    return rows


# ============ 连续时间轴刻度 ============
def pick_ticks(times):
    wanted = ['09:31', '10:00', '10:30', '11:00', '11:30',
              '13:01', '13:30', '14:00', '14:30', '15:00']
    ticks = []
    for w in wanted:
        for k, t in enumerate(times):
            if t >= w:
                ticks.append(k)
                break
    return ticks


def _nearest_idx(tt, t):
    best = 0
    best_d = 1e9
    for k, tv in enumerate(tt):
        hh, mm, ss = map(int, tv.split(':'))
        th, tm, ts = map(int, t.split(':'))
        d = abs((hh * 3600 + mm * 60 + ss) - (th * 3600 + tm * 60 + ts))
        if d < best_d:
            best_d = d
            best = k
    return best


# ============ 叠加绘图 ============
def plot_overlay(sym, name, day, floor_ev, res_ev, audit):
    c = day['close'].values.astype(float)
    x = np.arange(len(c))
    tt = day['trade_time'].values

    fig, ax = plt.subplots(figsize=(16, 7), dpi=200)
    ax.plot(x, c, color='#444', lw=0.9, zorder=1, label='收盘价')

    # ---- floor 信号 ----
    for fe in floor_ev:
        xi = fe['idx']
        if fe['type'] == 'B':
            ax.scatter(xi, fe['price'], marker='^', s=130, zorder=6,
                       facecolors='#2ecc71', edgecolors='#1e8449', linewidths=1.4,
                       label='floor 买入(入场)')
        elif fe['type'] == 'S':
            ax.scatter(xi, fe['price'], marker='v', s=130, zorder=6,
                       facecolors='#e67e22', edgecolors='#ca6f1e', linewidths=1.4,
                       label='floor 卖出(开空)')
        else:  # X
            ax.scatter(xi, fe['price'], marker='v', s=140, zorder=7,
                       facecolors='#e74c3c', edgecolors='#922b21', linewidths=1.6,
                       label='floor 出场(TRAIL/S)')

    # ---- resonance 信号 ----
    for re in res_ev:
        xi = re['idx']
        if re['type'] == 'B':
            ax.scatter(xi, re['price'], marker='o', s=100, zorder=6,
                       facecolors='none', edgecolors='#17becf', linewidths=1.6,
                       label='resonance 买入(入场)')
        elif re['type'] == 'S':
            ax.scatter(xi, re['price'], marker='s', s=100, zorder=6,
                       facecolors='none', edgecolors='#bcbd22', linewidths=1.6,
                       label='resonance 卖出(开空)')
        else:  # X
            ax.scatter(xi, re['price'], marker='x', s=130, zorder=7,
                       facecolors='none', edgecolors='#e377c2', linewidths=2.0,
                       label='resonance 出场(TRAIL/S)')

    # ---- 实盘审计参考点(空心星) ----
    for a in audit:
        xi = _nearest_idx(tt, a['time'])
        col = '#9b59b6' if a['type'] == 'B' else '#8e44ad'
        ax.scatter(xi, a['price'], marker='*', s=170, zorder=8,
                   facecolors='none', edgecolors=col, linewidths=1.6,
                   label='实盘推送(审计)')

    ticks = pick_ticks(tt)
    ax.set_xticks(ticks)
    ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9, rotation=0)
    ax.set_xlabel('时间（连续交易时段，午间无空白）', fontsize=11)
    ax.set_ylabel('价格（元）', fontsize=11)
    ax.set_title(
        f'{sym} {name} · {DAY} · v9.2.2 floor 与 resonance 算法信号叠加对比\n'
        f'绿▲=floor买入  红▼=floor出场  青●=resonance买入  品红×=resonance出场  紫空★=实盘推送审计点',
        fontsize=13)

    # 去重图例
    handles, labels = ax.get_legend_handles_labels()
    seen = {}
    uniq = []
    for hh, ll in zip(handles, labels):
        if ll not in seen:
            seen[ll] = 1
            uniq.append((hh, ll))
    ax.legend([h for h, l in uniq], [l for h, l in uniq],
              loc='upper left', ncol=2, fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    png = os.path.join(OUT, f'{sym.split(".")[0]}.png')
    fig.savefig(png, dpi=200)
    plt.close(fig)
    return png


def main():
    os.makedirs(OUT, exist_ok=True)
    summary = {}
    all_floor = {}
    all_res = {}

    for sym, name in SYMS:
        day, pc = load_day(sym)
        floor_ev = run_algo(day, pc, sym, mode='floor')
        res_ev = run_algo(day, pc, sym, mode='resonance')
        audit = load_audit(sym)

        # CSV
        for mode, ev in [('floor', floor_ev), ('resonance', res_ev)]:
            fcsv = os.path.join(OUT, f'{mode}_signals_{sym.split(".")[0]}.csv')
            with open(fcsv, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['type', 'time', 'price', 'reason', 'entry_price', 'side'])
                for e in ev:
                    w.writerow([e['type'], e['time'], e['price'], e['reason'],
                                e['entry_price'], e['side']])

        all_floor[sym] = floor_ev
        all_res[sym] = res_ev

        png = plot_overlay(sym, name, day, floor_ev, res_ev, audit)

        nb_f = sum(1 for e in floor_ev if e['type'] == 'B')
        ns_f = sum(1 for e in floor_ev if e['type'] == 'S')
        nx_f = sum(1 for e in floor_ev if e['type'] == 'X')
        nb_r = sum(1 for e in res_ev if e['type'] == 'B')
        ns_r = sum(1 for e in res_ev if e['type'] == 'S')
        nx_r = sum(1 for e in res_ev if e['type'] == 'X')

        summary[sym] = {
            'name': name, 'pc': pc, 'bars': len(day),
            'floor_B': nb_f, 'floor_S': ns_f, 'floor_X': nx_f,
            'resonance_B': nb_r, 'resonance_S': ns_r, 'resonance_X': nx_r,
            'audit_total': len(audit),
            'png': os.path.basename(png),
        }
        print(f'[{sym}] pc={pc:.3f} bars={len(day)} | '
              f'floor B/S/X={nb_f}/{ns_f}/{nx_f} | '
              f'resonance B/S/X={nb_r}/{ns_r}/{nx_r} | '
              f'audit={len(audit)}')

    with open(os.path.join(OUT, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    _render_dashboard(summary, all_floor, all_res, OUT)
    print('DONE ->', OUT)


def _render_dashboard(summary, all_floor, all_res, OUT):
    import base64

    def b64(p):
        with open(os.path.join(OUT, p), 'rb') as f:
            return 'data:image/png;base64,' + base64.b64encode(f.read()).decode()

    rows = []
    for sym, s in summary.items():
        rows.append(
            f"<tr><td>{sym}</td><td>{s['name']}</td><td>{s['pc']:.3f}</td>"
            f"<td>{s['bars']}</td>"
            f"<td>{s['floor_B']}</td><td>{s['floor_S']}</td><td>{s['floor_X']}</td>"
            f"<td>{s['resonance_B']}</td><td>{s['resonance_S']}</td><td>{s['resonance_X']}</td>"
            f"<td>{s['audit_total']}</td></tr>")

    def sig_table(ev):
        return ("<table border='1' cellspacing='0' cellpadding='4'>"
                "<tr><th>类型</th><th>时间</th><th>价格</th><th>触发原因</th><th>入场价</th></tr>"
                + ''.join(
                    f"<tr><td>{e['type']}</td><td>{e['time']}</td><td>{e['price']:.3f}</td>"
                    f"<td>{e['reason']}</td><td>{e['entry_price']}</td></tr>"
                    for e in ev)
                + "</table>")

    imgs = ''.join(
        f"<h2>{sym} {s['name']}</h2><img src='{b64(s['png'])}' style='width:100%'>"
        f"<h3>floor 信号明细</h3>{sig_table(all_floor[sym])}"
        f"<h3>resonance 信号明细</h3>{sig_table(all_res[sym])}"
        for sym, s in summary.items())

    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>floor vs resonance 信号叠加 2026-07-24</title></head>
<body style='background:#1e1e1e;color:#ddd;font-family:SimHei,sans-serif;padding:20px'>
<h1>v9.2.2 floor 与 resonance 算法信号叠加对比 · 2026-07-24</h1>
<p>口径：core/miji_alpha.compute_miji_indicators + check_b_trigger/check_s_trigger，统一使用移动止损出场(0.4/0.6)。
<br>绿▲=floor买入，橙▼=floor卖空，红▼=floor出场；青●=resonance买入，黄■=resonance卖空，品红×=resonance出场；紫空★=实盘推送审计点。</p>
<table border='1' cellspacing='0' cellpadding='6'>
<tr><th>标的</th><th>名称</th><th>昨收pc</th><th>分钟数</th><th>floor买</th><th>floor卖空</th><th>floor出场</th><th>res买</th><th>res卖空</th><th>res出场</th><th>审计点</th></tr>
{''.join(rows)}
</table>
{imgs}
<p style='color:#888'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
    with open(os.path.join(OUT, 'dashboard.html'), 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
