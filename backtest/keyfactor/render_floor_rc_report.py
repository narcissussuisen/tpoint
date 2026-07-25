# -*- coding: utf-8 -*-
# ===================== SUPERSEDED =====================
# 本报告由含后视镜偏差的诊断脚本 (diagnose_floor_*.py / compare_3f_vs_D.py) 驱动, 结论已过时。
# 干净方法报告见 d_candidate_backtest.py + render 脚本。本文件仅作历史参照保留。
# ======================================================
"""渲染 floor 7/24 失效根因报告 (HTML + 叠加图). 纯数据驱动, 不调参."""
import os
import sys
import json
import csv
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)

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
    return df[df['trade_date'] == DAY].reset_index(drop=True)


def main():
    diag = json.load(open(os.path.join(OUT, 'diagnosis.json'), encoding='utf-8'))
    whatif = json.load(open(os.path.join(OUT, 'whatif.json'), encoding='utf-8'))

    charts = {}
    for sym, name in SYMS:
        day = load_day(sym)
        c = day['close'].values.astype(float)
        tt = day['trade_time'].values
        n = len(c)
        x = np.arange(n)

        rows = diag[sym]['rows']
        b_pts = [(r['time'], r['price']) for r in rows if r['type'] == 'B']
        s_pts = [(r['time'], r['price']) for r in rows if r['type'] == 'S']
        x_pts = [(r['time'], r['price']) for r in rows if r['type'] == 'X']

        def idx(t):
            return int(np.argmin(np.abs(np.arange(n) - _t2i(tt, t))))

        w = whatif[sym]
        a_buy_i = [idx(t) for t in w['A_buy_times']]
        a_sell_i = [idx(t) for t in w['A_sell_times']]
        b_buy_i = [idx(t) for t in w['B_buy_times']]
        b_sell_i = [idx(t) for t in w['B_sell_times']]

        fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
        ax.plot(x, c, color='#555', lw=0.9, zorder=1, label='收盘价')
        for t, p in b_pts:
            ax.scatter(idx(t), p, marker='^', s=130, zorder=7, facecolors='#2ecc71',
                       edgecolors='#145a32', linewidths=1.6, label='实际 floor 买入' if (t, p) == b_pts[0] else '')
        for t, p in s_pts:
            ax.scatter(idx(t), p, marker='v', s=130, zorder=7, facecolors='#e67e22',
                       edgecolors='#9c5400', linewidths=1.6, label='实际 floor 卖空' if (t, p) == s_pts[0] else '')
        for t, p in x_pts:
            ax.scatter(idx(t), p, marker='v', s=120, zorder=6, facecolors='#e74c3c',
                       edgecolors='#7b241c', linewidths=1.4, label='实际 floor 出场' if (t, p) == x_pts[0] else '')
        ax.scatter(a_buy_i, c[a_buy_i], marker='^', s=90, zorder=5, facecolors='none',
                   edgecolors='#2ecc71', linewidths=1.6, label=f"WhatIf-A 波动率floor买(k={w['A_K']}ATR,WL={w['A_WL']})")
        ax.scatter(a_sell_i, c[a_sell_i], marker='v', s=90, zorder=5, facecolors='none',
                   edgecolors='#e74c3c', linewidths=1.6, label='WhatIf-A 波动率floor卖')
        ax.scatter(b_buy_i, c[b_buy_i], marker='*', s=140, zorder=6, facecolors='#f1c40f',
                   edgecolors='#9a7d0a', linewidths=1.0, label='WhatIf-B 真实MACD背离')
        ax.scatter(b_sell_i, c[b_sell_i], marker='*', s=140, zorder=6, facecolors='#f1c40f',
                   edgecolors='#9a7d0a', linewidths=1.0, label='WhatIf-B 真实背离卖')

        wanted = ['09:31', '10:00', '10:30', '11:00', '11:30', '13:01', '13:30', '14:00', '14:30', '15:00']
        ticks = []
        for w_ in wanted:
            for k, t in enumerate(tt):
                if t >= w_:
                    ticks.append(k); break
        ax.set_xticks(ticks); ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9)
        ax.set_title(f'{sym} {name} · 7/24 · 实际 floor 信号 vs 修复候选点\n'
                     f'绿▲=实际买 橙▼=实际卖空 红▼=实际出场  绿空▲=波动率floor 黄★=真实背离', fontsize=12)
        handles, labels = ax.get_legend_handles_labels()
        seen = set(); uniq = []
        for hh, ll in zip(handles, labels):
            if ll and ll not in seen:
                seen.add(ll); uniq.append((hh, ll))
        ax.legend([h for h, l in uniq], [l for h, l in uniq], loc='upper left', ncol=2, fontsize=8.5, framealpha=0.9)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        p = os.path.join(OUT, f'rc_{sym.split(".")[0]}.png')
        fig.savefig(p, dpi=160); plt.close(fig)
        charts[sym] = os.path.basename(p)

    # ============ HTML ============
    def b64(name):
        with open(os.path.join(OUT, name), 'rb') as f:
            return 'data:image/png;base64,' + __import__('base64').b64encode(f.read()).decode()

    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>floor 算法 7/24 失效根因诊断</title></head>
<body style='background:#1e1e1e;color:#ddd;font-family:SimHei,sans-serif;padding:22px;line-height:1.6'>
<h1>floor 算法 7/24 失效根因诊断 — 不是止损问题，是信号逻辑问题</h1>
<p style='color:#bbb'>标的：161129 原油LOF / 513310 中韩半导体ETF · 交易日 2026-07-24 · 复刻口径 = 实盘 floor 引擎(compute_miji_indicators + check_*_trigger(floor) + 移动止损0.4/0.6)。</p>

<h2>一、结论（先说重点）</h2>
<p>7/24 floor 信号质量差，<b>根因不在止损幅度、也不在尾盘禁开</b>（那批 config A–D 是掩盖症状的表面功夫）。真正的结构性缺陷有三层 + 一个数据问题：</p>
<ul>
<li><b>RC1 · floor 叠加阈值波动率错配：</b> <code>FLOOR_DEV_PCT=1.5%</code> 是<b>静态百分比</b>，而标的 ATR≈0.38%（仅为价格的 0.38%）。1.5% ≈ 4 个 ATR，意味着"价格需偏离 VWAP 4 个 ATR 且同时创 15 根新低/新高"——在当日 7.8% 的总区间里看似可达，但盘中价格多在 &lt;1% 的窄带里震荡，该条件<b>全天几乎不触发</b>。结果：floor 的"抓精确拐点"价值在 7/24 <b>从未激活</b>——所有实际信号都走 MACD 基础路径，floor 退化为 strict。</li>
<li><b>RC2 · MACD 基础门控把"单 bar 绿/红柱收缩"误当背离：</b> 当前买点 = <code>local_low(15根新低) + green_shrinking(hist&lt;0 且本根比上根大)</code>。这不是背离（背离需"价格更低低 + DIF 更高低"），只是"价格跌到 15 根低位、MACD 绿柱比上一根少一点"。震荡标的里 <code>local_low</code> 天天发生、<code>green_shrinking</code> 频繁，于是 B/S 在噪声上疯狂触发。7/24 的 13:14 买、14:22 买、13:05 卖空全属此类。</li>
<li><b>RC3 · 无趋势过滤，纯均值回归接飞刀 / 逆趋势：</b> <code>b_trend_filter=False</code> 默认关闭，下跌趋势中照样发买入。14:22 在 161129 崩向 2.126 日内最低时买入（−2.75%）、14:55 在 513310 尾盘下跌段买入（−0.6%）都是接飞刀。此外 <code>s_signal_exit</code> 让空→被反向信号平→立即反手多，加剧 whip（10:44 空→13:13 平→13:14 反手多）。</li>
<li><b>RC4 · 数据源缺陷：</b> 513310 的 <code>volume</code> 列在 mootdx 导出里是 <code>5.8e-39</code>（损坏）。虽 VOL_DIV 已关，但 VWAP 退化为等权均价，引力判定口径被稀释。属需修复的旁路问题。</li>
</ul>

<h2>二、实测证据</h2>
<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>
<tr><th>标的</th><th>昨收</th><th>日内高/低</th><th>总区间%</th><th>ATR均值%</th><th>floor叠加触发(买/卖)</th><th>MACD基础触发(买/卖)</th><th>实际发信号(买/卖/出场)</th></tr>
"""
    for sym, name in SYMS:
        r = diag[sym]['res']
        html += (f"<tr><td>{sym} {name}</td><td>{r['pc']}</td><td>{r['day_high']}/{r['day_low']}</td>"
                 f"<td>{r['range_pct']:.2f}</td><td>{r['atr_mean_pct']:.3f}</td>"
                 f"<td>{r['floor_buy_cnt']}/{r['floor_sell_cnt']}</td>"
                 f"<td>{r['base_buy_cnt']}/{r['base_sell_cnt']}</td>"
                 f"<td>{sum(1 for e in r['events'] if e[0]=='B')}/"
                 f"{sum(1 for e in r['events'] if e[0]=='S')}/"
                 f"{sum(1 for e in r['events'] if e[0]=='X')}</td></tr>")
    html += "</table>"
    html += "<p style='color:#9b59b6'>读数：floor 叠加路径全天仅触发 5/2（161129）、4/4（513310）个候选 bar，且<b>无一成为实际信号</b>——被 MACD 基础路径的持续占仓/冷却吞掉。这证明 RC1：floor 的拐点价值当天被压制为零。</p>"

    # 逐信号根因
    html += "<h2>三、逐信号根因 + 已实现盈亏</h2>"
    for sym, name in SYMS:
        html += f"<h3>{sym} {name}</h3><table border='1' cellspacing='0' cellpadding='5' style='border-collapse:collapse'>"
        html += "<tr><th>类型</th><th>时间</th><th>价格</th><th>触发路径</th><th>g_dev%</th><th>ATR%</th><th>MACD子条件</th><th>退出</th><th>盈亏%</th></tr>"
        pnl = {p['open_time']: p for p in diag[sym]['res']['pnl']}
        for r in diag[sym]['rows']:
            if r['path'] == 'exit':
                html += f"<tr><td>{r['type']}</td><td>{r['time']}</td><td>{r['price']}</td><td>出场</td><td>-</td><td>-</td><td>{r['reason']}</td><td>-</td><td>-</td></tr>"
                continue
            m = r.get('macd', {})
            macd_str = ('local_high' if m.get('local_high') else '') + ('/local_low' if m.get('local_low') else '') \
                + ('/红柱缩短' if m.get('red_shrinking') else '') + ('/绿柱收缩' if m.get('green_shrinking') else '') \
                + ('/金叉' if m.get('golden_cross') else '') + ('/死叉' if m.get('dead_cross') else '')
            p = pnl.get(r['time'], {})
            html += (f"<tr><td>{r['type']}</td><td>{r['time']}</td><td>{r['price']}</td><td>{r['path']}</td>"
                     f"<td>{r.get('g_dev')}</td><td>{r.get('atr_pct')}</td><td>{macd_str}</td>"
                     f"<td>{p.get('exit_kind','')} {p.get('exit_time','')}</td><td>{p.get('pnl_pct','')}</td></tr>")
        html += "</table>"

    # What-If
    html += "<h2>四、What-If 验证（证明修复方向可行，非空谈）</h2>"
    html += "<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>"
    html += "<tr><th>标的</th><th>当前MACD噪声触发(买/卖)</th><th>WhatIf-A 波动率floor(k=2.5ATR,WL=30) 买/卖</th><th>WhatIf-B 真实背离 买/卖</th></tr>"
    for sym, name in SYMS:
        w = whatif[sym]
        html += (f"<tr><td>{sym} {name}</td><td>{w['cur_macd_buy']}/{w['cur_macd_sell']}</td>"
                 f"<td>{w['A_floor_k_atr_buy']}/{w['A_floor_k_atr_sell']}</td>"
                 f"<td>{w['B_real_div_buy']}/{w['B_real_div_sell']}</td></tr>")
    html += "</table>"
    html += "<p style='color:#bbb'>WhatIf-A（波动率归一 floor）：161129 的买候选含 <b>13:12</b>——正是你盘中收到的、盈利 +1.09% 的那笔真实买点。说明只要把 1.5% 静态阈改成 k×ATR，floor 就能在拐点激活、兑现其设计价值。WhatIf-B（真实背离）：会把 13:14 买、14:22 买、13:05 卖空全部 suppression 掉，并把卖点聚集到 <b>10:46–11:12 的 2.293 真实顶部</b>。两者叠加，正好对症 RC1+RC2。</p>"

    for sym, name in SYMS:
        html += f"<h3>{sym} {name}</h3><img src='{b64(charts[sym])}' style='width:100%'>"

    # 修复方向
    html += """<h2>五、可落地修复方向（针对根因，不是调参）</h2>
<ul>
<li><b>RC1 修复：</b> <code>FLOOR_DEV_PCT</code> 改为波动率归一：<code>thr = K * ATR%</code>（如 K=2.5，即价格需偏离 VWAP ≥2.5 个 ATR），并把 <code>LOCAL_W</code> 从 15 拉长到 30+，使"新低/新高"代表真实摆动而非 15 分钟杂波。需多日 OOS 定 K。</li>
<li><b>RC2 修复：</b> <code>macd_divergence_signal</code> 的买/卖由"绿/红柱单 bar 收缩"升级为<b>真实背离</b>：买 = 价格创窗口更低低 <b>且</b> DIF 创更高低；卖 = 价格创更高高 <b>且</b> DIF 创更低高。至少要求 DIF 拐头/金叉而非仅柱状收缩。这是信号质量的本质改动。</li>
<li><b>RC3 修复：</b> 默认开启 <code>b_trend_filter</code>（下跌趋势禁买，防接飞刀）；评估接入既有的 5 分钟指数伴随门控 <code>detect_miji_signals_5m_index</code>（大盘空头时压制造信号）；重新审视 <code>s_signal_exit</code> 的反手逻辑，避免空→平→立即反手多的 whip。</li>
<li><b>RC4 修复：</b> 修复 mootdx 导出 513310 的 volume 损坏（或在该列全 0/异常时显式退化为等权 VWAP），保证引力判定口径一致。</li>
</ul>
<p style='color:#e67e22'><b>为什么 config A–D 是表面功夫：</b> 放宽止损（A/B）不改变"在错误位置进场"这一事实——14:22 在崩盘段买入，止损再宽也救不回趋势性亏损；尾盘禁用（C/D）只是把坏信号藏起来，既不解释为何坏、也不让 floor 在其它时段兑现价值。真正的杠杆在 RC1–RC3 的信号逻辑层。</p>

<h2>六、诚实声明</h2>
<p style='color:#888'>1) 本诊断为<b>单交易日、双标的个例</b>，用于定位算法结构性缺陷，不等于全样本结论；What-If 候选点为路径层示意，参数(K、窗口、趋势阈值)须经多日 OOS 验证。<br>
2) 生产 monitor 仍以 floor 为准；本次仅诊断 resonance/诊断分支，未改动生产代码。<br>
3) 回测结果为模型驱动，<b>绝不构成交易信号</b>。请勿据此直接买卖。</p>
<p style='color:#888'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""

    with open(os.path.join(OUT, 'rc_report.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print('REPORT ->', os.path.join(OUT, 'rc_report.html'))


def _t2i(tt, t):
    best = 0; best_d = 1e9
    for k, tv in enumerate(tt):
        hh, mm, ss = map(int, tv.split(':'))
        th, tm, ts = map(int, t.split(':'))
        d = abs((hh*3600+mm*60+ss) - (th*3600+tm*60+ts))
        if d < best_d:
            best_d = d; best = k
    return best


if __name__ == '__main__':
    main()
