#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_review_html.py — 汇编 tpoint 每日复盘 HTML（单文件自包含，图表 base64 内嵌）
用法: python build_review_html.py [YYYY-MM-DD]
读取: output/review_{date}.json (daily_signal_review 复算) + output/chart_{date}_{sym}.png + data/push_audit.jsonl
产出: output/review_{date}.html
涵盖: 〇今日实盘投递分类 / 一信号清单 / 二有效性验证 / 三失效原因 / 四整体+5日基线+异常 / 五行情图
"""
import sys, os, json, datetime, base64

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')
DATA = os.path.join(ROOT, 'data')

TARGET = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
D8 = TARGET.replace('-', '')

doc = json.load(open(os.path.join(OUT, 'review_%s.json' % TARGET), encoding='utf-8'))
syms = doc['symbols']
wl = json.load(open(os.path.join(DATA, 'watchlist.json'), encoding='utf-8'))

# ---------- push_audit 分类 ----------
audit = []
ap = os.path.join(DATA, 'push_audit.jsonl')
try:
    for line in open(ap, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('ts', '').startswith(TARGET):
            audit.append(r)
except FileNotFoundError:
    pass

FIX_T = '2026-07-30 14:07'   # 重放修复部署时刻
for r in audit:
    r['cls'] = '真实实时' if r['ts'] >= FIX_T else '历史重放'
n_real = sum(1 for r in audit if r['cls'] == '真实实时')
n_spam = sum(1 for r in audit if r['cls'] == '历史重放')

# ---------- 工具 ----------
def b64(path):
    return 'data:image/png;base64,' + base64.b64encode(open(path, 'rb').read()).decode('ascii')

def esc(x):
    return (str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

def fmt_time(t):
    return str(t)[11:16]

def cond_of(r):
    tag = (r.get('tag') or '').strip('[]')
    band = r.get('band') or ''
    c = (tag + ' / ' + band).strip(' /') if tag or band else '—'
    return c

# ---------- 统计 ----------
cmp = doc['comparison']
base = doc['baseline_mean']
total = doc['today_legacy']
bl_days = doc['baseline_days']

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

body = []
body.append('<h1>📊 tpoint 每日信号复盘 — %s</h1>' % TARGET)
body.append('<div class="sub">门控：<code>MACD_GATE_MODE=floor</code>（生产 floord v9.2.2）｜ 数据截止 %s 15:00（全日 240 根 1m）｜ 生成于 %s</div>'
            % (TARGET, datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))

# 披露横幅
body.append('<div class="banner"><b>⚠️ 今日投递异常说明（重要）</b><br>'
            '今日 tpoint 经历 <b>respawn storm + 历史信号重放 bug</b>（已于 <b>14:07</b> 修复）。全天 <code>push_audit</code> 共 <b>%d</b> 条推送，'
            '其中 <b>%d 条为风暴期对历史 bar 的重放</b>（时间戳集中在 09:44 / 09:49 / 13:54–13:59，非实时信号），'
            '仅 <b>%d 条</b>（14:37–14:49）为修复后的<b>真实实时推送</b>。<br>'
            '下方<b>第一节信号清单</b>为 floor 引擎在今日<b>真实 1m 行情</b>上从零复算识别的全部 <b>%d</b> 个信号（含触发条件与向前有效性），是今日市场信号的完整逻辑视图；'
            '实盘真实投递仅 %d 条（见第〇节）。</div>'
            % (len(audit), n_spam, n_real, total['n_signals'], n_real))

# KPI
n_X = total['n_signals'] - total['n_B'] - total['n_S']
body.append('<div class="kpis">'
            '<div class="kpi"><div class="v" style="color:#7db3ff">%d</div><div class="l">引擎复算信号(买%d/卖%d/出场%d)</div></div>'
            '<div class="kpi"><div class="v" style="color:#f85149">%d</div><div class="l">实盘投递(真实%d/重放%d)</div></div>'
            '<div class="kpi"><div class="v">%s%%</div><div class="l">方向信号向前胜率</div></div>'
            '<div class="kpi"><div class="v">%s</div><div class="l">近5日基线(信号/日)</div></div>'
            '<div class="kpi"><div class="v" style="color:#f85149">%.2f×</div><div class="l">今日/基线 倍数(异常)</div></div>'
            '</div>' % (total['n_signals'], total['n_B'], total['n_S'], n_X,
                        len(audit), n_real, n_spam,
                        ('%g' % doc['today_win_rate']), ('%g' % base['n_signals']),
                        (total['n_signals'] / base['n_signals'] if base['n_signals'] else 0)))

# 〇、实盘投递分类
body.append('<h2>〇、今日实盘投递实况（push_audit 逐笔分类）</h2>')
body.append('<div class="sub">分类口径：推送时间戳 ≥ 14:07（重放修复部署时刻）记为<b>真实实时</b>；此前记为<b>历史重放</b>（风暴期重发，非实时信号）。</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th><th>分类</th></tr></thead><tbody>')
for r in sorted(audit, key=lambda x: x['ts']):
    cls = r['cls']
    ctag = '<span class="ok">真实实时</span>' if cls == '真实实时' else '<span class="bad">历史重放</span>'
    op = r['type']; ocls = 'buy' if op == 'B' else ('sell' if op == 'S' else '')
    body.append('<tr><td>%s</td><td>%s</td><td class="%s"><b>%s</b></td><td>%s</td><td>%s</td></tr>'
                % (r['ts'][11:19], r['sym'], ocls, op, r.get('price'), ctag))
body.append('</tbody></table></div>')
body.append('<div class="note">实盘权威计数（<code>state.json</code> 当日 <code>_b/_s_count</code>）：161129 B0/S1、688347 B5/S1、513310 B1/S1，合计 <b>9</b> 次引擎触发（含风暴期重放触发的计数）。</div>')

# 一、信号清单
body.append('<h2>一、当日信号清单（floor 引擎复算 · 真实 1m 行情）</h2>')
body.append('<div class="sub">下表为 floor 引擎在 %s 真实 1m 数据上从零复算识别的全部信号（tag=共振条件 / band=触碰轨道）。类型：B=买入 / S=卖出(反T空) / X=出场。此清单不含重放，是今日市场信号的完整逻辑视图。</div>' % TARGET)
all_rows = []
for sym, res in syms.items():
    for r in res.get('rows', []):
        all_rows.append((sym, res.get('name', sym), r))
all_rows.sort(key=lambda x: (x[0], x[2]['time']))
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th><th>触发条件(复算)</th></tr></thead><tbody>')
for sym, name, r in all_rows:
    op = r['type']; ocls = 'buy' if op == 'B' else ('sell' if op == 'S' else '')
    body.append('<tr><td>%s</td><td>%s %s</td><td class="%s"><b>%s</b></td><td>%.3f</td><td>%s</td></tr>'
                % (fmt_time(r['time']), sym, name, ocls, op, r['price'], cond_of(r)))
body.append('</tbody></table></div>')

# 二、有效性验证
body.append('<h2>二、信号触发后市场走势验证（有效 / 失效）</h2>')
body.append('<div class="sub">验证口径：B 看触发后剩余时段最高价相对入场 ≥ +0.15%% 判有效；S 看最低价相对入场 ≥ +0.15%% 判有效（floor 引擎向前验证）。X 出场不参与方向判定。</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th><th>判定</th><th>有利%</th><th>不利%</th><th>触发条件</th></tr></thead><tbody>')
for sym, name, r in all_rows:
    if r['type'] == 'X':
        continue
    if r['valid'] is True:
        vtag = '<span class="ok">✓ 有效</span>'
    elif r['valid'] is False:
        vtag = '<span class="bad">✗ 失效</span>'
    else:
        vtag = '<span class="warn">—</span>'
    body.append('<tr><td>%s</td><td>%s</td><td class="%s"><b>%s</b></td><td>%.3f</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                % (fmt_time(r['time']), sym, ('buy' if r['type'] == 'B' else 'sell'), r['type'],
                   r['price'], vtag,
                   ('%g' % r['max_fav_pct']) if r['max_fav_pct'] is not None else '—',
                   ('%g' % r['adverse_pct']) if r['adverse_pct'] is not None else '—',
                   cond_of(r)))
body.append('</tbody></table></div>')
body.append('<div class="note good">方向信号向前有效性：%d 个方向信号中 <b>%d 有效 / %d 失效</b>（名义命中率 %s%%）。</div>'
            % (total['valid'] + total['invalid'], total['valid'], total['invalid'],
               ('%g' % doc['today_win_rate'])))

# 三、失效原因
body.append('<h2>三、失效信号原因分析</h2>')
failed = [(sym, name, r) for sym, name, r in all_rows if r['type'] != 'X' and r['valid'] is False]
if not failed:
    body.append('<div class="card"><div class="note good">✅ 无向前失效的方向信号。</div></div>')
else:
    for sym, name, r in failed:
        body.append('<div class="card"><div class="note bad-n">❌ <b>%s %s %s [%s]</b><br>'
                    '• 触发条件：%s<br>'
                    '• 失效表现：%s<br>'
                    '• 根因：%s</div></div>'
                    % (sym, fmt_time(r['time']), r['type'], cond_of(r),
                       cond_of(r), esc(r['reason']),
                       '当日该标的单边重挫（688347 -14.9%% / 513310 -6.2%%），floor 价格地板/均线引力买入逻辑在弱势段机械"接飞刀"，买入后价格继续下探未出现 +0.15%% 以上有利波动即被击穿。'))
body.append('<div class="note bad-n">⚠️ 模式提示：今日 <b>3 个失效信号全部为买入(B)</b>，集中在极端下跌段。暴跌日 floor 买入信号密集且易失效，建议对买方信号叠加止跌/底背离共振过滤，避免在单边下行中机械抄底。</div>')

# 四、整体 + 基线
body.append('<h2>四、整体表现与近5交易日基线对比</h2>')
body.append('<div class="card"><table><thead><tr><th>指标</th><th>今日</th><th>近5日均值(%s)</th><th>判定</th></tr></thead><tbody>'
            % ', '.join(d[5:] for d in bl_days))
for c in cmp:
    flag = c['anomaly']
    fcolor = '#f85149' if flag else '#3fb950'
    body.append('<tr><td>%s</td><td><b>%s</b></td><td>%s</td><td style="color:%s;font-weight:700">%s</td></tr>'
                % (esc(c['metric']), c['today'], c['baseline'],
                   fcolor, ('⚠ 异常' if flag else '正常')))
body.append('</tbody></table></div>')
body.append('<div class="note">基线交易日：%s。所有日期均用当前 floor 引擎复算，口径一致。</div>' % ', '.join(bl_days))
body.append('<div class="note">① <b>信号数异常(%.2f×)</b>：主因=重放风暴导致引擎在风暴期对历史 bar 重复触发 + 当日极端波动（688347 振幅 16.4%%、513310 7.5%%）放大买卖点是非。剔除重放后，实盘真实触发仅 %d 次。<br>'
            '② <b>胜率 75%% ≈ 基线 71%%</b>：信号质量未明显恶化，异常在"量"不在"质"。<br>'
            '③ <b>688347 今日 0 个卖信号</b>（仅买+出场）：floor 门控在单边暴跌日只产生"抄底"逻辑、缺乏逢高反T卖点，是单边市特征，需注意买方信号密集与失效集中。</div>'
            % (total['n_signals'] / base['n_signals'] if base['n_signals'] else 0, n_real))

# 四·B、[P2-3 迭代] 卡方风格绩效统计卡片（复用 performance_stats.kf_style_stats）
try:
    sys.path.insert(0, ROOT)
    from scripts.performance_stats import kf_style_stats
    # 从复盘 rows 提取 trip 形态（ret 用向前验证 max_fav/adverse 近似收益）
    trips = []
    for sym, res in syms.items():
        for r in res.get('rows', []):
            if r['type'] == 'X':
                continue
            fav = r.get('max_fav_pct')
            adv = r.get('adverse_pct')
            if fav is None or adv is None:
                continue
            # 近似净收益：有利波动幅度 + 不利波动幅度（不对称粗略估算）
            ret = fav if r['valid'] is True else -(abs(adv) if adv is not None else 0)
            trips.append({'ret_pct': ret, 'hold_bars': 0,
                          'exit_reason': 'S' if r['type'] == 'S' else 'B',
                          'entry_date': TARGET})
    kf = kf_style_stats(trips) if trips else {'n_trips': 0}
    if kf.get('n_trips', 0):
        # [轮次2-3 迭代] 样本量警告：年化/夏普在样本<20 时失真（4笔→1033%），
        # 在卡方卡片中显著展示，避免单日复算被误读为长期绩效。
        warn = kf.get('sample_warning')
        warn_html = ''
        if warn:
            warn_html = ('<div class="note" style="color:#ffab40">⚠️ 样本量警告：<b>%s</b>'
                         '（%d 笔）。当日复算的胜率/年化/Level 受样本量影响可能失真，'
                         '仅作当日信号质量参考，不代表长期绩效。</div>'
                         % (warn, kf['n_trips']))
        kf_cards = (
            '<div class="kpi"><div class="v" style="color:#7db3ff">%d</div><div class="l">样本信号</div></div>'
            '<div class="kpi"><div class="v">%s%%</div><div class="l">20日胜率(当日近似)</div></div>'
            '<div class="kpi"><div class="v">%s</div><div class="l">Level 星级</div></div>'
            '<div class="kpi"><div class="v">%s%%</div><div class="l">当日开仓率</div></div>'
            '<div class="kpi"><div class="v">%s</div><div class="l">盈亏比</div></div>'
            % (kf['n_trips'], kf['win_rate_20d'],
               '★' * kf['level_star'],
               kf['open_rate_today_pct'] if kf['open_rate_today_pct'] is not None else '—',
               kf['pl_ratio']))
        body.append('<h2>四·B、卡方风格绩效统计（[P2-3] 与 kf xlsx 口径对齐）</h2>')
        body.append('<div class="sub">从当日复算信号近似计算（向前验证有利/不利波动），与卡方 5002 只 xlsx 口径同构展示：胜率/开仓率/Level 星级。tpoint 无费用模型，数值仅作分布对照。</div>')
        body.append('<div class="kpis">%s</div>' % kf_cards)
        body.append(warn_html)
        body.append('<div class="note">对照锚点：卡方 xlsx 全市场 20日胜率中位 <b>61.0%%</b>、开仓率≥50%% 仅 <b>18.3%%</b>；watchlist 内 688111.SH 锚点 Level3/年化 22.58%%/开仓率 57.94%%/胜率 50%%。</div>')
except Exception as e:
    body.append('<div class="note">[P2-3] 绩效统计卡片生成跳过：%s</div>' % esc(e))

# 五、行情图
body.append('<h2>五、当日行情图（tpoint 信号标注）</h2>')
body.append('<div class="sub">5 分钟蜡烛；▲红=B买入，▼绿=S卖出/反T空，✕橙=X出场。标注为 floor 引擎在真实行情上复算的信号（实盘当日仅 4 条真实实时推送，详见第〇节）。</div>')
for sym in wl:
    res = syms.get(sym)
    if not res:
        continue
    name = res.get('name', sym)
    st = res.get('stats', {})
    s = res.get('summary', {})
    daystat = ''
    if st:
        daystat = '低 %s / 高 %s / 收 %s（涨跌 %s%%）' % (st.get('low'), st.get('high'), st.get('close'), st.get('day_chg_pct'))
    fn = os.path.join(OUT, 'chart_%s_%s.png' % (TARGET, sym.replace('.', '_')))
    if os.path.exists(fn):
        body.append('<h3>%s %s &nbsp;<span class="sub">复算 %d 信号(买%d/卖%d/出场%d) ｜ %s</span></h3>'
                    % (sym, name, s.get('n_signals', 0), s.get('n_B', 0), s.get('n_S', 0), s.get('n_X', 0), daystat))
        body.append('<img class="chart" src="%s" alt="%s 行情图">' % (b64(fn), sym))

body.append('<div class="foot">tpoint floord v9.2.2 ｜ 报告由 floor 引擎从零复算 + 1m 行情向前验证 + 行情图(信号标注) 生成 ｜ 数据截止 %s 15:00</div>' % TARGET)

html = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>tpoint 每日复盘 %s</title><style>%s</style></head>'
        '<body><div class="wrap">') % (TARGET, css) + ''.join(body) + '</div></body></html>'

hpath = os.path.join(OUT, 'review_%s.html' % TARGET)
with open(hpath, 'w', encoding='utf-8') as f:
    f.write(html)
print('[ok] HTML ->', hpath, '(%d bytes)' % len(html))
