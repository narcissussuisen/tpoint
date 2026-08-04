#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_exec_html.py — 渲染 exec_compare_2026_07_21.json 为结构化复盘 HTML"""
import os, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
J = json.load(open(os.path.join(ROOT, 'output', 'exec_compare_2026_07_21.json'), encoding='utf-8'))
D = J['date']
SYMS = [('161129.SZ', '原油LOF易方达'), ('688347.SH', '华虹宏力')]


def pct(x, color=True, suf='%'):
    if x is None:
        return '—'
    s = f'{x:+.2f}{suf}' if x >= 0 else f'{x:.2f}{suf}'
    if not color:
        return s
    cls = 'up' if x >= 0 else 'down'   # 红涨绿跌
    return f'<span class="{cls}">{s}</span>'


def fwd(x):
    if x is None:
        return '<span class="muted">n/a</span>'
    cls = 'up' if x >= 0 else 'down'
    return f'<span class="{cls}">{x:+.2f}%</span>'


HTML = []
HTML.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>tpoint 交易执行复盘 + strict/floor 对比 · {D}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#0d1117;color:#c9d1d9;margin:0;padding:24px;line-height:1.6}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:24px;color:#f0f6fc;border-bottom:2px solid #30363d;padding-bottom:12px;margin-bottom:6px}}
h2{{font-size:19px;color:#58a6ff;margin-top:34px;border-left:4px solid #58a6ff;padding-left:10px}}
h3{{font-size:16px;color:#e3b341;margin-top:22px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:18px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 18px;margin:14px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}}
th,td{{border:1px solid #30363d;padding:6px 8px;text-align:center}}
th{{background:#21262d;color:#c9d1d9;font-weight:600}}
td.l{{text-align:left}}
.up{{color:#ff7b72}} .down{{color:#3fb950}} .muted{{color:#6e7681}}
.B{{color:#ff7b72;font-weight:700}} .S{{color:#3fb950;font-weight:700}}
.kv{{display:flex;flex-wrap:wrap;gap:8px 22px;margin:8px 0}}
.kv div{{font-size:13px}}
.kv b{{color:#f0f6fc}}
.note{{background:#1c2128;border-left:3px solid #e3b341;padding:10px 14px;margin:12px 0;font-size:13px;border-radius:0 6px 6px 0}}
.warn{{border-left-color:#ff7b72}}
.ok{{border-left-color:#3fb950}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:900px){{.grid2{{grid-template-columns:1fr}}}}
.tag{{display:inline-block;background:#21262d;border:1px solid #30363d;border-radius:4px;padding:1px 7px;font-size:11px;color:#8b949e;margin-left:6px}}
.pnl-win{{color:#3fb950}} .pnl-loss{{color:#ff7b72}}
footer{{margin-top:30px;color:#6e7681;font-size:12px;border-top:1px solid #30363d;padding-top:12px}}
.cmp-dim{{background:#161b22}}
</style></head><body><div class="wrap">
<h1>tpoint 交易执行复盘 · strict vs floor 算法对比</h1>
<div class="sub">交易日 <b>{D}（周二）</b> · 标的 <b>161129 原油LOF易方达</b> / <b>688347 华虹宏力</b> · 引擎 miji_alpha（1分钟K · 生产 EXIT_CFG=移动止损0.4/0.6，硬/时间止损关）<br>
数据：mootdx 通达信 TCP 7709（与生产同源）；算法重放喂当日<b>已收盘</b>240根1m棒，<b>无未来函数</b>。实时执行取自 <code>data/state.json</code>。</div>
""")

# ============ 0. 实时执行总览 ============
HTML.append('<h2>一、实时执行总览（生产 monitor 实际记录）</h2>')
live = J['live']
HTML.append('<div class="note warn"><b>关键事实：</b>今日实时系统因开盘数据源断流（复盘已定位的根因），两标的实际执行与"算法重放"严重不对称——')
HTML.append('<ul style="margin:6px 0">')
HTML.append(f'<li><b>161129</b>：state 无 <code>bar_161129</code> / 无 20260721 计数键 → <b>当日未被扫描，0 信号、0 成交</b>（非策略问题，是数据层问题）。</li>')
p688 = live['688347.SH']['position']
HTML.append(f'<li><b>688347</b>：记录 <b>1 笔买入 @ 335.5（09:33，idx2）</b>，仓位 size_pct=2%，<b>至今未平仓</b>；系统冻结 max_fav=341.5（+1.79%）。但当日该股涨停收 397.78，若持仓至收盘理论浮盈 <b>+18.5%</b>，系统视图因断流已滞后。</li>')
HTML.append('</ul>')
HTML.append('以下"算法重放"是对<b>当日完整 1m 数据</b>用两种门控重算的<b>假设性表现</b>，用于评估算法本身，不代表今日真实推送。</div>')

# ============ 逐标的 ============
for sym, label in SYMS:
    blk = J['symbols'][sym]
    o = blk['ohlc']
    lv = live[sym]
    st = blk['modes']['strict']
    fl = blk['modes']['floor']
    HTML.append(f'<h2>二、{sym} {label} 逐标的复盘</h2>')

    # 行情概要
    HTML.append('<div class="card"><div class="kv">')
    HTML.append(f'<div>开盘 <b>{o["open"]}</b></div><div>最高 <b class="up">{o["high"]}</b></div>'
                f'<div>最低 <b class="down">{o["low"]}</b></div><div>收盘 <b>{o["close"]}</b></div>')
    HTML.append(f'<div>涨跌幅 <span class="{"up" if o["day_chg_pct"]>=0 else "down"}">{o["day_chg_pct"]:+.2f}%</span></div>')
    HTML.append(f'<div>日内振幅 <b>{o["intraday_range_pct"]}%</b></div>')
    HTML.append(f'<div>成交/中位 <b>{o["vol_ratio"]}×</b></div>')
    HTML.append(f'<div>最大超卖偏离 <span class="down">{o["max_os_dev_pct"]}%</span></div>')
    HTML.append(f'<div>最大超买偏离 <span class="up">{o["max_ob_dev_pct"]}%</span></div>')
    HTML.append('</div></div>')

    # 实时成交
    HTML.append('<h3>2.1 实时成交执行（实际）</h3>')
    if lv['position']:
        p = lv['position']
        entry = p['entry_price']; ep = p['entry_idx']
        eod = o['close']
        unreal = (eod/entry-1)*100
        # 真实买入时间取自重放首笔 B 信号(idx 一致=entry_idx)
        buy_time = '09:33'
        for s in st['signals']:
            if s['dir'] == 'B' and s['idx'] == ep:
                buy_time = s['time'][11:16]; break
        HTML.append('<table><tr><th>时间</th><th>方向</th><th>成交价</th><th>仓位</th><th>触发原因</th><th>状态</th><th>盈亏</th></tr>')
        HTML.append(f'<tr><td>{buy_time}</td><td class="B">买入 B</td><td>{entry}</td>'
                    f'<td>size_pct={p["size_pct"]}%</td><td class="l">{p["entry_reason"]}</td>'
                    f'<td>持仓未平（系统冻结 max_fav={p["max_fav"]}）</td>'
                    f'<td>理论至收盘 <span class="up">+{unreal:.1f}%</span></td></tr>')
        HTML.append('</table>')
        HTML.append(f'<div class="note">实时仅 1 笔买入、0 笔卖出。该股当日涨停（+20%），移动止损在 09:36 附近本应触发 +1.0% 离场（算法重放已验证），'
                    f'但因盘中扫描断流，系统从未收到后续 bar，故<b>未触发任何出场</b>，持仓停留在 335.5。'
                    f'若用户实际持有，至收盘浮盈约 <b class="up">+{unreal:.1f}%</b>；系统记录的 max_fav 341.5 已严重滞后。</div>')
    else:
        HTML.append('<div class="note warn"><b>161129 当日 0 成交</b>：state 无扫描计数，开盘 09:31–09:35 monitor 日志反复 <code>no intraday data</code> / <code>NoneType ... klines</code>，分钟K盘中不可得 → 引擎从未被调用。无实时盈亏可言。</div>')

    # 算法重放信号表
    HTML.append('<h3>2.2 算法重放信号明细（strict 生产 vs floor 拟flip）</h3>')
    HTML.append('<div class="grid2">')
    for mode, mblk in (('strict', st), ('floor', fl)):
        HTML.append(f'<div class="card"><div style="color:#e3b341;font-weight:700;margin-bottom:6px">{mode.upper()} 模式 · {mblk["n_sig"]} 信号 (B{mblk["nB"]}/S{mblk["nS"]})</div>')
        HTML.append('<table><tr><th>时间</th><th>方向</th><th>价</th><th>因子</th><th>fwd@12m</th></tr>')
        for s in mblk['signals']:
            HTML.append(f'<tr><td>{s["time"][11:16]}</td><td class="{s["dir"]}">{s["dir"]}</td>'
                        f'<td>{s["price"]}</td><td class="l" style="font-size:11px">{s["detail"]}</td>'
                        f'<td>{fwd(s["fwd12"])}</td></tr>')
        HTML.append('</table></div>')
    HTML.append('</div>')

    # P&L 与准确率
    HTML.append('<h3>2.3 配对盈亏与准确率（生产出场配置）</h3>')
    HTML.append('<table><tr><th>指标</th><th>strict</th><th>floor</th><th>差异解读</th></tr>')
    HTML.append(f'<tr><td class="l">信号数 (B/S)</td><td>{st["n_sig"]} ({st["nB"]}/{st["nS"]})</td><td>{fl["n_sig"]} ({fl["nB"]}/{fl["nS"]})</td><td class="l">floor 多 {fl["n_sig"]-st["n_sig"]} 个</td></tr>')
    HTML.append(f'<tr><td class="l">准确率 B@12m</td><td>{pct(st["B_acc12"])}</td><td>{pct(fl["B_acc12"])}</td><td class="l">买点方向质量</td></tr>')
    HTML.append(f'<tr><td class="l">准确率 S@12m</td><td>{pct(st["S_acc12"])}</td><td>{pct(fl["S_acc12"])}</td><td class="l">卖点方向质量</td></tr>')
    HTML.append(f'<tr><td class="l">配对笔数</td><td>{st["pnl"]["total"]}</td><td>{fl["pnl"]["total"]}</td><td class="l">—</td></tr>')
    wr_st = st['pnl']['win_rate']; wr_fl = fl['pnl']['win_rate']
    HTML.append(f'<tr><td class="l">胜率</td><td>{wr_st}%</td><td>{wr_fl}%</td><td class="l">—</td></tr>')
    HTML.append(f'<tr><td class="l">总收益(名义)</td><td class="{"pnl-win" if st["pnl"]["total_ret"]>=0 else "pnl-loss"}">{pct(st["pnl"]["total_ret"])}</td>'
                f'<td class="{"pnl-win" if fl["pnl"]["total_ret"]>=0 else "pnl-loss"}">{pct(fl["pnl"]["total_ret"])}</td><td class="l">floor {"占优" if fl["pnl"]["total_ret"]>st["pnl"]["total_ret"] else "持平"}</td></tr>')
    HTML.append('</table>')

    # 本标的小结
    if sym == '161129.SZ':
        HTML.append('<div class="note ok"><b>161129 小结（下跌日 -4.36%，均值回归）：</b>'
                    f'floor 明显占优——多捕获 3 笔买点（11:02/11:26/14:51，均靠"价格地板"新低触发），'
                    f'买准确率 66.7% vs strict 的 0%（strict 仅有的 1 笔买在 14:35 买在平盘后继续跌）。'
                    f'配对收益 floor +0.99% vs strict -0.10%。卖点两模式均 100% 正确（下跌日卖在高位）。'
                    f'<b>strict 在此类低换手 LOF 上因缺少 MACD 背离而几乎哑火。</b></div>')
    else:
        HTML.append('<div class="note warn"><b>688347 小结（涨停日 +20%，强趋势）：</b>'
                    f'两模式<b>买点完全一致</b>（均仅 09:33 一笔 335.5，+1.0% 被移动止损离场），配对收益都是 +1.0%——<b>打平</b>。'
                    f'分歧全在卖点：floor 多出 4 笔卖（"价格天花板"每创新高即触发），但当日是涨停趋势，'
                    f'绝大多数卖点后价格继续涨（S准确率严格 37.5% / floor 41.7%，均偏低=卖飞）。'
                    f'两模式都没吃到 +18.5% 的涨停主升，因移动止损过早止盈——这是<b>出场规则</b>问题，非门控问题。</div>')

# ============ 横向对比 ============
HTML.append('<h2>三、strict vs floor 横向对比结论</h2>')

HTML.append('<h3>3.1 信号数量与时间分布</h3>')
HTML.append('<table><tr><th>标的</th><th>模式</th><th>信号数</th><th>买/卖</th><th>首信号</th><th>频率(个/时)</th></tr>')
for sym, label in SYMS:
    blk = J['symbols'][sym]
    for mode in ('strict', 'floor'):
        m = blk['modes'][mode]
        HTML.append(f'<tr><td class="l">{label}</td><td>{mode}</td><td>{m["n_sig"]}</td><td>{m["nB"]}/{m["nS"]}</td>'
                    f'<td>{m["first_time"][11:16]}</td><td>{m["freq_per_h"]}</td></tr>')
HTML.append('</table>')
HTML.append('<div class="note">floor 灵敏度更高：161129 频率 1.5/h vs 0.5/h（3×），688347 3.25/h vs 2.25/h（1.4×）。'
            '首信号时间两模式一致——floor 的优势在<b>后续更多的中段信号</b>，而非更早触发。</div>')

HTML.append('<h3>3.2 方向一致性 / 分歧点</h3>')
HTML.append('<div class="card"><div class="l">')
HTML.append('<p><b>161129（下跌日）：</b>两模式<b>共有</b>信号 = 10:31 卖 + 14:35 买（2 个）。'
            'strict 独有 = <b>无</b>；floor 独有 = 11:02 买、11:26 买、13:38 卖、14:51 买（4 个，均"价格地板/天花板"触发）。'
            '→ 分歧在 <b>买点</b>：strict 因缺 MACD 背离漏掉 3 笔下跌中的低吸机会。</p>')
HTML.append('<p><b>688347（涨停日）：</b>两模式<b>共有</b> = 09:33 买 + 09:41 卖（2 个）。'
            '严格看，两者<b>卖点机制不同</b>：strict 卖靠"MACD红柱缩短"（7 个独有），floor 卖靠"价格天花板/新高"（11 个独有），仅 09:41 重叠。'
            '→ 分歧在 <b>卖点</b>：floor 在趋势日每创新高就追一个卖，制造大量卖飞噪声。</p>')
HTML.append('</div></div>')

HTML.append('<h3>3.3 准确率（信号发出后 12 分钟实际走势是否符合预期）</h3>')
HTML.append('<table><tr><th>标的</th><th>模式</th><th>买准确率@12m</th><th>卖准确率@12m</th><th>解读</th></tr>')
for sym, label in SYMS:
    blk = J['symbols'][sym]
    for mode in ('strict', 'floor'):
        m = blk['modes'][mode]
        interp = ('买踩对/卖卖飞' if m['S_acc12'] < 50 else '卖点质量好') if mode == 'strict' else ('买点更丰富' if sym.startswith('161') else '卖点更多但噪声')
        HTML.append(f'<tr><td class="l">{label}</td><td>{mode}</td><td>{pct(m["B_acc12"])}</td><td>{pct(m["S_acc12"])}</td><td class="l">{interp}</td></tr>')
HTML.append('</table>')
HTML.append('<div class="note">买准确率：161129 上 floor(66.7%) ≫ strict(0%)；688347 上两模式均 100%（早盘低吸完美）。'
            '卖准确率：下跌日两模式均 100%；涨停日两模式均 <45%（趋势中卖点天然易错）。'
            '<b>floor 不提升卖点质量，反而因"价格天花板"在趋势日增加错误卖点。</b></div>')

HTML.append('<h3>3.4 灵敏度差异（触发先后 / 频率）</h3>')
HTML.append('<div class="note">floor = strict + 价格地板(B)/天花板(S) 附加通道。它不抢跑首信号，但在<b>价格创新低（买）/创新高（卖）</b>时额外放行，'
            '故中段信号更密。对<b>均值回归标的</b>（161129）这是加分（多抓低吸）；对<b>强趋势标的</b>（688347）这是减分（多抓卖飞）。</div>')

HTML.append('<h3>3.5 两算法优劣总结</h3>')
HTML.append('<div class="grid2">')
HTML.append('<div class="card"><div style="color:#58a6ff;font-weight:700">strict（生产默认）</div><p><b>优势</b></p><ul class="l">'
            '<li>信号干净、噪声少，趋势日卖飞更少（688347 卖点 8 vs floor 12）</li>'
            '<li>买点需 MACD 背离确认，假突破少</li>'
            '<li>OOS 历史回测证优（每信号净 T 稳定）</li></ul>'
            '<p><b>不足</b></p><ul class="l">'
            '<li>低换手/背离稀少标的（161129 LOF）几乎哑火：今日仅 2 信号、买准确率 0%</li>'
            '<li>下跌日中段低吸机会大量漏抓</li></ul></div>')
HTML.append('<div class="card"><div style="color:#e3b341;font-weight:700">floor（拟 flip）</div><p><b>优势</b></p><ul class="l">'
            '<li>灵敏度高，均值回归日多抓低吸（161129 多 3 买，收益 +0.99% vs -0.10%）</li>'
            '<li>不强制 MACD，绕开 md 因子反向预测陷阱</li>'
            '<li>沙箱效率最高（每信号净 T / 均净 T% 居首）</li></ul>'
            '<p><b>不足</b></p><ul class="l">'
            '<li>趋势/涨停日"价格天花板"狂刷卖信号 → 卖飞噪声（688347 多 4 卖，准确率仍 <45%）</li>'
            '<li>信号频次高，需更强过滤否则刷屏</li></ul></div>')
HTML.append('</div>')

HTML.append('<h3>3.6 横向结论</h3>')
HTML.append('<div class="note ok">'
            '<b>① 今日行情分两类，算法表现分化：</b>161129（下跌均值回归）floor 占优；688347（涨停强趋势）两模式买点打平、floor 卖点更噪。<br>'
            '<b>② 单日样本极小</b>（688347 两模式均仅 1 笔买），不可据一日推翻沙箱跨样本结论；floor 在沙箱 OOS 上净 T 更优是更可靠的信号。<br>'
            '<b>③ 两标的真实亏损/漏报均源于数据断流</b>，非算法——若实时数据正常，strict 今日本应给 161129 至少 2 信号、688347 完整买卖序列。<br>'
            '<b>④ 真正的共性短板是移动止损过早止盈</b>：688347 移动止损 09:36 离场 +1.0%，错过后续 +18.5% 涨停主升，strict/floor 都无法解决。<br>'
            '<b>⑤ 建议</b>：先落地已完成的"数据源韧性 + 静默告警"止血；门控 flip（strict→floor）作为独立决策，须先跑多日 OOS 验证，且对趋势标的加"价格天花板冷却/趋势过滤"抑制卖飞。</div>')

HTML.append(f'<footer>生成：scripts/exec_compare_0721.py（真实1m重放 + 生产EXIT_CFG配对）· 渲染：scripts/build_exec_html.py · '
            f'数据隔离、无未来函数、不构成投资建议。今日实时执行问题详见前次复盘报告 output/review_2026_07_21.html。</footer>')
HTML.append('</div></body></html>')

out = os.path.join(ROOT, 'output', 'exec_compare_2026_07_21.html')
with open(out, 'w', encoding='utf-8') as f:
    f.write(''.join(HTML))
print('HTML ->', out, os.path.getsize(out), 'bytes')
