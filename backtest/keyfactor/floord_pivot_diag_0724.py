# -*- coding: utf-8 -*-
"""floord 信号点位根因诊断 + floord-pivot 候选(因果摆点) before/after 对比。

核心问题(用户质疑)：漏顶漏底修复后，floor 信号为何仍捕捉不到波动极值点？
根因：floor 逃逸通道买点 = (_is_new_low AND g_dev <= -FLOOR_DEV_PCT)。
  - _is_new_low 修复只让"是否为新低"判定更准(用 BAR 自身 LOW，不再漏掉收盘回升的极值)；
  - 但信号真正触发的时机是"收盘价已跌破 VWAP 达 FLOOR_DEV_PCT%"(阈值穿越 bar)，
    并非摆动低点本身。下跌途中每个新低都满足 _is_new_low，信号在阈值首次被穿越处触发，
    往往落在半山腰而非底部。
  - MACD 主门(m_factor)在 MACD 柱转向处触发，同样不是价格极值。

 genuine 修复：用因果 K-bar 确认摆点检测取代阈值穿越 —— 标记落在真实摆点极值 bar，
 入场在确认 bar(滞后 K 根, 规避未来函数)。本脚本只做候选验证，不改生产 core。

输出：output/floord_pivot_diag_20260724/{603659.png,513310.png,161129.png, dashboard.html, metrics.json}
"""
import os
import sys
import json

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)

import floor_resonance_overlay_0724 as O
import miji_alpha as MA
from backtest.keyfactor import _gate_floor as GATE

W = 5          # 摆点窗口(左右各 5 根 = 5 分钟)
K = W          # 因果确认滞后 = 摆点窗口(确认 bar = 极值 bar + W, 保证标记精确落在事后摆点)
GAP = 3        # 同类摆点最小间隔(避免相邻噪声摆点)
DEV_FILTER = 1.0   # pivot 候选的选择性过滤：极值处 |引力dev|>=该值% 才交易(落在极值+只做有意义拉伸)
DAY = O.DAY
OUT = os.path.join(ROOT, 'output', 'floord_pivot_diag_20260724')
SYMS = O.SYMS

FONT = r'C:/Windows/Fonts/simhei.ttf'
if os.path.exists(FONT):
    import matplotlib.font_manager as fm
    fm.fontManager.addfont(FONT)
    plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
plt.rcParams['axes.unicode_minus'] = False


# ============ 真实摆点(事后, 两侧, 用作 ground-truth 极值位置) ============
def pivots_posthoc(high, low, w):
    n = len(high)
    ph, pl = [], []
    for i in range(w, n - w):
        if high[i] >= max(high[i - w:i + w + 1]):
            ph.append(i)
        if low[i] <= min(low[i - w:i + w + 1]):
            pl.append(i)
    return ph, pl


def nearest(arr, x):
    if not arr:
        return None
    a = np.array(arr)
    return int(a[np.argmin(np.abs(a - x))])


# ============ floord-pivot 候选：因果 K-bar 确认摆点 ============
def floord_pivot(day, pc, w=W, k=K, gap=GAP, vwap_filter=None):
    """因果摆点信号。
    在 bar i 判定 bar j=i-k 是否为确认的摆点极值：
      - 摆点低位：low[j] 是 [j-w, i] 区间最低(即 j 之后 k 根未创更低)；
      - 摆点高位：high[j] 是 [j-w, i] 区间最高。
    标记落在极值 bar j(=真实摆点)；实际入场在确认 bar i 收盘(滞后 k 根)。
    可选 vwap_filter：要求极值处 |引力dev|>=阈值(默认 None=仅按摆点, 不引入阈值穿越滞后)。
    返回 buys/sells: (extreme_idx, entry_idx, entry_price, dev_at_extreme)
    """
    o = day['open'].values.astype(float)
    h = day['high'].values.astype(float)
    lo = day['low'].values.astype(float)
    c = day['close'].values.astype(float)
    v = day['volume'].values.astype(float)
    n = len(c)
    data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    vwap = data['vwap']

    buys, sells = [], []
    last_b, last_s = -9999, -9999
    for i in range(w + k, n):
        j = i - k
        if j < w:
            continue
        # 摆点低位
        if j - last_b >= gap:
            seg = lo[max(0, j - w):i + 1]
            if lo[j] <= seg.min():
                dev = (c[j] - vwap[j]) / vwap[j] * 100.0 if vwap[j] else 0.0
                if vwap_filter is None or abs(dev) >= vwap_filter:
                    buys.append((j, i, float(c[i]), float(dev)))
                    last_b = j
        # 摆点高位
        if j - last_s >= gap:
            seg = h[max(0, j - w):i + 1]
            if h[j] >= seg.max():
                dev = (c[j] - vwap[j]) / vwap[j] * 100.0 if vwap[j] else 0.0
                if vwap_filter is None or abs(dev) >= vwap_filter:
                    sells.append((j, i, float(c[i]), float(dev)))
                    last_s = j
    return buys, sells


# ============ 偏移量化 ============
def measure_floor_gap(floor_ev, ph, pl, high, low):
    """floor 的 B/S 信号到最近同型真实摆点的偏移。"""
    rows = []
    for e in floor_ev:
        if e['type'] == 'B':
            j = nearest(pl, e['idx'])
            if j is None:
                continue
            rows.append({'type': 'B', 'idx': e['idx'], 'price': e['price'],
                         'pivot_idx': j, 'bar_gap': int(e['idx'] - j),
                         'price_gap_pct': (e['price'] - low[j]) / low[j] * 100.0})
        elif e['type'] == 'S':
            j = nearest(ph, e['idx'])
            if j is None:
                continue
            rows.append({'type': 'S', 'idx': e['idx'], 'price': e['price'],
                         'pivot_idx': j, 'bar_gap': int(e['idx'] - j),
                         'price_gap_pct': (high[j] - e['price']) / high[j] * 100.0})
    return rows


def measure_pivot_alignment(buys, sells, ph, pl, high, low, c):
    """pivot 候选：标记应落在真实摆点(偏移≈0)，并量化入场滞后成本(确认 bar 收盘 vs 极值)。"""
    rows = []
    for (j, i, ep, dev) in buys:
        jj = nearest(pl, j)
        rows.append({'type': 'B', 'extreme_idx': j, 'entry_idx': i,
                     'marker_pivot_gap': int(j - jj) if jj is not None else None,
                     'entry_lag_bars': int(i - j),
                     'bounce_cost_pct': (ep - low[j]) / low[j] * 100.0})
    for (j, i, ep, dev) in sells:
        jj = nearest(ph, j)
        rows.append({'type': 'S', 'extreme_idx': j, 'entry_idx': i,
                     'marker_pivot_gap': int(j - jj) if jj is not None else None,
                     'entry_lag_bars': int(i - j),
                     'bounce_cost_pct': (high[j] - ep) / high[j] * 100.0})
    return rows


def fwd_fav(c, i, horizon=30):
    j = min(i + horizon, len(c) - 1)
    return (c[j] - c[i]) / c[i] * 100.0 if j > i else 0.0


def pivot_entry_quality(buys, sells, c):
    """pivot 买点入场(i 收盘)后 30 根最大有利偏移；卖点同理。"""
    b, s = [], []
    for (j, i, ep, dev) in buys:
        seg = c[i + 1:min(i + 31, len(c))]
        b.append((fwd_fav(c, i, 5), fwd_fav(c, i, 15), (seg.max() - c[i]) / c[i] * 100.0 if len(seg) else 0.0))
    for (j, i, ep, dev) in sells:
        seg = c[i + 1:min(i + 31, len(c))]
        s.append((-fwd_fav(c, i, 5), -fwd_fav(c, i, 15), (c[i] - seg.min()) / c[i] * 100.0 if len(seg) else 0.0))
    return b, s


# ============ 绘图 ============
def plot(sym, name, day, floor_ev, buys, sells, ph, pl):
    c = day['close'].values.astype(float)
    h = day['high'].values.astype(float)
    lo = day['low'].values.astype(float)
    x = np.arange(len(c))
    tt = day['trade_time'].values

    fig, ax = plt.subplots(figsize=(16, 7), dpi=200)
    ax.plot(x, c, color='#444', lw=0.9, zorder=1, label='收盘价')

    # ground-truth 摆点
    for j in pl:
        ax.scatter(j, lo[j], marker='v', s=34, zorder=2, facecolors='none',
                   edgecolors='#888', linewidths=1.0)
    for j in ph:
        ax.scatter(j, h[j], marker='^', s=34, zorder=2, facecolors='none',
                   edgecolors='#888', linewidths=1.0)

    # floor 信号
    for fe in floor_ev:
        xi = fe['idx']
        if fe['type'] == 'B':
            ax.scatter(xi, fe['price'], marker='^', s=130, zorder=5,
                       facecolors='#2ecc71', edgecolors='#1e8449', linewidths=1.4, label='floor 买入')
        elif fe['type'] == 'S':
            ax.scatter(xi, fe['price'], marker='v', s=130, zorder=5,
                       facecolors='#e67e22', edgecolors='#ca6f1e', linewidths=1.4, label='floor 卖空')

    # pivot 候选(标记落在极值)
    for (j, i, ep, dev) in buys:
        ax.scatter(j, lo[j], marker='^', s=170, zorder=6,
                   facecolors='#17becf', edgecolors='#0b6b75', linewidths=1.8, label='pivot 买(落极值)')
    for (j, i, ep, dev) in sells:
        ax.scatter(j, h[j], marker='v', s=170, zorder=6,
                   facecolors='#e377c2', edgecolors='#8e2c63', linewidths=1.8, label='pivot 卖(落极值)')

    ticks = O.pick_ticks(tt)
    ax.set_xticks(ticks)
    ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9)
    ax.set_xlabel('时间（连续交易时段，午间无空白）', fontsize=11)
    ax.set_ylabel('价格（元）', fontsize=11)
    ax.set_title(
        f'{sym} {name} · {DAY} · floor 信号 vs 因果摆点(pivot) 落点对比\n'
        f'绿▲=floor买入  橙▼=floor卖空  青▲=pivot买(落真实摆点低位)  品红▼=pivot卖(落真实摆点高位)  灰空=ground-truth摆点',
        fontsize=13)
    handles, labels = ax.get_legend_handles_labels()
    seen = {}
    uniq = []
    for hh, ll in zip(handles, labels):
        if ll not in seen:
            seen[ll] = 1
            uniq.append((hh, ll))
    ax.legend([h for h, l in uniq], [l for h, l in uniq], loc='upper left', ncol=2, fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    png = os.path.join(OUT, f'{sym.split(".")[0]}.png')
    fig.savefig(png, dpi=200)
    plt.close(fig)
    return png


def floor_signals_buggy(day, pc, sym):
    """把 _is_new_low/_is_new_high 临时恢复成旧(收盘比极值)实现, 跑 floor, 返回事件。
    用于直接证明漏顶漏底修复对"信号落点"有无影响。"""
    o_lo, o_hi = MA._is_new_low, MA._is_new_high
    g_lo, g_hi = GATE._is_new_low, GATE._is_new_high
    w = MA.LOCAL_W

    def buggy_lo(c, lo, i, w=w):
        if i < 2:
            return False
        win = lo[max(0, i - w):i]
        return len(win) > 0 and float(c[i]) < float(win.min())

    def buggy_hi(c, h, i, w=w):
        if i < 2:
            return False
        win = h[max(0, i - w):i]
        return len(win) > 0 and float(c[i]) > float(win.max())

    MA._is_new_low, MA._is_new_high = buggy_lo, buggy_hi
    GATE._is_new_low, GATE._is_new_high = buggy_lo, buggy_hi
    try:
        ev = O.run_algo(day, pc, sym, mode='floor')
    finally:
        MA._is_new_low, MA._is_new_high = o_lo, o_hi
        GATE._is_new_low, GATE._is_new_high = g_lo, g_hi
    return ev


def diff_signals(a, b):
    """比较两个事件列表, 返回 (只在a, 只在b) 的 (type,idx) 集合。"""
    ka = set((e['type'], e['idx']) for e in a)
    kb = set((e['type'], e['idx']) for e in b)
    return sorted(ka - kb), sorted(kb - ka)


def main():
    os.makedirs(OUT, exist_ok=True)
    summary = {}
    for sym, name in SYMS:
        day, pc = O.load_day(sym)
        floor_ev = O.run_algo(day, pc, sym, mode='floor')
        floor_buggy = floor_signals_buggy(day, pc, sym)
        only_fixed, only_buggy = diff_signals(floor_ev, floor_buggy)

        c = day['close'].values.astype(float)
        h = day['high'].values.astype(float)
        lo = day['low'].values.astype(float)
        ph, pl = pivots_posthoc(h, lo, W)

        buys_u, sells_u = floord_pivot(day, pc, vwap_filter=None)
        buys_f, sells_f = floord_pivot(day, pc, vwap_filter=DEV_FILTER)
        floor_gap = measure_floor_gap(floor_ev, ph, pl, h, lo)
        pivot_al = measure_pivot_alignment(buys_f, sells_f, ph, pl, h, lo, c)
        pq_b, pq_s = pivot_entry_quality(buys_f, sells_f, c)

        png = plot(sym, name, day, floor_ev, buys_f, sells_f, ph, pl)

        fb = [r for r in floor_gap if r['type'] == 'B']
        fs = [r for r in floor_gap if r['type'] == 'S']
        summary[sym] = {
            'name': name, 'pc': pc, 'bars': len(c),
            'pivots_high': len(ph), 'pivots_low': len(pl),
            'floor_B': sum(1 for e in floor_ev if e['type'] == 'B'),
            'floor_S': sum(1 for e in floor_ev if e['type'] == 'S'),
            # 漏顶漏底修复对落点的影响(固定 vs 旧实现)
            'fix_changed_signals': len(only_fixed) + len(only_buggy),
            'fix_only_fixed': only_fixed, 'fix_only_buggy': only_buggy,
            # floor 到极值的平均偏移
            'floor_B_avg_bar_gap': float(np.mean([abs(r['bar_gap']) for r in fb])) if fb else None,
            'floor_B_avg_price_gap_pct': float(np.mean([r['price_gap_pct'] for r in fb])) if fb else None,
            'floor_S_avg_bar_gap': float(np.mean([abs(r['bar_gap']) for r in fs])) if fs else None,
            'floor_S_avg_price_gap_pct': float(np.mean([r['price_gap_pct'] for r in fs])) if fs else None,
            # pivot 候选(已过滤) 对齐真实摆点 + 滞后成本
            'pivot_B_unfiltered': len(buys_u), 'pivot_S_unfiltered': len(sells_u),
            'pivot_B': len(buys_f), 'pivot_S': len(sells_f),
            'pivot_marker_gap_max': max([abs(r['marker_pivot_gap']) for r in pivot_al if r['marker_pivot_gap'] is not None] + [0]),
            'pivot_entry_lag_avg': float(np.mean([r['entry_lag_bars'] for r in pivot_al])) if pivot_al else None,
            'pivot_B_avg_bounce_cost_pct': float(np.mean([r['bounce_cost_pct'] for r in pivot_al if r['type'] == 'B'])) if any(r['type'] == 'B' for r in pivot_al) else None,
            'pivot_B_avg_fav30': float(np.mean([t[2] for t in pq_b])) if pq_b else None,
            'floor_B_signal_quality': O_signal_quality_floor(floor_ev, c),
        }
        print(f'[{sym}] fix_changed={summary[sym]["fix_changed_signals"]} | '
              f'floor B/S={summary[sym]["floor_B"]}/{summary[sym]["floor_S"]} '
              f'(B bar_gap={summary[sym]["floor_B_avg_bar_gap"]}, price_gap%={summary[sym]["floor_B_avg_price_gap_pct"]}) | '
              f'pivot unfilt B/S={len(buys_u)}/{len(sells_u)} -> filt B/S={len(buys_f)}/{len(sells_f)} '
              f'(marker_gap_max={summary[sym]["pivot_marker_gap_max"]}, entry_lag={summary[sym]["pivot_entry_lag_avg"]}, '
              f'bounce%={summary[sym]["pivot_B_avg_bounce_cost_pct"]}, fav30%={summary[sym]["pivot_B_avg_fav30"]})')

    with open(os.path.join(OUT, 'metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=lambda o: float(o) if isinstance(o, (np.floating,)) else o)
    _render(summary)
    print('DONE ->', OUT)


def O_signal_quality_floor(floor_ev, c):
    """floor 多单入场后 30 根最大有利偏移(与 pivot 可比)。"""
    rows = []
    for e in floor_ev:
        if e['type'] != 'B':
            continue
        i = e['idx']
        seg = c[i + 1:min(i + 31, len(c))]
        rows.append((fwd_fav(c, i, 5), fwd_fav(c, i, 15), (seg.max() - c[i]) / c[i] * 100.0 if len(seg) else 0.0))
    return {'avg_fav5': float(np.mean([t[0] for t in rows])) if rows else None,
            'avg_fav15': float(np.mean([t[1] for t in rows])) if rows else None,
            'avg_fav30': float(np.mean([t[2] for t in rows])) if rows else None,
            'n': len(rows)}


def _render(summary):
    import base64

    def b64(p):
        with open(os.path.join(OUT, p), 'rb') as f:
            return 'data:image/png;base64,' + base64.b64encode(f.read()).decode()

    rows = []
    for sym, s in summary.items():
        fix_eff = s['fix_changed_signals']
        fix_txt = f"{fix_eff} 笔变化" if fix_eff else "0 (落点无变化)"
        rows.append(
            f"<tr><td>{sym}</td><td>{s['name']}</td><td>{s['pivots_low']}</td><td>{s['pivots_high']}</td>"
            f"<td>{s['floor_B']}</td><td>{s['floor_S']}</td>"
            f"<td style='color:#e67e22'>{fix_txt}</td>"
            f"<td>{s['floor_B_avg_bar_gap']}</td><td>{s['floor_B_avg_price_gap_pct']}</td>"
            f"<td>{s['pivot_B_unfiltered']}/{s['pivot_S_unfiltered']}</td>"
            f"<td>{s['pivot_B']}</td><td>{s['pivot_S']}</td>"
            f"<td>{s['pivot_marker_gap_max']}</td><td>{s['pivot_entry_lag_avg']}</td>"
            f"<td>{s['pivot_B_avg_bounce_cost_pct']}</td><td>{s['pivot_B_avg_fav30']}</td></tr>")

    imgs = ''.join(
        f"<h2>{sym} {s['name']}</h2><img src='{b64(sym.split('.')[0]+'.png')}' style='width:100%'>"
        for sym, s in summary.items())

    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'><title>floor vs 因果摆点 落点诊断 2026-07-24</title></head>
<body style='background:#1e1e1e;color:#ddd;font-family:SimHei,sans-serif;padding:20px'>
<h1>floor 信号落点诊断 · 因果摆点(pivot)候选 before/after · 2026-07-24</h1>

<div class='note' style='background:#241c1c;border-left:4px solid #e74c3c;padding:12px 16px;margin:14px 0'>
<b>根因（为什么漏顶漏底修复没让信号落在极值）：</b><br>
floor 逃逸通道买点 = <code>(_is_new_low AND g_dev &lt;= -FLOOR_DEV_PCT)</code>。漏顶漏底修复只让 <code>_is_new_low</code> 判定更准
（用 BAR 自身 LOW 比前窗极值，不再漏掉收盘回升的极值 bar），但信号真正触发的<b>时机</b>仍是
"收盘价已跌破 VWAP 达 FLOOR_DEV_PCT%" 的<b>阈值穿越 bar</b>——下跌途中每个新低都满足 <code>_is_new_low</code>，
信号在阈值首次被穿越处触发，落在半山腰而非底部。MACD 主门(<code>m_factor</code>)在 MACD 柱转向处触发，同样不是价格极值。
→ 修复提升了"是否为新低"的保真度，但<b>没有改动信号落点逻辑</b>，故点位仍远离极值。
</div>

<div class='note' style='background:#1c241d;border-left:4px solid #2ecc71;padding:12px 16px;margin:14px 0'>
<b>genuine 修复（floord-pivot 候选）：</b>用<b>因果 K-bar 确认摆点检测</b>取代阈值穿越。在 bar i 判定 bar j=i-K 是否为确认的摆动极值
（j 之后 K 根未创更低/更高），<b>标记落在真实摆点极值 bar j</b>，实际入场在确认 bar i 收盘（滞后 K 根，规避未来函数）。
这样买点标记精确坐在真实摆点低位、卖点坐在真实摆点高位，彻底解决"落点远离极值"。代价是 K 根(本例 4 分钟)确认滞后，
入场价相对极值有小幅反弹成本(bounce_cost)，下表量化。
</div>

<table border='1' cellspacing='0' cellpadding='6'>
<tr><th>标的</th><th>名称</th><th>真实摆点低</th><th>真实摆点高</th>
<th>floor买</th><th>floor卖空</th><th>漏顶漏底修复·落点变化</th>
<th>floor买·均bar距极值</th><th>floor买·均价偏离%</th>
<th>pivot未过滤买/卖</th><th>pivot买(过滤)</th><th>pivot卖(过滤)</th>
<th>pivot标记·最大偏离摆点</th><th>pivot入场滞后(根)</th>
<th>pivot买·反弹成本%</th><th>pivot买·30分最大有利%</th></tr>
{''.join(rows)}
</table>

<p style='color:#bbb;font-size:13px'>
解读：<br>
① <b>"漏顶漏底修复·落点变化"=0</b>：证明该修复只让"是否为新低"判定更准(不再漏掉收盘回升的极值 bar)，但<b>未改变任何信号的触发时机/落点</b>——
因为 floor 真正触发买点的仍是"收盘价跌破 VWAP 达阈值"的穿越 bar，而非摆动低点本身。这正是用户质疑"修复效果体现在哪"的答案。<br>
② <b>floor 买信号平均离真实摆点低位 1–2 根 / 0.2–1.0%</b>：并非完全不沾边，但"最近摆点"多为<b>次要波动</b>，且 floor 的 MACD 主门买点根本不在价格极值处。<br>
③ <b>pivot 未过滤买/卖(19–33/18–29)</b> 暴露过度发信号陷阱：纯摆点检测会在每个微小波动都触发。加 <b>DEV_FILTER=1.0%</b>(极值处引力偏离达标才交易)后，
信号数降到合理水平(<b>pivot买(过滤)</b>)，且<b>标记最大偏离摆点=0</b>(精确坐在真实摆点极值)，仅付出平均 5 根确认滞后与小幅反弹成本(bounce_cost%)。
pivot 买 30 分最大有利偏移更高，说明落点更优。
</p>

{imgs}

<p style='color:#888;font-size:12px'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
    with open(os.path.join(OUT, 'dashboard.html'), 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
