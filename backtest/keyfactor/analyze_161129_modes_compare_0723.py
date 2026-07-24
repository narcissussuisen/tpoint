#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
161129 (原油LOF) 2026-07-23 — strict / floor / resonance 三模式信号叠加对比。

- 三种模式各跑一遍 detect_daily:
    strict : 生产历史默认 (vol_div 关)
    floor  : 生产当前默认 (vol_div 关)
    resonance : v9.3.0 (vol_div 强制开, min_resonance=2)
- 把三模式的 B/S 信号点叠加到同一张实际行情图上 (颜色=模式, 形状=买卖)
- 另附: 各模式信号数量对比柱状图 + 信号对比明细表 (按bar对齐, 看哪些信号三模式共识/分歧)
- resonance 额外保留 v9 真实出场纪律配对 (forward-T)
输出:
  output/161129_modes_compare_20260723/{dashboard.html, overlay.png, counts.png, signals_compare.csv, summary.json}
"""
import sys, os, json, base64
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))

from test_resonance_v930 import detect_daily, _segment_days
import miji_engine as ME
from exit_manager import simulate_day, make_config, aggregate_metrics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SYM = '161129.SZ'
DAY = '2026-07-23'
DATA_CSV = r"F:\keyfactor_data\1m\161129.SZ_1m.csv"
OUT = os.path.join('output', '161129_modes_compare_20260723')

MODES = [('strict', False), ('floor', False), ('resonance', True)]
COLORS = {'strict': '#4a90d9', 'floor': '#f39c12', 'resonance': '#2ecc71'}

CFG = make_config(use_stop=True, stop_atr_mult=1.5, stop_mode='atr',
                  use_time=True, time_stop_bars=90,
                  use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
                  s_signal_exit=True)


def main():
    df = pd.read_csv(DATA_CSV)
    df['trade_date'] = df['trade_date'].astype(str)
    df['trade_time'] = df['trade_time'].astype(str)
    c_all = df['close'].values.astype(float)

    groups = _segment_days(df)
    seg = next((g for g in groups if df['trade_date'].iloc[g[0]] == DAY), None)
    gs, ge = seg
    day_df = df.iloc[gs:ge].reset_index(drop=True)
    c = day_df['close'].values.astype(float)
    t = np.arange(len(c))

    # ---- 三模式信号检测 ----
    sig_by_mode = {}
    for mode, vol in MODES:
        sigs = detect_daily(df, macd_gate_mode=mode, vol_div_enabled=vol, min_resonance=ME.RESONANCE_THRESHOLD)
        sig_by_mode[mode] = [s for s in sigs if gs <= s['idx'] < ge]

    # 按 bar 对齐的合并映射
    union_idx = sorted({s['idx'] for mode, _ in MODES for s in sig_by_mode[mode]})
    compare_rows = []
    for idx in union_idx:
        local = idx - gs
        price = round(float(c_all[idx]), 2)
        row = {'time': df['trade_time'].iloc[idx], 'price': price}
        for mode, _ in MODES:
            typ = next((s['type'] for s in sig_by_mode[mode] if s['idx'] == idx), None)
            row[mode] = typ if typ else '—'
        # resonance 触发明细
        rdetail = next((s['detail'] for s in sig_by_mode['resonance'] if s['idx'] == idx), '')
        row['resonance_detail'] = rdetail
        compare_rows.append(row)
    cmp_df = pd.DataFrame(compare_rows)

    # 模式计数
    counts = {}
    for mode, _ in MODES:
        b = sum(1 for s in sig_by_mode[mode] if s['type'] == 'B')
        s = sum(1 for s in sig_by_mode[mode] if s['type'] == 'S')
        counts[mode] = {'B': b, 'S': s, 'total': b + s}

    # 共识/分歧统计 (在同时有信号的 bar 上, 类型是否一致)
    agree = disagree = 0
    for idx in union_idx:
        typs = [next((s['type'] for s in sig_by_mode[m] if s['idx'] == idx), None) for m, _ in MODES]
        typs = [x for x in typs if x]
        if len(typs) >= 2:
            if len(set(typs)) == 1:
                agree += 1
            else:
                disagree += 1

    # ---- resonance v9 配对 ----
    o = df['open'].values.astype(float)[gs:ge]
    h = df['high'].values.astype(float)[gs:ge]
    lo = df['low'].values.astype(float)[gs:ge]
    pc = float(c_all[gs - 1]) if gs > 0 else float(c[0])
    data = ME.compute_miji_indicators(o, h, lo, c, None, pc, has_vol=False)
    prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'n': len(c)}
    res_sigs = [{'type': s['type'], 'idx': s['idx'] - gs, 'price': s['price'], 'reason': s['detail']}
                for s in sig_by_mode['resonance']]
    trips = simulate_day(res_sigs, prices, CFG)
    metrics = aggregate_metrics(trips)
    trip_df = pd.DataFrame(trips)

    # ---- 图1: 价格 + 三模式信号叠加 ----
    os.makedirs(OUT, exist_ok=True)
    fig, ax = plt.subplots(figsize=(15, 5.5))
    ax.plot(t, c, color='#888', lw=0.8, label='close', zorder=1)
    for mode, _ in MODES:
        for typ in ('B', 'S'):
            xs, ys = [], []
            for s in sig_by_mode[mode]:
                if s['type'] == typ:
                    xs.append(s['idx'] - gs); ys.append(s['price'])
            if xs:
                ax.scatter(xs, ys, marker='^' if typ == 'B' else 'v',
                           color=COLORS[mode], s=55, linewidths=0.6, edgecolors='k',
                           zorder=3, label=f'{mode} {typ}', alpha=0.9)
    ax.set_title(f'{SYM} {DAY} — strict/floor/resonance signal overlay (color=mode, ^ = B, v = S)', fontsize=12)
    ax.set_xlabel('bar (1min)'); ax.set_ylabel('price')
    ax.legend(loc='upper left', ncol=3, fontsize=9)
    ax.grid(alpha=0.22)
    fig.tight_layout()
    overlay_png = os.path.join(OUT, 'overlay.png')
    fig.savefig(overlay_png, dpi=110); plt.close(fig)

    # ---- 图2: 各模式信号数量对比 ----
    fig2, ax2 = plt.subplots(figsize=(7, 4))
    x = np.arange(len(MODES)); w = 0.35
    bs = [counts[m]['B'] for m, _ in MODES]
    ss = [counts[m]['S'] for m, _ in MODES]
    ax2.bar(x - w/2, bs, w, label='B (buy)', color='#2ecc71')
    ax2.bar(x + w/2, ss, w, label='S (sell)', color='#e74c3c')
    ax2.set_xticks(x); ax2.set_xticklabels([m for m, _ in MODES])
    ax2.set_ylabel('signal count'); ax2.set_title(f'{SYM} {DAY} signal count by mode')
    for i, (b, s) in enumerate(zip(bs, ss)):
        ax2.text(i - w/2, b + 0.1, str(b), ha='center', fontsize=9)
        ax2.text(i + w/2, s + 0.1, str(s), ha='center', fontsize=9)
    ax2.legend(); ax2.grid(alpha=0.22, axis='y')
    fig2.tight_layout()
    counts_png = os.path.join(OUT, 'counts.png')
    fig2.savefig(counts_png, dpi=110); plt.close(fig2)

    with open(overlay_png, 'rb') as f:
        ov_b64 = base64.b64encode(f.read()).decode('ascii')
    with open(counts_png, 'rb') as f:
        ct_b64 = base64.b64encode(f.read()).decode('ascii')

    summary = {
        'symbol': SYM, 'day': DAY,
        'counts': counts,
        'agree_bars': agree, 'disagree_bars': disagree,
        'resonance_v9': metrics,
    }
    os.makedirs(OUT, exist_ok=True)
    cmp_df.to_csv(os.path.join(OUT, 'signals_compare.csv'), index=False, encoding='utf-8-sig')
    trip_df.to_csv(os.path.join(OUT, 'trips.csv'), index=False, encoding='utf-8-sig')
    with open(os.path.join(OUT, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    html = build_html(summary, cmp_df, trip_df, ov_b64, ct_b64)
    with open(os.path.join(OUT, 'dashboard.html'), 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"=== {SYM} {DAY} 三模式信号对比 ===")
    for m, _ in MODES:
        print(f"  {m:10s}: B={counts[m]['B']} S={counts[m]['S']} 共{counts[m]['total']}")
    print(f"  多模式同bar共识={agree} 分歧={disagree}")
    print(f"  resonance v9配对: 笔数={metrics['total']} 胜率={metrics['win_rate']}% 盈亏比={metrics['pl_ratio']} 总收益={metrics['total_ret']}%")
    print(f"  输出: {os.path.abspath(OUT)}")


def build_html(summary, cmp_df, trip_df, ov_b64, ct_b64):
    counts = summary['counts']
    kpi = ""
    for m, _ in [('strict', 0), ('floor', 0), ('resonance', 0)]:
        kpi += (f"<div class='kpi'><div class='v' style='color:{COLORS[m]}'>{counts[m]['total']}</div>"
                f"<div class='l'>{m} 总</div></div>")
    kpi += (f"<div class='kpi'><div class='v'>{counts['strict']['total']+counts['floor']['total']+counts['resonance']['total']}</div>"
            f"<div class='l'>三模式合计</div></div>")
    kpi += (f"<div class='kpi'><div class='v'>{summary['agree_bars']}</div><div class='l'>同bar共识</div></div>")
    kpi += (f"<div class='kpi'><div class='v' style='color:#e74c3c'>{summary['disagree_bars']}</div><div class='l'>同bar分歧</div></div>")

    cmp_rows = ""
    for _, r in cmp_df.iterrows():
        cells = ""
        for m in ('strict', 'floor', 'resonance'):
            v = r[m]
            cls = ''
            if v == 'B': cls = 'b'
            elif v == 'S': cls = 's'
            cells += f"<td class='{cls}'>{v}</td>"
        cmp_rows += (f"<tr><td>{r['time']}</td><td>{r['price']}</td>{cells}"
                     f"<td class='det'>{r['resonance_detail']}</td></tr>")

    m = summary['resonance_v9']
    trip_rows = ""
    for _, t in trip_df.iterrows():
        trip_rows += (f"<tr><td>{t['entry_idx']}</td><td>{t['entry_price']}</td><td>{t['exit_idx']}</td>"
                      f"<td>{t['exit_price']}</td><td>{t['exit_reason']}</td><td>{t['ret_pct']}</td>"
                      f"<td>{t['hold_bars']}</td><td class='det'>{t['entry_reason']}</td></tr>")

    return f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>161129 {summary['day']} 三模式信号对比</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;margin:0;background:#0f1115;color:#e6e6e6;padding:20px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8aa;font-size:13px;margin-bottom:18px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}}
.kpi{{background:#1a1e26;border:1px solid #2a2f3a;border-radius:10px;padding:12px 16px;min-width:104px}}
.kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:12px;color:#9aa}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:24px}}
th,td{{border:1px solid #2a2f3a;padding:5px 7px;text-align:center}}
th{{background:#1a1e26;color:#bcd}}
td.b{{color:#2ecc71;font-weight:700}} td.s{{color:#e74c3c;font-weight:700}}
td.det{{text-align:left;color:#bbb;font-size:11.5px}}
h2{{font-size:15px;border-left:3px solid #4a90d9;padding-left:8px;margin:24px 0 10px}}
img{{max-width:100%;border:1px solid #2a2f3a;border-radius:8px;margin-bottom:10px}}
.warn{{background:#2a1f1f;border:1px solid #5a3a3a;color:#f0b;padding:8px 12px;border-radius:8px;font-size:12.5px;margin-bottom:16px}}
.legend{{font-size:12px;color:#bbb;margin:4px 0 18px}}
.legend b{{color:#e6e6e6}}
</style></head><body>
<h1>161129 原油LOF · {summary['day']} · strict / floor / resonance 信号对比</h1>
<div class='sub'>数据=1分钟K · strict/floor 为生产门控(vol_div关) · resonance 为 v9.3.0试验(vol_div强制开)</div>
<div class='kpis'>{kpi}</div>
<div class='warn'>⚠️ 模型回放，非实盘信号。resonance 为试验分支，生产以 floor 为准。叠加图同一bar多模式命中即重合显示。</div>

<h2>① 价格走势 + 三模式信号叠加</h2>
<div class='legend'>颜色=<b>模式</b>（蓝=strict / 橙=floor / 绿=resonance）；形状=<b>买卖</b>（▲=B买 / ▼=S卖）。</div>
<img src='data:image/png;base64,{ov_b64}'>

<h2>② 各模式信号数量对比</h2>
<img src='data:image/png;base64,{ct_b64}'>

<h2>③ 信号对比明细（按bar对齐）</h2>
<table><thead><tr><th>时间</th><th>价格</th><th>strict</th><th>floor</th><th>resonance</th><th>resonance 触发条件</th></tr></thead>
<tbody>{cmp_rows}</tbody></table>

<h2>④ resonance 模式 v9 出场纪律配对（移动止损0.4/0.6 + 硬止损 + 时间止损 + S出场）</h2>
<table><thead><tr><th>入场idx</th><th>入场价</th><th>出场idx</th><th>出场价</th><th>出场原因</th><th>收益%</th><th>持有</th><th>入场原因</th></tr></thead>
<tbody>{trip_rows}</tbody></table>
<div class='sub'>共识=同bar多模式类型一致；分歧=同bar出现 B/S 冲突。B/S 仅在对应模式真实触发时显示，否则为「—」。</div>
</body></html>"""


if __name__ == '__main__':
    main()
