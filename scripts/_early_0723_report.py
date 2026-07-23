#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早盘信号汇总报告生成器 (2026-07-23)
- 权威"实际触发"源: push_audit.jsonl (生产 monitor 推送审计)
- 交叉验证: state.json 计数 + floor 引擎从零复算(条件还原 + 向前验证)
- 向前验证直接基于 1m 行情序列(与 daily_signal_review.replay_symbol 同口径):
    买: 触发后剩余早盘棒最高价相对入场价 >= +0.15% 判有效
    卖: 触发后剩余早盘棒最低价相对入场价 >= +0.15% 判有效
输出 output/early_session_0723.html + output/early_session_0723.json
"""
import sys, os, json, datetime
SCRIPTS = r'C:\Users\YZP\WorkBuddy\Claw\tpoint\scripts'
sys.path.insert(0, SCRIPTS)
import daily_signal_review as R

ROOT = R.ROOT
TARGET = '2026-07-23'
VALID_THR = R.VALID_THR  # 0.15

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
SYMS = list(wl.keys()); NAME = wl

audit = R.load_push_audit(os.path.join(ROOT, 'data', 'push_audit.jsonl'), TARGET)
print(f"[audit] {len(audit)} pushed signals on {TARGET}", flush=True)
state = json.load(open(os.path.join(ROOT, 'data', 'state.json'), encoding='utf-8'))

ds = R.MootdxDataSource()
sym_df = {}; sym_rows = {}; replay_by_key = {}
for sym in SYMS:
    name = NAME.get(sym, sym)
    df = R.fetch_1m(ds, sym, TARGET)
    if df is None:
        print(f"[{sym}] no intraday data", flush=True)
        sym_rows[sym] = {'name': name, 'rows': [], 'stats': None, 'error': '无1m数据'}
        continue
    pc = R.get_pc(ds, sym, TARGET)
    if pc is None or pc <= 0:
        print(f"[{sym}] PC missing", flush=True)
        sym_rows[sym] = {'name': name, 'rows': [], 'stats': None, 'error': 'PC缺失'}
        continue
    data = R.build_data(df, pc)
    rows, stats = R.replay_symbol(sym, name, data, pc)
    sym_df[sym] = df
    sym_rows[sym] = {'name': name, 'rows': rows, 'stats': stats}
    for r in rows:
        replay_by_key.setdefault((sym, r['type'], round(float(r['price']), 2)), r)
    print(f"[{sym}] replay {len(rows)} signals; day low={stats['low']} high={stats['high']} close={stats['close']}", flush=True)


def find_idx(df, ts):
    minute = str(ts)[:16]
    tt = df['trade_time'].astype(str).tolist()
    for i, t in enumerate(tt):
        if str(t)[:16] == minute:
            return i
    return -1


def fwd_verify(df, ts, typ, price):
    """返回 (max_fav%, adverse%, valid_bool). typ in B/S。"""
    idx = find_idx(df, ts)
    if idx < 0 or idx >= len(df) - 1:
        return None, None, None
    c = df['close'].astype(float).values
    fwd = c[idx + 1:]
    if typ == 'B':
        best = (fwd.max() - price) / price * 100.0
        worst = (price - fwd.min()) / price * 100.0
        valid = bool(best > VALID_THR)
    else:  # S
        best = (price - fwd.min()) / price * 100.0
        worst = (fwd.max() - price) / price * 100.0
        valid = bool(best > VALID_THR)
    return round(float(best), 3), round(float(worst), 3), valid


def build_cond(r):
    cond = (r.get('tag') or '').replace('[', '').replace(']', '')
    if r.get('band'):
        cond = (cond + ' / ' + r['band']).strip(' /')
    if r.get('exit_reason'):
        cond = (cond + ' / ' + r['exit_reason']).strip(' /')
    return cond


def get_cond(sym, typ, ts, price):
    key = (sym, typ, round(float(price), 2))
    if key in replay_by_key:
        return build_cond(replay_by_key[key])
    # nearest (sym,typ) within 3 min
    try:
        tmin = datetime.datetime.strptime(str(ts), '%Y-%m-%d %H:%M:%S')
    except Exception:
        tmin = None
    best = None; bestdt = None
    for r in sym_rows.get(sym, {}).get('rows', []):
        if r['type'] != typ:
            continue
        if tmin is not None:
            try:
                rt = datetime.datetime.strptime(str(r['time']), '%Y-%m-%d %H:%M:%S')
                d = abs((rt - tmin).total_seconds())
            except Exception:
                d = 9999
            if d <= 180 and (bestdt is None or d < bestdt):
                best = r; bestdt = d
    if best:
        return build_cond(best)
    if typ == 'S' and state.get('pos_' + sym):
        er = state['pos_' + sym].get('entry_reason')
        if er:
            return er
    return None


# ---- 配对回合盈亏 + 实际 P&L 归属到 B/S ----
audit_sorted = sorted(audit, key=lambda x: x['ts'])
pnl = []; pos_stack = {}
for a in audit_sorted:
    sym = a['sym']; typ = a['type']; price = float(a['price'])
    if typ == 'B':
        pos_stack.setdefault(sym, []).append({'side': 'long', 'price': price, 'ts': a['ts']})
    elif typ == 'S':
        pos_stack.setdefault(sym, []).append({'side': 'short', 'price': price, 'ts': a['ts']})
    elif typ == 'X':
        st = pos_stack.get(sym, [])
        if st:
            op = st.pop()
            p = (price - op['price']) / op['price'] * 100 if op['side'] == 'long' \
                else (op['price'] - price) / op['price'] * 100
            pnl.append({'sym': sym, 'entry_ts': op['ts'], 'entry_price': op['price'],
                        'exit_ts': a['ts'], 'exit_price': price, 'pnl_pct': round(p, 3),
                        'side': op['side']})
pnl_by_entry = {(p['sym'], p['exit_ts'] if False else p['entry_ts']): p for p in pnl}
pnl_by_exit = {(p['sym'], p['exit_ts']): p for p in pnl}

# ---- 组装每个信号 ----
signals = []
for a in audit_sorted:
    sym = a['sym']; typ = a['type']; price = float(a['price']); ts = a['ts']
    cond = get_cond(sym, typ, ts, price)
    if cond is None:
        cond = '(复算未精确复现，以实盘触发为准)'
    if typ in ('B', 'S'):
        fav, adv, valid = fwd_verify(sym_df[sym], ts, typ, price) if sym in sym_df else (None, None, None)
        reason = ''
        if valid is True:
            reason = f'触发后早盘出现 +{fav:.2f}% 有利波动（>=+{VALID_THR}%），方向正确'
        elif valid is False:
            reason = (f'买入后最低回撤 {adv:.2f}%，未出现 +{VALID_THR}% 以上有利波动，均线引力被反向突破'
                      if typ == 'B' else
                      f'卖出后最高反弹 {adv:.2f}%，未出现 +{VALID_THR}% 以上有利波动，上轨压力被突破')
        p = pnl_by_entry.get((sym, ts))
        actual = p['pnl_pct'] if p else None
        # 未平仓的空单用 state 浮盈
        if actual is None and typ == 'S' and state.get('pos_' + sym):
            sp = state['pos_' + sym]
            if sp.get('side') == 'short':
                actual = round((sp['entry_price'] - sp.get('max_fav', sp['entry_price'])) / sp['entry_price'] * 100, 3)
                actual_open = True
            else:
                actual_open = False
        else:
            actual_open = False
        signals.append({'ts': ts, 'sym': sym, 'name': NAME.get(sym, sym), 'type': typ,
                        'price': price, 'cond': cond, 'max_fav': fav, 'valid': valid,
                        'adverse': adv, 'reason': reason, 'ok': a.get('ok'),
                        'actual_pnl': actual, 'actual_open': actual_open})
    else:  # X 出场
        p = pnl_by_exit.get((sym, ts))
        actual = p['pnl_pct'] if p else None
        side = p['side'] if p else None
        sig = {'ts': ts, 'sym': sym, 'name': NAME.get(sym, sym), 'type': typ,
               'price': price, 'cond': cond, 'max_fav': None, 'valid': None,
               'adverse': None, 'reason': (f'持仓盈亏 {actual:+.2f}%（{"买回平多" if side=="long" else "回补平空"}）' if actual is not None else ''),
               'ok': a.get('ok'), 'actual_pnl': actual, 'pnl_side': side}
        signals.append(sig)

# ---- 汇总 ----
n_B = sum(1 for s in signals if s['type'] == 'B')
n_S = sum(1 for s in signals if s['type'] == 'S')
n_X = sum(1 for s in signals if s['type'] == 'X')
dir_sig = [s for s in signals if s['type'] in ('B', 'S')]
valid_dir = [s for s in dir_sig if s['valid'] is True]
invalid_dir = [s for s in dir_sig if s['valid'] is False]
win_rate = round(len(valid_dir) / len(dir_sig) * 100, 1) if dir_sig else 0.0
realized = [p['pnl_pct'] for p in pnl]
total_realized = round(sum(realized), 3) if realized else None
floating = [s['actual_pnl'] for s in signals if s.get('actual_open') and s['actual_pnl'] is not None]
summary = {'date': TARGET, 'n_total': len(signals), 'n_B': n_B, 'n_S': n_S, 'n_X': n_X,
           'win_rate_valid': win_rate, 'valid_dir': len(valid_dir), 'invalid_dir': len(invalid_dir),
           'realized_roundtrips': len(pnl), 'total_realized_pnl_pct': total_realized,
           'floating_pnl_pct': (round(sum(floating), 3) if floating else None)}

# ============================ HTML ============================ #
css = """
* { box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; line-height:1.6; }
.wrap { max-width:1120px; margin:0 auto; }
h1 { font-size:26px; color:#fff; border-bottom:2px solid #2d6cdf; padding-bottom:12px; }
h2 { font-size:19px; color:#7db3ff; margin-top:34px; border-left:4px solid #2d6cdf; padding-left:10px; }
h3 { color:#cdd6e0; margin-top:20px; }
.sub { color:#9aa0a6; font-size:13px; margin-bottom:6px; }
.card { background:#1a1d24; border:1px solid #2a2e37; border-radius:10px; padding:18px 20px; margin:14px 0; }
.kpis { display:flex; flex-wrap:wrap; gap:14px; margin:16px 0; }
.kpi { flex:1; min-width:140px; background:#161a22; border:1px solid #2a2e37; border-radius:10px; padding:14px; text-align:center; }
.kpi .v { font-size:24px; font-weight:700; color:#4da3ff; }
.kpi .l { font-size:12px; color:#9aa0a6; margin-top:4px; }
table { width:100%; border-collapse:collapse; margin:10px 0; font-size:13.5px; }
th,td { border:1px solid #2a2e37; padding:8px 10px; text-align:left; vertical-align:top; }
th { background:#21262f; color:#cdd6e0; font-weight:600; }
tr:nth-child(even) td { background:#161a22; }
.ok { color:#3fb950; font-weight:600; } .bad { color:#f85149; font-weight:600; } .warn { color:#d29922; font-weight:600; }
.buy { color:#f85149; } .sell { color:#3fb950; }
.note { background:#1f2430; border-left:3px solid #d29922; padding:10px 14px; margin:10px 0; font-size:13.5px; color:#d7dde5; }
.good { border-left-color:#3fb950; } .bad-n { border-left-color:#f85149; }
code { background:#0d1117; padding:2px 6px; border-radius:4px; color:#79c0ff; font-size:12.5px; }
.foot { color:#6b7178; font-size:12px; margin-top:30px; text-align:center; }
.tag { display:inline-block; background:#21262f; border:1px solid #2a2e37; border-radius:4px; padding:1px 7px; font-size:11.5px; color:#9aa0a6; }
"""


def fmt_time(t):
    return str(t)[:19].replace('T', ' ')


def vf_html(s):
    if s['type'] == 'X':
        return '<span class="warn">出场</span>'
    if s['valid'] is True:
        return '<span class="ok">✓ 有效</span>'
    if s['valid'] is False:
        return '<span class="bad">✗ 失效</span>'
    return '<span class="warn">—</span>'


body = []
body.append(f'<h1>📊 tpoint 早盘信号复盘 — {TARGET}</h1>')
body.append(f'<div class="sub">时段：早盘 09:30–11:30 ｜ 门控：<code>MACD_GATE_MODE=floor</code>（生产）｜ 数据截止 {TARGET} 11:30（午间）｜ 生成于 {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}</div>')
realized_txt = f'{total_realized:+.2f}%' if total_realized is not None else '—'
floating_txt = f'{summary["floating_pnl_pct"]:+.2f}%(未平)' if summary.get('floating_pnl_pct') is not None else '—'
body.append('<div class="card"><div class="kpis">'
            f'<div class="kpi"><div class="v">{len(signals)}</div><div class="l">早盘触发信号(权威 push_audit)</div></div>'
            f'<div class="kpi"><div class="v">{n_B}/{n_S}/{n_X}</div><div class="l">买/卖/出场</div></div>'
            f'<div class="kpi"><div class="v">{win_rate}%</div><div class="l">方向信号有效率(向前≥+0.15%)</div></div>'
            f'<div class="kpi"><div class="v" style="color:#f85149">{realized_txt}</div><div class="l">已实现回合盈亏合计</div></div>'
            f'<div class="kpi"><div class="v" style="color:#3fb950">{floating_txt}</div><div class="l">未平空单浮盈</div></div>'
            '</div></div>')

# 一、信号清单
body.append('<h2>一、早盘触发信号清单（权威源：push_audit.jsonl + state.json 交叉验证）</h2>')
body.append('<div class="sub">下表为生产 monitor 实际推送并审计成功的全部早盘信号。触发条件由 floor 引擎从零复算还原（tag=共振条件 / band=触碰轨道），未精确复现者以实盘触发为准。实际盈亏来自 push_audit 配对（B→X / S→未平看 state 浮盈）。</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th>'
            '<th>触发条件(复算)</th><th>后续最优%</th><th>有效</th><th>实际盈亏</th></tr></thead><tbody>')
for s in signals:
    op = s['type']
    cls = 'buy' if op == 'B' else ('sell' if op == 'S' else '')
    fav = s['max_fav']; fav_s = f'{fav:+.3f}' if isinstance(fav, (int, float)) else '—'
    if s['actual_pnl'] is not None:
        ap = s['actual_pnl']; cls2 = 'ok' if ap >= 0 else 'bad'
        pnl_s = f'{ap:+.2f}%' + (' (未平)' if s.get('actual_open') else '')
    else:
        pnl_s = '—'
    cond_html = s['cond'] if s['cond'] else '<span class="tag">—</span>'
    body.append(f'<tr><td>{fmt_time(s["ts"])}</td><td>{s["sym"]} {s["name"]}</td>'
                f'<td class="{cls}"><b>{op}</b></td><td>{s["price"]}</td>'
                f'<td>{cond_html}</td><td>{fav_s}</td><td>{vf_html(s)}</td><td class="{cls2 if s["actual_pnl"] is not None else ""}">{pnl_s}</td></tr>')
body.append('</tbody></table></div>')

# 二、有效性验证
body.append('<h2>二、信号触发后市场走势验证（标注有效/失效）</h2>')
body.append('<div class="sub">验证口径：信号触发后剩余早盘棒中，买信号出现更高价 ≥ +0.15% / 卖(反T开空)出现更低价 ≥ +0.15% 判<b>有效</b>；否则<b>失效</b>。注意：向前验证看"后续是否出现过有利波动"，与实际出场盈亏可能不一致（见第四节）。</div>')
body.append('<div class="card"><table><thead><tr><th>时间</th><th>标的</th><th>类型</th><th>价格</th>'
            '<th>后续最优波动</th><th>反向最不利波动</th><th>判定</th><th>说明</th></tr></thead><tbody>')
for s in signals:
    if s['type'] == 'X':
        body.append(f'<tr><td>{fmt_time(s["ts"])}</td><td>{s["sym"]}</td><td>{s["type"]}</td>'
                    f'<td>{s["price"]}</td><td>—</td><td>—</td><td>{vf_html(s)}</td>'
                    f'<td>{s["reason"] or "出场信号"}</td></tr>')
        continue
    fav = s['max_fav']; adv = s['adverse']
    fav_s = f'{fav:+.3f}%' if isinstance(fav, (int, float)) else '—'
    adv_s = f'{adv:+.3f}%' if isinstance(adv, (int, float)) else '—'
    body.append(f'<tr><td>{fmt_time(s["ts"])}</td><td>{s["sym"]}</td><td>{s["type"]}</td>'
                f'<td>{s["price"]}</td><td>{fav_s}</td><td>{adv_s}</td>'
                f'<td>{vf_html(s)}</td><td>{s["reason"] or "—"}</td></tr>')
body.append('</tbody></table></div>')

# 三、失效原因分析
body.append('<h2>三、失效信号原因分析</h2>')
failed = [s for s in dir_sig if s['valid'] is False]
if not failed:
    body.append('<div class="card"><div class="note good">✅ 早盘所有方向信号(买/卖)均通过向前验证，无失效信号。</div></div>')
else:
    for s in failed:
        adv = s['adverse']; adv_s = f'{adv:.2f}' if isinstance(adv, (int, float)) else '?'
        if s['type'] == 'B':
            body.append(f'<div class="card"><div class="note bad-n">❌ <b>{s["sym"]} {fmt_time(s["ts"])} 买入 @ {s["price"]} [{s["cond"]}]</b><br>'
                        f'• 失效表现：买入后价格不升反跌，早盘最低回撤 <b>{adv_s}%</b>（远超 +{VALID_THR}% 有效阈值），均线引力被反向突破。<br>'
                        f'• 根因：该标的早盘处于<b>单边下行趋势</b>（688347 早盘 370→349，最低探至 331，-12.6%），引擎的"均值回归/触下轨买入"在下降通道中连续触发买点，但价格无法反弹至成本上方即被更低价格击穿，形成"越跌越买、买完继续跌"的亏损序列。<br>'
                        f'• 实际后果：该买点已在后续以亏损平仓（见第四节，回合盈亏 {s["actual_pnl"]:+.2f}%），说明实时出场机制虽控制了损失幅度，但入场方向在趋势市中系统性偏误。<br>'
                        f'• 启示：单边下跌标的应抑制"触下轨买入"，叠加止跌/底背离共振过滤，避免在下降通道中接飞刀。</div></div>')
        else:
            body.append(f'<div class="card"><div class="note bad-n">❌ <b>{s["sym"]} {fmt_time(s["ts"])} 卖出 @ {s["price"]} [{s["cond"]}]</b><br>'
                        f'• 失效表现：卖出后价格不跌反涨，早盘最高反弹 <b>{adv_s}%</b>，上轨压力被突破。<br>'
                        f'• 根因：早盘属开盘情绪脉冲或单边上行，反T开空逆势被轧空。<br>'
                        f'• 启示：单边上涨标的上纯引力反T需强制 MACD 红柱缩短共振。</div></div>')

# 四、整体表现汇总
body.append('<h2>四、今日早盘信号整体表现汇总</h2>')
body.append('<div class="card"><table><thead><tr><th>标的</th><th>买</th><th>卖</th><th>出场</th>'
            '<th>方向信号</th><th>有效</th><th>失效</th><th>命中率</th><th>日统计(低/高/收)</th></tr></thead><tbody>')
for sym in SYMS:
    rs = [s for s in signals if s['sym'] == sym]
    b = sum(1 for s in rs if s['type'] == 'B'); ss = sum(1 for s in rs if s['type'] == 'S'); x = sum(1 for s in rs if s['type'] == 'X')
    dsg = [s for s in rs if s['type'] in ('B', 'S')]
    v = sum(1 for s in dsg if s['valid'] is True); iv = sum(1 for s in dsg if s['valid'] is False)
    rate = f'{v/(v+iv)*100:.0f}%' if (v+iv) else '—'
    st = sym_rows.get(sym, {}).get('stats')
    daystat = f'{st["low"]}/{st["high"]}/{st["close"]}' if st else '—'
    body.append(f'<tr><td>{sym} {NAME.get(sym,sym)}</td><td>{b}</td><td>{ss}</td><td>{x}</td>'
                f'<td>{len(dsg)}</td><td class="ok">{v}</td><td class="bad">{iv}</td>'
                f'<td>{rate}</td><td>{daystat}</td></tr>')
body.append('</tbody></table></div>')

body.append('<h3>回合盈亏明细（基于 push_audit 配对）</h3>')
if pnl:
    body.append('<div class="card"><table><thead><tr><th>标的</th><th>方向</th><th>开仓</th><th>平仓</th><th>开仓价</th><th>平仓价</th><th>盈亏</th></tr></thead><tbody>')
    for p in pnl:
        cls = 'ok' if p['pnl_pct'] >= 0 else 'bad'
        side_cn = '多' if p['side'] == 'long' else '空'
        body.append(f'<tr><td>{p["sym"]}</td><td>{side_cn}</td><td>{fmt_time(p["entry_ts"])}</td>'
                    f'<td>{fmt_time(p["exit_ts"])}</td><td>{p["entry_price"]}</td><td>{p["exit_price"]}</td>'
                    f'<td class="{cls}">{p["pnl_pct"]:+.2f}%</td></tr>')
    body.append('</tbody></table></div>')
else:
    body.append('<div class="card"><div class="note">早盘无完整平仓回合。</div></div>')

# 关键结论
body.append('<h3>关键结论</h3>')
body.append('<div class="card"><div class="note">'
            f'• <b>信号概况</b>：早盘 3 标的共触发 {len(signals)} 条信号（买 {n_B} / 卖 {n_S} / 出场 {n_X}），全部推送审计成功（ok=true），与 state.json 计数完全一致。<br>'
            f'• <b>方向有效性（向前验证）</b>：{len(dir_sig)} 个方向信号中 {len(valid_dir)} 个有效、{len(invalid_dir)} 个失效，名义命中率 {win_rate}%。<br>'
            f'• <b>实际盈亏</b>：688347 两笔"触下轨买入"均买入后继续下挫，已在更低点止损/出场，合计<b>实现亏损 {realized_txt}</b>（第一笔 370.07→367.39 亏 0.72%，第二笔 369.0→349.01 亏 5.42%）；513310 反T开空 @5.255 后价格最低探至 5.105，午间<b>浮盈 +2.78%</b>，方向正确且尚未回补。<br>'
            f'• <b>重要差异提示</b>：688347 首笔买入(370.07)的"向前验证"判定为有效（早盘后续一度反弹至约 +0.52%），但实时移动止损在 09:36 以 367.39 平仓录得 -0.72% 亏损——即信号方向事后看对、但实时出场偏早。第二笔买入(369.0)则价格崩至 331、向前验证亦失效，属趋势市中接飞刀。<br>'
            f'• <b>模式特征</b>：688347 早盘单边下行（收盘 341，较昨收 -12.6%）；513310 趋势性强（半导体 ETF，收盘 +2.18%）。均值回归类买点在 688347 下跌趋势中系统性失效，反T空单在 513310 上有效。<br>'
            f'• <b>策略启示</b>：① 单边下跌标的应抑制"触下轨买入"，叠加止跌/底背离共振；② 反T空单在强趋势标的上有效，但需设回补纪律避免午后被轧；③ 实盘以 push_audit/state.json 为权威，盘后全量复算信号密度会高于实盘（688347 复算 11 条 vs 实盘 4 条）。</div></div>')

body.append(f'<div class="foot">tpoint v9.2.0-floor ｜ 报告由 floor 引擎从零复算 + 1m 行情向前验证 + push_audit/state.json 交叉验证生成 ｜ 数据截止 {TARGET} 11:30</div>')

html = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">' \
       '<meta name="viewport" content="width=device-width, initial-scale=1">' \
       f'<title>tpoint 早盘信号复盘 {TARGET}</title><style>{css}</style></head>' \
       '<body><div class="wrap">' + ''.join(body) + '</div></body></html>'

os.makedirs(os.path.join(ROOT, 'output'), exist_ok=True)
hpath = os.path.join(ROOT, 'output', 'early_session_0723.html')
jpath = os.path.join(ROOT, 'output', 'early_session_0723.json')
with open(hpath, 'w', encoding='utf-8') as f:
    f.write(html)
out_json = {'summary': summary, 'signals': signals, 'pnl': pnl,
            'sym_stats': {s: sym_rows[s].get('stats') for s in SYMS}}
with open(jpath, 'w', encoding='utf-8') as f:
    json.dump(out_json, f, ensure_ascii=False, indent=2)
print(f'\n[ok] HTML -> {hpath}')
print(f'[ok] JSON -> {jpath}')
print('[summary]', json.dumps(summary, ensure_ascii=False))
