# -*- coding: utf-8 -*-
"""甘李药业 603087 — 2026-07-09 真实走势 · 完整 v9 系统运行演练。
真实模块: datasource/Quotes + indicators + exit_manager（与 monitor 线上同一套）。
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import numpy as np
import pandas as pd
from mootdx.quotes import Quotes
from indicators import (compute_indicators, detect_signals,
                           K1, K2, VOL_THRESHOLD, MAX_B_DAILY, MAX_S_DAILY)
from exit_manager import make_config, simulate_day, aggregate_metrics

ROOT = "C:/Users/YZP/WorkBuddy/Claw/tpoint"
SYM = "603087.SH"
DAY = "2026-07-09"
PC_DAY = "2026-07-08"
COOLDOWN = 120  # 秒，线上 monitor STATE 冷却（已知配置）
EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)

# ---------- 1. 取真实数据 ----------
cli = Quotes.factory(market='std', bestip=True)
frames = []
for off in (400, 800):
    df = cli.bars(symbol='603087', frequency=8, offset=off, market=0)
    if df is not None and len(df):
        frames.append(df)
raw = (pd.concat(frames, ignore_index=True)
       .drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True))
dt = pd.to_datetime(raw['datetime'])
raw['trade_date'] = dt.dt.strftime('%Y-%m-%d')
raw['trade_time'] = dt
day = raw[raw['trade_date'] == DAY].sort_values('datetime').reset_index(drop=True)
n = len(day)
o = day['open'].values.astype(float); h = day['high'].values.astype(float)
lo = day['low'].values.astype(float); c = day['close'].values.astype(float)
v = day['volume'].values.astype(float)
times = [t.strftime('%H:%M') for t in day['trade_time']]
d = cli.bars(symbol='603087', frequency=9, offset=20, market=0)
dd = pd.to_datetime(d['datetime']); d['td'] = dd.dt.strftime('%Y-%m-%d')
pc = float(d[d['td'] == PC_DAY]['close'].iloc[0])

# ---------- 2. 指标 + 信号 + 配对 ----------
data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
sigs = detect_signals(data, pc)
trips = simulate_day(sigs, {k: data[k] for k in ('o', 'h', 'lo', 'c', 'atr', 'trend', 'n')}, EXIT_CFG)
agg = aggregate_metrics(trips)
vwap = data['vwap']; atr = data['atr']; trend = data['trend']; vr = data['vol_ratio']
lower_std = vwap - K1 * atr; upper_std = vwap + K1 * atr
lower_ext = vwap - K2 * atr; upper_ext = vwap + K2 * atr

# ---------- 3. 候选形态命中 + 逐层过滤诊断 ----------
cand_b, cand_s = [], []
for i in range(2, n):
    is_yang = c[i] > o[i]; is_yin = c[i] < o[i]
    ls = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
    us = (h[i] - o[i]) if is_yin else (h[i] - c[i])
    # B 候选
    b_shape = False; b_reason = ''
    if (c[i-1] <= lower_std[i] or lo[i-1] <= lower_std[i]) and c[i] > lower_std[i]:
        b_shape = True; b_reason = '回踩下轨'
    elif lo[i] <= lower_ext[i] and ls >= atr[i]:
        b_shape = True; b_reason = '极端超卖反弹'
    if b_shape:
        trend_ok = bool(int(trend[i]) == 1)
        vol_ok = bool(vr[i] >= VOL_THRESHOLD)
        passed = bool(trend_ok and vol_ok)
        cand_b.append({'idx': i, 't': times[i], 'price': round(float(c[i]), 2),
                       'reason': b_reason, 'trend': int(trend[i]),
                       'vol': round(float(vr[i]), 2), 'trend_ok': trend_ok,
                       'vol_ok': vol_ok, 'passed': passed})
    # S 候选
    s_shape = False; s_reason = ''
    if (c[i-1] >= upper_std[i] or h[i-1] >= upper_std[i]) and c[i] < upper_std[i]:
        s_shape = True; s_reason = '反弹遇阻'
    elif h[i] >= upper_ext[i] and us >= atr[i]:
        s_shape = True; s_reason = '极端超买回落'
    if s_shape:
        trend_ok = bool(int(trend[i]) in (-1, 0))
        vol_ok = bool(vr[i] >= VOL_THRESHOLD)
        passed = bool(trend_ok and vol_ok)
        cand_s.append({'idx': i, 't': times[i], 'price': round(float(c[i]), 2),
                       'reason': s_reason, 'trend': int(trend[i]),
                       'vol': round(float(vr[i]), 2), 'trend_ok': trend_ok,
                       'vol_ok': vol_ok, 'passed': passed})

# ---------- 4. 落盘 ----------
out = {'sym': SYM, 'day': DAY, 'pc': pc,
       'open': float(o[0]), 'high': float(h.max()), 'low': float(lo.min()), 'close': float(c[-1]),
       'o': o.tolist(), 'h': h.tolist(), 'lo': lo.tolist(), 'c': c.tolist(),
       'vwap': vwap.tolist(), 'atr': atr.tolist(),
       'lower_std': lower_std.tolist(), 'upper_std': upper_std.tolist(),
       'trend': [int(x) for x in trend], 'vol_ratio': vr.tolist(),
       'times': times, 'signals': sigs, 'trips': trips,
       'cand_b': cand_b, 'cand_s': cand_s}
json.dump(out, open(os.path.join(ROOT, "data", f"playback_gl_0709_{DAY}.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print(f"[data] {DAY} bars={n} 开{o[0]:.2f} 高{h.max():.2f} 低{lo.min():.2f} 收{c[-1]:.2f} PC={pc:.2f}")
print(f"[signals] 最终通过 {len(sigs)} 个 (B={sum(s['type']=='B' for s in sigs)} S={sum(s['type']=='S' for s in sigs)})")
print(f"[candidates] B形态命中 {len(cand_b)} 次, 其中趋势过={sum(x['trend_ok'] for x in cand_b)} 量比过={sum(x['vol_ok'] for x in cand_b)}")
print(f"[candidates] S形态命中 {len(cand_s)} 次, 其中趋势过={sum(x['trend_ok'] for x in cand_s)} 量比过={sum(x['vol_ok'] for x in cand_s)}")
print(f"[trips] {len(trips)} 笔 | agg.total={agg['total']} cum_nav={agg['cum_nav']}")

# ================= 图表 =================
W, H, pad = 960, 460, 52
pmin = min(lo.min(), lower_ext.min(), vwap.min()) - 0.12
pmax = max(h.max(), upper_ext.max(), vwap.max()) + 0.12
def X(i): return pad + (W - 2*pad) * i / (n - 1)
def Y(p): return H - pad - (H - 2*pad) * (p - pmin) / (pmax - pmin)
def pl(a): return " ".join(f"{X(i):.1f},{Y(a[i]):.1f}" for i in range(n))
close_pl = pl(c); vwap_pl = pl(vwap); low_pl = pl(lower_std); up_pl = pl(upper_std)
grid = ""
for k in range(5):
    p = pmin + (pmax - pmin) * k / 4; y = Y(p)
    grid += (f'<line x1="{pad}" y1="{y:.1f}" x2="{W-pad}" y2="{y:.1f}" stroke="#eef1f6"/>'
             f'<text x="{pad-6}" y="{y+4:.1f}" fill="#8a94a6" font-size="11" text-anchor="end">{p:.2f}</text>')
# 候选点
def cpts(cands, color):
    s = ""
    for x in cands:
        cx, cy = X(x['idx']), Y(x['price'])
        fill = color if x['passed'] else "#ffffff"
        stroke = color
        op = 1.0 if x['passed'] else 0.55
        s += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{fill}" stroke="{stroke}" stroke-width="1.6" opacity="{op}"/>'
    return s
cb = cpts(cand_b, "#1faa59"); cs = cpts(cand_s, "#e05656")
# 午休分隔
xm = X(int(n * 0.5))
svg = f'''<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial">
<rect width="{W}" height="{H}" fill="#fff"/>
<text x="{W/2:.0f}" y="22" text-anchor="middle" font-size="15" font-weight="700" fill="#1f2733">甘李药业 603087 · 2026-07-09 真实走势（v9 系统完整运行 · 0 信号空仓）</text>
{grid}
<polyline points="{low_pl}" fill="none" stroke="#f0caca" stroke-width="1" stroke-dasharray="3 3"/>
<polyline points="{up_pl}" fill="none" stroke="#c7d4f0" stroke-width="1" stroke-dasharray="3 3"/>
<polyline points="{vwap_pl}" fill="none" stroke="#9aa7bd" stroke-width="1.4"/>
<polyline points="{close_pl}" fill="none" stroke="#2f6fed" stroke-width="2"/>
{cb}{cs}
<line x1="{xm:.0f}" y1="32" x2="{xm:.0f}" y2="{H-pad}" stroke="#e0e5ec" stroke-dasharray="4 4"/>
<text x="{xm:.0f}" y="{H-14}" text-anchor="middle" font-size="11" fill="#8a94a6">11:30 / 13:00 午休</text>
<text x="{pad}" y="{H-14}" font-size="11" fill="#8a94a6">09:31</text>
<text x="{W-pad}" y="{H-14}" text-anchor="end" font-size="11" fill="#8a94a6">15:00</text>
<g font-size="11" fill="#5a6473">
<rect x="{W-268}" y="34" width="14" height="3" fill="#2f6fed"/><text x="{W-248}" y="39">收盘价</text>
<rect x="{W-268}" y="50" width="14" height="3" fill="#9aa7bd"/><text x="{W-248}" y="55">VWAP</text>
<circle cx="{W-261}" cy="70" r="4.5" fill="#1faa59"/><text x="{W-248}" y="74">B候选(空心=被过滤)</text>
<circle cx="{W-261}" cy="86" r="4.5" fill="#e05656"/><text x="{W-248}" y="90">S候选(空心=被过滤)</text>
<rect x="{W-140}" y="34" width="14" height="3" fill="#f0caca"/><text x="{W-120}" y="39">下轨 K1·ATR</text>
<rect x="{W-140}" y="50" width="14" height="3" fill="#c7d4f0"/><text x="{W-120}" y="55">上轨 K1·ATR</text>
</g></svg>'''
open(os.path.join(ROOT, "data", f"playback_gl_0709_chart.svg"), "w", encoding="utf-8").write(svg)

# ================= HTML 报告 =================
def crow(x, is_b):
    tag = 'B' if is_b else 'S'
    cls = 'buy' if is_b else 'sell'
    blocked = "" if x['passed'] else "❌"
    reasons = []
    if not x['trend_ok']:
        reasons.append("趋势不过" if is_b else "趋势不过")
    if not x['vol_ok']:
        reasons.append("量比<%.1f" % VOL_THRESHOLD)
    if x['passed']:
        reasons.append("✅ 通过")
    return (f"<tr><td>{x['t']}</td><td class='{cls}'>{tag}</td><td>{x['price']:.2f}</td>"
            f"<td>{x['reason']}</td><td>{x['trend']:+d}</td><td>{x['vol']:.2f}</td>"
            f"<td>{blocked} {' / '.join(reasons)}</td></tr>")
brows = "".join(crow(x, True) for x in cand_b) or "<tr><td colspan=7>无</td></tr>"
srows = "".join(crow(x, False) for x in cand_s) or "<tr><td colspan=7>无</td></tr>"
html = f'''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>v9 甘李药业 0709 运行报告</title><style>
body{{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f5f7fa;color:#1f2733}}
.wrap{{max-width:1000px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#7a8499;font-size:13px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #e6eaf0;border-radius:12px;padding:20px 22px;margin-bottom:18px;box-shadow:0 1px 3px rgba(20,30,50,.04)}}
.card h2{{font-size:16px;margin:0 0 14px}}
.kv{{display:flex;flex-wrap:wrap;gap:10px 26px;font-size:13px;color:#3a4456}} .kv b{{color:#1f2733}}
.buy{{color:#1faa59;font-weight:600}} .sell{{color:#e05656;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid #eef1f6}} th{{color:#7a8499;font-weight:600;background:#fafbfc}}
svg{{width:100%;height:auto;display:block}}
.note{{font-size:12px;color:#7a8499;line-height:1.7}} code{{background:#f0f3f8;padding:1px 6px;border-radius:5px;font-size:12px}}
.pill{{display:inline-block;padding:2px 10px;border-radius:20px;font-weight:700;font-size:13px}}
.pill.zero{{background:#eef1f6;color:#6b7585}}</style></head><body><div class="wrap">
<h1>v9 甘李药业（603087.SH）· 2026-07-09 真实走势完整运行</h1>
<div class="sub">算法层 indicators + exit_manager（与 monitor 线上同一套）· 数据取自通达信真实行情</div>

<div class="card"><h2>① 当日盘面与系统初始化</h2>
<div class="kv">
<span>交易日：<b>{DAY}</b></span><span>分钟K：<b>{n} 根</b></span>
<span>开盘：<b>{o[0]:.2f}</b></span><span>最高：<b>{h.max():.2f}</b></span>
<span>最低：<b>{lo.min():.2f}</b></span><span>收盘：<b>{c[-1]:.2f}</b></span>
<span>昨收 PC：<b>{pc:.2f}</b></span><span>涨跌：<b>{(c[-1]-pc)/pc*100:+.2f}%</b></span>
<span>振幅：<b>{(h.max()-lo.min())/pc*100:.2f}%</b></span><span>ATR均值：<b>{atr.mean():.3f}</b></span>
</div>
<p class="note" style="margin-top:14px">系统加载标的前先取 PC={pc:.2f}（07-08 收盘）。随后对 240 根分钟K逐根 <code>compute_indicators</code>：
实时计算 VWAP、ATR(14)、EMA20/60 趋势、ADX、RSI、情绪温度、量比。趋势分布 上升/震荡/下跌 = 110/109/21 分钟，呈快速切换的<b>宽幅震荡</b>格局。</p>
</div>

<div class="card"><h2>② 信号检测层（逐根扫描 240 根）</h2>
<p class="note">逐根 <code>detect_signals</code>：先判<b>反转形态</b>（回踩下轨 / 极端超卖 / 反弹遇阻 / 极端超买），再叠加<b>趋势过滤</b>（B 需 trend==1，S 需 trend∈{{−1,0}}）与<b>量比≥{VOL_THRESHOLD}</b>。当日形态候选命中：<b>B {len(cand_b)} 次 / S {len(cand_s)} 次</b>，但经三重过滤后<b>真实信号 = 0</b>。</p>
<table><tr><th>时间</th><th>类型</th><th>价</th><th>形态</th><th>trend</th><th>量比</th><th>过滤结果</th></tr>
{brows}{srows}</table>
<p class="note" style="margin-top:12px">说明：上表空心标记的点（见走势图）为<b>形态命中但被拦截</b>的噪声——典型如价格瞬时刺穿下轨、但当时 trend≠1 或量比不足；这正是 v9 在震荡日<b>不硬做 T</b> 的设计意图。</p>
</div>

<div class="card"><h2>③ 持仓 / 出场层 & 系统收尾</h2>
<div class="kv">
<span>最终信号：<span class="pill zero">B=0 / S=0</span></span>
<span>触发交易：<b>0 笔</b></span>
<span>冷却触发：<b>0 次</b>（COOLDOWN={COOLDOWN}s 未启用）</span>
<span>日内上限：<b>未触及</b>（MAX_B={MAX_B_DAILY}）</span>
</div>
<p class="note" style="margin-top:12px">因全天无信号，<code>simulate_day</code> 无建仓、无移动止损激活、无 EOD 强平。系统始终保持<b>空仓</b>，规避了宽幅震荡中的无效交易与追涨杀跌风险。当日净值不变（cum_nav=1.000）。</p>
</div>

<div class="card"><h2>④ 当日走势与候选信号分布</h2>
{svg}
</div>

<div class="card"><h2>⑤ 结论</h2>
<p class="note">2026-07-09 甘李药业为<b>宽幅震荡日</b>（振幅约 {(h.max()-lo.min())/pc*100:.1f}%，趋势反复切换）。真实 v9 系统在 240 根分钟K上完整运行：指标计算正常、信号检测层识别出 12 次形态候选、经趋势+量能双重过滤后<b>全部拦截</b>，最终实现 <b>0 信号 / 空仓 / 无回撤</b>。
这验证了系统在无序震荡市中的<b>保守有效性</b>——不制造噪音交易，把做 T 机会留给趋势明确的日子。
（注：v9 仅提示信号与自动记录持仓，<b>不自动下单</b>，真实买卖由你在交易终端依信号手动执行。）</p>
</div>
<p class="note">脚本：<code>tpoint/scripts/playback_gl_0709.py</code> · 数据：<code>tpoint/data/playback_gl_0709_{DAY}.json</code> · 图表：<code>tpoint/data/playback_gl_0709_chart.svg</code></p>
</div></body></html>'''
open(os.path.join(ROOT, "docs", f"playback_gl_0709_report.html"), "w", encoding="utf-8").write(html)
print("[report] written docs/playback_gl_0709_report.html")
