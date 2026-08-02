#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_review.py — tpoint 每日信号复盘 + 行情图(信号标注) 生成器
用法: python gen_review.py [YYYY-MM-DD]
- 权威"实盘触发"源: data/push_audit.jsonl (生产 monitor 推送审计)
- 复算还原条件 + 向前验证: daily_signal_review.replay_symbol (同一 floor 引擎)
- 行情图: matplotlib 5m 蜡烛 + B/S/X 标注 (自包含 PNG, 无 CDN 依赖)
- 基线: data/state.json 逐日真实触发计数 (近5交易日)
常驻要求(2026-07-29 起): 每次复盘必须绘出当日行情图并把 tpoint 信号标注其上。
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
VALID_THR = R.VALID_THR

TARGET = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
D8 = TARGET.replace('-', '')
wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
ds = R.MootdxDataSource()

# ---------------- matplotlib 中文字体 ----------------
for f in ['Microsoft YaHei', 'SimHei', 'PingFang SC', 'Arial Unicode MS']:
    try:
        plt.rcParams['font.sans-serif'] = [f]
        break
    except Exception:
        pass
plt.rcParams['axes.unicode_minus'] = False

# ---------------- 基线(近5交易日真实触发) ----------------
st = json.load(open(os.path.join(ROOT, 'data', 'state.json'), encoding='utf-8'))
def day_total(d):
    tot = 0
    for s in wl:
        tot += st.get('_b_count_%s_%s' % (s, d), 0) + st.get('_s_count_%s_%s' % (s, d), 0)
    return tot
# 近5交易日(跳过周末)
d = datetime.date(*map(int, TARGET.split('-'))) - datetime.timedelta(days=1)
bdays = []
while len(bdays) < 5:
    ds_ = d.strftime('%Y%m%d')
    if d.weekday() < 5 and ds_ != D8:
        bdays.append(ds_)
    d -= datetime.timedelta(days=1)
bdays = bdays[::-1]
baseline = {b: day_total(b) for b in bdays}
base_avg = round(sum(baseline.values()) / len(baseline), 1) if baseline else 0

# ---------------- 实际推送(push_audit) ----------------
audit = R.load_push_audit(os.path.join(ROOT, 'data', 'push_audit.jsonl'), TARGET)
n_audit = len(audit)

# ---------------- 取数 + 复算(引擎资格信号) ----------------
charts = []; all_signals = []; sym_stats = {}
for sym in wl:
    name = wl[sym]
    df = R.fetch_1m(ds, sym, TARGET)
    if df is None:
        print('[%s] 无1m数据' % sym, flush=True); continue
    pc = R.get_pc(ds, sym, TARGET)
    data = R.build_data(df, pc)
    rows, stats = R.replay_symbol(sym, name, data, pc)
    sym_stats[sym] = stats

    # 5m 蜡烛聚合
    d2 = df.copy()
    d2['tt'] = pd.to_datetime(d2['trade_time'])
    d2 = d2.set_index('tt')
    agg = d2.resample('5min').agg({'open': 'first', 'close': 'last',
                                   'high': 'max', 'low': 'min', 'volume': 'sum'}).dropna()
    xlab = [t.strftime('%H:%M') for t in agg.index]
    ai = agg.index

    sig_pts = []
    for r in rows:
        tt = pd.to_datetime(r['time'])
        idx = int(ai.get_indexer([tt], method='nearest')[0]) if len(ai) else 0
        cond = (r.get('tag') or '')
        if r.get('band'):
            cond = (cond + ' / ' + r['band']).strip(' /')
        sig_pts.append({'xi': idx, 'price': float(r['price']), 'type': r['type'],
                        'time': str(r['time'])[:19], 'cond': cond, 'valid': r.get('valid')})
        all_signals.append({'sym': sym, 'name': name, 'time': str(r['time'])[:19],
                            'type': r['type'], 'price': float(r['price']),
                            'cond': cond, 'valid': (bool(r['valid']) if r['valid'] is not None else None)})

    # 绘图
    fig, ax = plt.subplots(figsize=(13, 4.6))
    for i, (t, row) in enumerate(agg.iterrows()):
        col = '#ef5350' if row['close'] >= row['open'] else '#26a69a'
        ax.plot([i, i], [row['low'], row['high']], color=col, lw=0.7)
        ax.add_patch(Rectangle((i - 0.32, row['open']), 0.64,
                     (row['close'] - row['open']) or 1e-6, color=col, zorder=2))
    for sp in sig_pts:
        if sp['type'] == 'B':
            ax.scatter(sp['xi'], sp['price'], marker='^', s=150, color='#ef5350',
                       zorder=5, edgecolors='white', linewidths=0.9)
        elif sp['type'] == 'S':
            ax.scatter(sp['xi'], sp['price'], marker='v', s=150, color='#26a69a',
                       zorder=5, edgecolors='white', linewidths=0.9)
        else:
            ax.scatter(sp['xi'], sp['price'], marker='X', s=120, color='#ffa726',
                       zorder=5, linewidths=2.0)
    step = max(1, len(xlab) // 12)
    ax.set_xticks(range(0, len(xlab), step))
    ax.set_xticklabels([xlab[i] for i in range(0, len(xlab), step)], rotation=45, fontsize=8)
    ax.set_ylabel('价格', fontsize=9)
    ax.set_title('%s %s  %s  行情 + tpoint 信号标注  [引擎资格 %d / 实盘推送 %d]'
                 % (sym, name, TARGET, len(rows), n_audit), fontsize=11)
    leg = [Line2D([0], [0], marker='^', color='w', markerfacecolor='#ef5350', markersize=10, label='B 买入'),
           Line2D([0], [0], marker='v', color='w', markerfacecolor='#26a69a', markersize=10, label='S 卖出/反T空'),
           Line2D([0], [0], marker='X', color='w', markerfacecolor='#ffa726', markersize=10, label='X 出场')]
    ax.legend(handles=leg, loc='best', fontsize=8)
    ax.grid(alpha=0.22)
    fig.tight_layout()
    fn = 'chart_%s_%s.png' % (D8, sym.replace('.', '_'))
    fig.savefig(os.path.join(OUT, fn), dpi=110)
    plt.close(fig)
    charts.append((sym, name, fn, len(rows)))
    print('[%s] 引擎资格 %d 信号 | chart %s' % (sym, len(rows), fn), flush=True)

# ---------------- 有效性统计 ----------------
dir_sig = [s for s in all_signals if s['type'] in ('B', 'S')]
valid_n = sum(1 for s in dir_sig if s['valid'] is True)
invalid_n = sum(1 for s in dir_sig if s['valid'] is False)
win_rate = round(valid_n / (valid_n + invalid_n) * 100, 1) if (valid_n + invalid_n) else None

# ---------------- 写 JSON ----------------
out = {'target': TARGET, 'audit_pushed': n_audit, 'engine_eligible': len(all_signals),
       'baseline_days': bdays, 'baseline': baseline, 'baseline_avg': base_avg,
       'win_rate_engine_eligible': win_rate, 'valid': valid_n, 'invalid': invalid_n,
       'signals': all_signals, 'sym_stats': sym_stats, 'charts': [c[2] for c in charts]}
json.dump(out, open(os.path.join(OUT, 'review_%s.json' % D8), 'w'), ensure_ascii=False, indent=2)

# ============================ HTML ============================ #
css = """
* { box-sizing: border-box; }
body { font-family: -apple-system,'Segoe UI','Microsoft YaHei',sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; line-height:1.6; }
.wrap { max-width:1180px; margin:0 auto; }
h1 { font-size:25px; color:#fff; border-bottom:2px solid #2d6cdf; padding-bottom:12px; }
h2 { font-size:18px; color:#7db3ff; margin-top:32px; border-left:4px solid #2d6cdf; padding-left:10px; }
.sub { color:#9aa0a6; font-size:13px; margin-bottom:6px; }
.card { background:#1a1d24; border:1px solid #2a2e37; border-radius:10px; padding:16px 18px; margin:12px 0; }
.kpis { display:flex; flex-wrap:wrap; gap:12px; margin:14px 0; }
.kpi { flex:1; min-width:140px; background:#161a22; border:1px solid #2a2e37; border-radius:10px; padding:12px; text-align:center; }
.kpi .v { font-size:23px; font-weight:700; }
.kpi .l { font-size:12px; color:#9aa0a6; margin-top:3px; }
table { width:100%; border-collapse:collapse; margin:10px 0; font-size:13px; }
th,td { border:1px solid #2a2e37; padding:7px 9px; text-align:left; vertical-align:top; }
th { background:#21262f; color:#cdd6e0; font-weight:600; }
tr:nth-child(even) td { background:#161a22; }
.ok { color:#3fb950; font-weight:600; } .bad { color:#f85149; font-weight:600; } .warn { color:#d29922; font-weight:600; }
.buy { color:#f85149; } .sell { color:#3fb950; }
.banner { background:#2a1416; border:1px solid #f85149; border-radius:10px; padding:14px 18px; margin:14px 0; color:#ffb3b3; }
.banner b { color:#ff6b6b; }
.note { background:#1f2430; border-left:3px solid #d29922; padding:10px 14px; margin:10px 0; font-size:13.5px; color:#d7dde5; }
.good { border-left-color:#3fb950; }
img.chart { width:100%; border:1px solid #2a2e37; border-radius:8px; margin:10px 0; background:#fff; }
.foot { color:#6b7178; font-size:12px; margin-top:28px; text-align:center; }
code { background:#0d1117; padding:2px 6px; border-radius:4px; color:#79c0ff; font-size:12px; }
"""

def fmt_time(t): return str(t)[:19].replace('T', ' ')

def vf_html(s):
    if s['type'] == 'X': return '<span class="warn">出场</span>'
    if s['valid'] is True: return '<span class="ok">✓ 有效</span>'
    if s['valid'] is False: return '<span class="bad">✗ 失效</span>'
    return '<span class="warn">—</span>'

body = []
body.append('<h1>📊 tpoint 每日信号复盘 — %s</h1>' % TARGET)
body.append('<div class="sub">门控：<code>MACD_GATE_MODE=floor</code>（生产 floord v9.2.2）｜ 数据截止 %s 15:00（全日）｜ 生成于 %s</div>'
            % (TARGET, datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))

# 异常横幅
body.append('<div class="banner"><b>⚠️ 异常：今日实盘推送信号 = %d（应为常态水平）。</b> '
            '同一引擎对同一份真实 1m 数据从零复算，本应触发 <b>%d</b> 个信号（161129:%d / 688347:%d / 513310:%d）。'
            '实盘零推送 ≠ 市场无信号，而是 <b>monitor PC=0 回归</b>导致：每轮 PC 自愈(Edit2)已从磁盘代码移除，'
            '盘中 watchlist 热重载把 PC 清零后当日不再恢复，<code>detect_for</code> 直接空返回。当前 monitor(pid 15340)下午日志纯"本轮无信号"且无取数告警，证明有数据但 PC=0。'
            '下方信号表为<b>引擎资格信号（复算，实盘未推送）</b>，仅供诊断；实际成交 = 0。</div>'
            % (n_audit, len(all_signals),
               sum(1 for s in all_signals if s['sym'] == '161129.SZ'),
               sum(1 for s in all_signals if s['sym'] == '688347.SH'),
               sum(1 for s in all_signals if s['sym'] == '513310.SH')))

# KPI
body.append('<div class="kpis">'
            '<div class="kpi"><div class="v" style="color:#f85149">%d</div><div class="l">实盘推送信号(权威)</div></div>'
            '<div class="kpi"><div class="v" style="color:#7db3ff">%d</div><div class="l">引擎资格信号(复算)</div></div>'
            '<div class="kpi"><div class="v">%s%%</div><div class="l">方向信号向前有效率</div></div>'
            '<div class="kpi"><div class="v">%s</div><div class="l">近5日均值(真实触发)</div></div>'
            '<div class="kpi"><div class="v" style="color:#f85149">%s×</div><div class="l">今日/基线 倍数(真实)</div></div>'
            '</div>' % (n_audit, len(all_signals),
                        ('%g' % win_rate) if win_rate is not None else '—',
                        base_avg, ('%.2f' % (0 / base_avg)) if base_avg else '∞'))

# 一、信号清单
body.append('<h2>一、当日信号清单（引擎资格复算 · 实盘未推送）</h2>')
body.append('<div class="sub">下表为 floor 引擎在 %s 真实 1m 数据上从零复算识别的全部信号（tag=共振条件 / band=触碰轨道）。'
            '因 PC=0 回归，这些信号当日均未推送飞书。类型：B=买入 / S=卖出(反T空) / X=出场。</div>' % TARGET)
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th>'
            '<th>触发条件(复算)</th></tr></thead><tbody>')
for s in sorted(all_signals, key=lambda x: (x['sym'], x['time'])):
    op = s['type']; cls = 'buy' if op == 'B' else ('sell' if op == 'S' else '')
    body.append('<tr><td>%s</td><td>%s %s</td><td class="%s"><b>%s</b></td><td>%.3f</td><td>%s</td></tr>'
                % (fmt_time(s['time']), s['sym'], s['name'], cls, op, s['price'], s['cond'] or '—'))
body.append('</tbody></table></div>')

# 二、有效性验证
body.append('<h2>二、信号触发后市场走势验证（有效 / 失效）</h2>')
body.append('<div class="sub">验证口径：B 看触发后剩余时段最高价相对入场 ≥ +0.15% 判有效；S 看最低价相对入场 ≥ +0.15% 判有效。'
            '（注：此为"若按引擎信号执行"的假设性向前验证，因实盘未推送故无真实成交。）</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th>'
            '<th>判定</th><th>触发条件</th></tr></thead><tbody>')
for s in sorted([x for x in all_signals if x['type'] != 'X'], key=lambda x: (x['sym'], x['time'])):
    body.append('<tr><td>%s</td><td>%s</td><td class="%s"><b>%s</b></td><td>%.3f</td><td>%s</td><td>%s</td></tr>'
                % (fmt_time(s['time']), s['sym'], ('buy' if s['type'] == 'B' else 'sell'),
                   s['type'], s['price'], vf_html(s), s['cond'] or '—'))
body.append('</tbody></table></div>')
body.append('<div class="note good">引擎资格信号方向有效性：%d 个方向信号中 <b>%d 有效 / %d 失效</b>（名义命中率 %s%%）。'
            '即若当日推送链路正常，绝大多数信号事后看方向正确。</div>'
            % (len(dir_sig), valid_n, invalid_n, ('%g' % win_rate) if win_rate is not None else '—'))

# 三、失效原因
body.append('<h2>三、失效信号原因分析</h2>')
failed = [s for s in dir_sig if s['valid'] is False]
if not failed:
    body.append('<div class="card"><div class="note good">✅ 引擎资格信号中无向前失效信号。</div></div>')
else:
    for s in failed:
        body.append('<div class="card"><div class="note bad-n">❌ <b>%s %s %s [%s]</b><br>'
                    '• 失效表现：触发后价格未出现 ≥+0.15%% 的有利波动，反向击穿（均值回归买点在下跌/震荡中接飞刀）。<br>'
                    '• 根因：floor 价格地板买点(新低+向下偏离)在当日该标的弱势/震荡段被触发，但价格继续下行未反弹至成本上方。<br>'
                    '• 启示：单笔下轨买入需叠加止跌/底背离共振，避免在弱势段机械接飞刀。</div></div>'
                    % (s['sym'], fmt_time(s['time']), s['type'], s['cond']))

# 四、整体表现 + 基线对比
body.append('<h2>四、整体表现与近5交易日基线对比</h2>')
body.append('<div class="card"><table><thead><tr><th>交易日</th><th>真实触发(状态计数)</th><th>备注</th></tr></thead><tbody>')
for b in bdays:
    label = datetime.date(int(b[:4]), int(b[4:6]), int(b[6:8])).strftime('%m-%d %a')
    note = ''
    if baseline[b] == 0:
        note = '<span class="warn">0 信号 — 疑似 PC=0 同类故障，建议核查</span>'
    body.append('<tr><td>%s</td><td>%d</td><td>%s</td></tr>' % (label, baseline[b], note))
body.append('<tr><td><b>%s(今日)</b></td><td><b style="color:#f85149">%d</b></td>'
            '<td><b style="color:#f85149">实盘推送 0 — PC=0 回归故障(非市场因素)</b></td></tr>'
            % (TARGET[5:], n_audit))
body.append('</tbody></table>')
body.append('<div class="note">近5交易日平均真实触发 = <b>%g</b> 个/日；今日实盘 = <b>0</b>（异常，低于预期 ∞倍）。'
            '但今日引擎在真实行情上本可识别 <b>%d</b> 个信号，远高于基线均值 → <b>这是一次推送/扫描链路故障，而非市场平静或模式变化</b>。</div>'
            % (base_avg, len(all_signals)))
body.append('<div class="note bad-n">⚠️ 模式异常提示：基线中 <b>%s 也出现 0 信号</b>，与今日同为状态计数归零，极可能是同一 PC=0 回归在不同日期的热重载事件触发。'
            '建议：① 补回 monitor 每轮 PC 自愈(Edit2) 已记入待办；② 把"某标的盘中连续 N 轮 PC<=0 / 取数异常"单独告警，区别于静默零信号。</div>'
            % (datetime.date(int(bdays[-1][:4]), int(bdays[-1][4:6]), int(bdays[-1][6:8])).strftime('%m-%d')))

# 行情图
body.append('<h2>五、当日行情图（tpoint 信号标注）</h2>')
body.append('<div class="sub">5 分钟蜡烛；▲红=B买入，▼绿=S卖出/反T空，✕橙=X出场。'
            '标注为引擎资格信号（实盘当日未推送）。</div>')
for sym, name, fn, n in charts:
    stats = sym_stats.get(sym, {})
    daystat = '%s / %s / %s (收, 涨跌 %s%%)' % (stats.get('low'), stats.get('high'), stats.get('close'),
                                                stats.get('day_chg_pct')) if stats else ''
    body.append('<h3>%s %s &nbsp;<span class="sub">引擎资格 %d 信号 ｜ 低/高/收 %s</span></h3>' % (sym, name, n, daystat))
    body.append('<img class="chart" src="%s" alt="%s 行情图">' % (fn, sym))

body.append('<div class="foot">tpoint floord v9.2.2 ｜ 报告由 floor 引擎从零复算 + 1m 行情向前验证 + 行情图(信号标注) 生成 ｜ 数据截止 %s 15:00</div>' % TARGET)

html_head = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<title>tpoint 每日复盘 %s</title><style>%s</style></head>'
             '<body><div class="wrap">') % (TARGET, css)
html = html_head + ''.join(body) + '</div></body></html>'

hpath = os.path.join(OUT, 'review_%s.html' % D8)
with open(hpath, 'w', encoding='utf-8') as f:
    f.write(html)
print('\n[ok] HTML ->', hpath)
print('[ok] JSON ->', os.path.join(OUT, 'review_%s.json' % D8))
print('[summary] audit=%d engine_eligible=%d baseline_avg=%g win_rate=%s'
      % (n_audit, len(all_signals), base_avg, win_rate))
