#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""build_review_html.py — tpoint 每日信号复盘 HTML 生成器（2026-08-04 实盘化重构 v2）

报告哲学（用户拍板 2026-08-04 晚）：只看「真实推送给我的交易信号」。
- 信号唯一权威源 = data/push_audit.jsonl（飞书ACK确认）；复算信号不进报告（仅后台对账）。
- 有效判定 = round-trip 净盈亏 > 0（扣双边成本），替代旧 0.15% 阈值。
- 五节结构：〇实盘投递诊断 / 一 round-trip有效性 / 二 负收益根因 / 三 实盘基线对比 /
  四 行情图（仅有推送标的）/ 五 波动段捕获分析（自迭代改进建议）。

读取：output/live_review_<D>.json（live_roundtrip_review.py 产出）
     data/push_audit.jsonl、data/watchlist.json、output/chart_<D>_<sym>.png（base64 内嵌）
CLI：python build_review_html.py [YYYY-MM-DD]（缺省=今天）
产出：output/review_<D>.html
"""
import os, sys, json, base64, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
OUT = os.path.join(ROOT, 'output')

TARGET = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')

live = json.load(open(os.path.join(OUT, f'live_review_{TARGET}.json'), encoding='utf-8'))
wl = json.load(open(os.path.join(DATA, 'watchlist.json'), encoding='utf-8'))

# ---------- push_audit（当日，含失败记录） ----------
audit = []
with open(os.path.join(DATA, 'push_audit.jsonl'), encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get('ts', '')).startswith(TARGET):
            audit.append(r)
audit.sort(key=lambda x: x['ts'])


def sym_label(sym):
    """标的标注统一格式：'300058.SZ 蓝色光标'。"""
    nm = wl.get(sym, '')
    return ('%s %s' % (sym, nm)) if nm else sym


def b64(path):
    return 'data:image/png;base64,' + base64.b64encode(open(path, 'rb').read()).decode('ascii')


def esc(x):
    return str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ---------- 〇节诊断（仅实盘链路，不含复算对比） ----------
n_B = sum(1 for r in audit if r.get('type') == 'B')
n_S = sum(1 for r in audit if r.get('type') == 'S')
n_X = sum(1 for r in audit if r.get('type') == 'X')
n_backfill = sum(1 for r in audit if r.get('backfill'))
n_ok = sum(1 for r in audit if r.get('ok'))
ok_rate = (n_ok / len(audit) * 100) if audit else 0.0
t_first = audit[0]['ts'][11:19] if audit else '—'
t_last = audit[-1]['ts'][11:19] if audit else '—'
syms_pushed = sorted({r.get('sym') for r in audit if r.get('sym')})

dups, _dup_ids = [], set()
for i in range(1, len(audit)):
    a, b = audit[i - 1], audit[i]
    if a.get('sym') == b.get('sym') and a.get('type') == b.get('type') and a.get('sym'):
        try:
            dt = abs((datetime.datetime.strptime(b['ts'], '%Y-%m-%d %H:%M:%S')
                      - datetime.datetime.strptime(a['ts'], '%Y-%m-%d %H:%M:%S')).total_seconds())
        except Exception:
            dt = 9999
        if dt <= 120:
            dups.append((a, b))
            _dup_ids.add(id(b))

findings, suggestions = [], []
if not audit:
    findings.append(('bad', '今日 push_audit 零记录：推送链路整体断流，或全标的零信号（需核对 monitor 日志区分）。'))
    suggestions.append('核查 monitor/watchdog 进程状态与信号群 webhook 连通性；若 monitor 正常而零信号，属无信号日，无需动作。')
else:
    if n_ok == len(audit):
        findings.append(('ok', f'推送链路：{len(audit)} 笔推送飞书全部 ACK 成功（code=0），成功率 100%。'))
    else:
        findings.append(('bad', f'推送链路：{len(audit) - n_ok}/{len(audit)} 笔飞书 ACK 失败，存在丢推。'))
        suggestions.append('飞书 ACK 失败 → 检查 _push_retry 退避与 webhook 频限；push_pending 补发队列是否生效。')
    cov = '早盘+午盘' if (t_first < '11:30' and t_last > '13:00') else '部分时段'
    findings.append(('ok', f'及时性：首笔 {t_first} / 末笔 {t_last}，推送时段覆盖 {cov}。'))
if n_backfill:
    findings.append(('warn', f'数据质量：{n_backfill} 笔为盘后人工补录——当日盘中落盘断流（推送成功但未落 audit/signal.txt）。'))
    suggestions.append('落盘断流 → monitor 写后缓冲已部署（_buffered_append + ctypes 降级 + 主循环自愈回写），观察是否归零；'
                       '若再发，排查 EDR 对 data/ 目录已存在文件的 CRT _wopen 拦截。')
for a_, b_ in dups:
    findings.append(('warn', f'重复推送：{sym_label(a_["sym"])} {a_["type"]} 于 {a_["ts"][11:19]} 与 {b_["ts"][11:19]} 各推一次'
                             '（120s 内同型重复，疑似并发进程/重启重放）。'))
if dups:
    suggestions.append('重复推送 → R1 靶点「信号指纹幂等（sym+bar_ts+type+price）+ 单实例锁 STALE_LOCK 误判修复」。')
if audit and not dups and not n_backfill and n_ok == len(audit):
    findings.append(('ok', '完整性：逐笔可验证，无缺失/重复/补录。'))
    suggestions.append('投递链路健康，维持现状。')

# ============================ HTML ============================ #
css = """
* { box-sizing: border-box; }
body { font-family: -apple-system,'Segoe UI','Microsoft YaHei',sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; line-height:1.6; }
.wrap { max-width:1200px; margin:0 auto; }
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
.bad-n { border-left-color:#f85149; }
.chart { width:100%; border:1px solid #2a2e37; border-radius:8px; margin:10px 0; background:#fff; }
.foot { color:#6b7178; font-size:12px; margin-top:28px; text-align:center; }
code { background:#0d1117; padding:2px 6px; border-radius:4px; color:#79c0ff; font-size:12px; }
"""

sm = live['summary']
bl = live['baseline']
vol = live['volatility']
trips = live['trips']

body = []
body.append('<h1>📊 tpoint 每日信号复盘 — %s</h1>' % TARGET)
body.append('<div class="sub">生产 monitor v9.3.0 ｜ 信号源=实盘推送（push_audit 飞书ACK确认）｜ 有效=round-trip 净盈亏&gt;0（扣双边成本：佣金万1+印花(仅股票)+滑点2bps/边）｜ 数据截止 %s 15:00 ｜ 生成于 %s</div>'
            % (TARGET, live['generated_at']))

# 异常横幅（仅 bad 级诊断触发）
bad_hits = [t for lv, t in findings if lv == 'bad']
if bad_hits:
    body.append('<div class="banner"><b>⚠️ 今日投递链路异常（需关注）</b><br>'
                + '<br>'.join('· ' + esc(t) for t in bad_hits) + '</div>')

# 总览 KPI
vr = sm['valid_rate_pct']
body.append('<div class="kpis">'
            f'<div class="kpi"><div class="v" style="color:#7db3ff">{sm["n_pushes"]}</div><div class="l">实盘推送(买{n_B}/卖{n_S}/出{n_X})</div></div>'
            f'<div class="kpi"><div class="v">{sm["n_trips"]}</div><div class="l">round-trip 配对</div></div>'
            f'<div class="kpi"><div class="v {"ok" if (vr or 0) >= 50 else "bad"}">{"—" if vr is None else str(vr) + "%"}</div><div class="l">T 单有效率(净>0)</div></div>'
            f'<div class="kpi"><div class="v {"ok" if sm["net_sum_pct"] > 0 else "bad"}">{sm["net_sum_pct"]:+.3f}%</div><div class="l">净盈亏合计</div></div>'
            f'<div class="kpi"><div class="v">{sm["gross_sum_pct"]:+.3f}%</div><div class="l">毛差合计</div></div>'
            '</div>')

# ================= 〇、实盘投递实况与诊断 =================
body.append('<h2>〇、今日实盘投递实况与诊断（push_audit 逐笔）</h2>')
body.append('<div class="sub">本节目的：追踪验证推送链路<b>及时性/完整性/准确性</b>，定位根因驱动自迭代。仅含真实推送，复算信号不参与。</div>')
body.append('<div class="kpis">'
            f'<div class="kpi"><div class="v">{len(audit)}</div><div class="l">投递总数(买{n_B}/卖{n_S}/出{n_X})</div></div>'
            f'<div class="kpi"><div class="v">{len(syms_pushed)}</div><div class="l">覆盖标的数</div></div>'
            f'<div class="kpi"><div class="v {"ok" if ok_rate == 100 and audit else "bad"}">{ok_rate:.0f}%</div><div class="l">飞书ACK成功率</div></div>'
            f'<div class="kpi"><div class="v">{t_first}</div><div class="l">首笔推送</div></div>'
            f'<div class="kpi"><div class="v">{t_last}</div><div class="l">末笔推送</div></div>'
            '</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th><th>飞书ACK</th><th>备注</th></tr></thead><tbody>')
TYPE_CN0 = {'B': '买入', 'S': '卖出', 'X': '出场'}
for r in audit:
    op = r.get('type') or '?'
    ocls = 'buy' if op == 'B' else ('sell' if op == 'S' else '')
    ack = '<span class="ok">✓</span>' if r.get('ok') else '<span class="bad">✗ %s</span>' % esc(r.get('feishu_msg') or '')
    notes = []
    if r.get('backfill'):
        notes.append('盘后补录（盘中落盘断流）')
    if id(r) in _dup_ids:
        notes.append('120s内同型重复')
    body.append('<tr><td>%s</td><td>%s</td><td class="%s"><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>'
                % (r['ts'][11:19], esc(sym_label(r.get('sym') or '—')), ocls, '%s(%s)' % (op, TYPE_CN0.get(op, '')),
                   r.get('price') if r.get('price') is not None else '—', ack, '；'.join(notes) or '—'))
body.append('</tbody></table></div>')
body.append('<div class="card"><b>诊断结论（根因分析）</b><ul style="margin:8px 0 0 18px">')
for lv, t in findings:
    cls = {'ok': 'ok', 'warn': 'warn', 'bad': 'bad'}[lv]
    tag = {'ok': '正常', 'warn': '关注', 'bad': '异常'}[lv]
    body.append(f'<li><span class="{cls}">[{tag}]</span> {esc(t)}</li>')
body.append('</ul></div>')
body.append('<div class="card"><b>可执行改进建议（自迭代驱动）</b><ol style="margin:8px 0 0 18px">'
            + ''.join('<li>%s</li>' % esc(s) for s in suggestions) + '</ol></div>')

# ================= 一、round-trip 有效性验证 =================
body.append('<h2>一、实盘信号 round-trip 有效性验证（一B一S 完整做T，净盈亏&gt;0 为有效）</h2>')
body.append('<div class="sub">配对规则：单仓位；正T B→S/X/EOD，反T S→B回补/EOD收盘回补；进出场价=当日推送价（EOD=收盘），成本=%s。同向重复信号按生产单仓位模型忽略。</div>'
            % esc(live['cost_model']))
if not trips:
    body.append('<div class="card"><div class="note">今日实盘推送未形成完整 round-trip（无配对或零推送）。</div></div>')
else:
    body.append('<div class="card"><table><thead><tr><th>标的</th><th>方向</th><th>进场</th><th>出场</th><th>持有</th><th>出场原因</th>'
                '<th>毛差%</th><th>成本%</th><th>净盈亏%</th><th>判定</th></tr></thead><tbody>')
    for t in trips:
        vcls = 'ok' if t['valid'] else 'bad'
        vtx = '✓ 有效' if t['valid'] else '✗ 无效'
        dcls = 'buy' if t['dir'] == '正T' else 'sell'
        body.append('<tr><td>%s</td><td class="%s"><b>%s</b></td><td>%s @%s</td><td>%s @%s</td><td>%s根</td><td>%s</td>'
                    '<td>%+.3f</td><td>%.3f</td><td><b>%+.3f</b></td><td><span class="%s">%s</span></td></tr>'
                    % (esc(sym_label(t['sym'])), dcls, t['dir'], t['entry_time'], t['entry_price'],
                       t['exit_time'], t['exit_price'], t['hold_bars'], t['exit_reason'],
                       t['gross_ret_pct'], t['cost_pct'], t['net_ret_pct'], vcls, vtx))
    body.append('</tbody></table></div>')
    if sm['orphans']:
        body.append('<div class="note">未配对信号（单仓位模型忽略）：%s</div>'
                    % '；'.join('%s %s %s（%s）' % (o['ts'][11:19], sym_label(o.get('sym', '—')), o['type'], o['note'])
                               for o in sm['orphans']))

# ================= 二、负收益 T 单根因分析 =================
body.append('<h2>二、负收益 T 单根因分析</h2>')
losses = [t for t in trips if not t['valid']]
if not losses:
    body.append('<div class="card"><div class="note good">✅ 今日无负收益 T 单。</div></div>')
else:
    body.append('<div class="sub">为什么做完 T 是负的：逐单归因（趋势/出场/成本/滑点四维），归因直接映射到可优化因子。</div>')
    for t in losses:
        tags = ''.join('<li>%s</li>' % esc(x) for x in t.get('loss_tags', ['未归类']))
        body.append('<div class="card"><div class="note bad-n">❌ <b>%s %s</b>　%s @%s → %s @%s（%s）　净 <b>%+.3f%%</b>'
                    '<ul style="margin:6px 0 0 18px">%s</ul></div></div>'
                    % (esc(sym_label(t['sym'])), t['dir'], t['entry_time'], t['entry_price'],
                       t['exit_time'], t['exit_price'], t['exit_reason'], t['net_ret_pct'], tags))

# ================= 三、整体表现 vs 近5日基线（纯实盘口径） =================
body.append('<h2>三、整体表现与近5交易日基线对比（实盘推送 round-trip 口径）</h2>')
body.append('<div class="sub">基线与今日完全同源：历史 push_audit 实推 + 当日行情配对。复算/模拟信号不参与决策，故不参与对比。</div>')
bm = bl['mean']
rows = [
    ('日均推送数', str(sm['n_pushes']), '—' if bm['n_pushes'] is None else str(bm['n_pushes'])),
    ('T 单有效率%', '—' if vr is None else '%g' % vr, '—' if bm['valid_rate_pct'] is None else '%g' % bm['valid_rate_pct']),
    ('净盈亏合计%', '%+.3f' % sm['net_sum_pct'], '—' if bm['net_sum_pct'] is None else '%+.3f' % bm['net_sum_pct']),
]
body.append('<div class="card"><table><thead><tr><th>指标</th><th>今日</th><th>近5日实盘基线均值</th></tr></thead><tbody>')
for m, cur, base_ in rows:
    body.append('<tr><td>%s</td><td><b>%s</b></td><td>%s</td></tr>' % (m, cur, base_))
body.append('</tbody></table>')
body.append('<table><thead><tr><th>日期</th><th>推送数</th><th>配对数</th><th>有效率%</th><th>净盈亏合计%</th></tr></thead><tbody>')
for d in bl['days']:
    body.append('<tr><td>%s</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td></tr>'
                % (d['date'], d['n_pushes'], d['n_trips'],
                   '—' if d['valid_rate_pct'] is None else d['valid_rate_pct'],
                   '%+.3f' % d['net_sum_pct']))
body.append('</tbody></table></div>')
if bl.get('note'):
    body.append('<div class="note">⚠️ %s</div>' % esc(bl['note']))

# ================= 四、行情图（仅有实盘推送的标的） =================
body.append('<h2>四、当日行情图（1m 分时 · 仅有实盘推送的标的）</h2>')
if not live['syms_with_push']:
    body.append('<div class="card"><div class="note">今日无实盘推送信号，按规则不绘制行情图。</div></div>')
else:
    body.append('<div class="sub">仅展示今日有实盘推送的标的（%d 只）；标注为真实推送点（非复算）。</div>' % len(live['syms_with_push']))
    for sym in live['syms_with_push']:
        cp = os.path.join(OUT, 'chart_%s_%s.png' % (TARGET, sym.replace('.', '_')))
        if os.path.exists(cp):
            body.append('<div class="card"><b>%s</b><br><img class="chart" src="%s"></div>' % (esc(sym_label(sym)), b64(cp)))

# ================= 五、当日行情捕获分析（仅结论：优化空间清单） =================
body.append('<h2>五、当日行情捕获分析（v9.3.0 优化空间）</h2>')
body.append('<div class="sub">后台已对全天 1m 行情做波动段切分与逐段归因（明细数据留档 live_review_%s.json，正文不展开）。'
            '此处仅输出结论：当前版本的捕获短板、可能原因与改进方向。</div>' % TARGET)
pool_amp = sum(v['total_amp_pct'] for v in vol.values() if 'total_amp_pct' in v)
pool_cap = sum(v['captured_pct'] for v in vol.values() if 'captured_pct' in v)
pool_sig_amp = sum(v.get('sig_amp_pct', 0) for v in vol.values())
pool_rate = round(min(pool_cap / pool_amp * 100, 100), 1) if pool_amp > 0 else None
pool_sig_rate = round(min(pool_cap / pool_sig_amp * 100, 100), 1) if pool_sig_amp > 0 else None
body.append('<div class="kpis">'
            f'<div class="kpi"><div class="v">{pool_amp:.1f}%</div><div class="l">当日有效波动总幅度</div></div>'
            f'<div class="kpi"><div class="v">{pool_cap:+.2f}%</div><div class="l">实盘捕获毛差</div></div>'
            f'<div class="kpi"><div class="v warn">{"—" if pool_sig_rate is None else str(pool_sig_rate) + "%"}</div><div class="l">显著段捕获率</div></div>'
            f'<div class="kpi"><div class="v">{len(live.get("opportunities", []))}</div><div class="l">优化空间项数</div></div>'
            '</div>')
for i, o in enumerate(live.get('opportunities', []), 1):
    body.append('<div class="card"><b>优化空间 %d</b>'
                '<table><tbody>'
                '<tr><td style="width:88px;color:#9aa0a6">问题描述</td><td>%s</td></tr>'
                '<tr><td style="color:#9aa0a6">可能原因</td><td>%s</td></tr>'
                '<tr><td style="color:#9aa0a6">改进方向</td><td><span class="warn">%s</span></td></tr>'
                '</tbody></table></div>'
                % (i, esc(o['problem']), esc(o['cause']), esc(o['direction'])))

body.append('<div class="foot">tpoint v9.3.0 ｜ 报告基于实盘推送 round-trip（净盈亏口径）+ 1m 行情生成 ｜ 数据截止 %s 15:00</div>' % TARGET)

html = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>tpoint 每日信号复盘 %s</title><style>%s</style></head>'
        '<body><div class="wrap">') % (TARGET, css) + ''.join(body) + '</div></body></html>'

hpath = os.path.join(OUT, 'review_%s.html' % TARGET)
with open(hpath, 'w', encoding='utf-8') as f:
    f.write(html)
print('[ok] HTML -> %s (%d bytes)' % (hpath, len(html.encode('utf-8'))))
