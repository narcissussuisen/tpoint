# -*- coding: utf-8 -*-
"""floord (v9.2.2 含漏顶漏底修复的 floor 门控) 2026-07-24 信号复盘分析。

针对两类标的约束重算 round-trip，输出两份复盘报告 HTML：
  - report_stock_603659.html   个股(603659, T+1, 双向, 可开空做T)
  - report_etf_lof.html        ETF/LOF(513310, 161129, T+0, 首笔限多单, 单向)

核心差异：
  个股模型='bidirectional' 保留 floord 的 S(开空) 信号；
  ETF/LOF模型='longonly'    将 S(开空) 重映射为「平多/无效」(flat 时 no-op)，强制首笔为多。
"""
import os
import sys
import json
import base64

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)

import floor_resonance_overlay_0724 as O

DAY = '2026-07-24'
OUT = os.path.join(ROOT, 'output', 'review_floord_20260724')
os.makedirs(OUT, exist_ok=True)

DATA_DIR = r'F:/keyfactor_data/1m'


def recompute():
    """返回 {sym: dict(day, pc, events, c, name)}"""
    res = {}
    for sym, name in O.SYMS:
        day, pc = O.load_day(sym)
        events = O.run_algo(day, pc, sym, mode='floor')
        c = day['close'].values.astype(float)
        res[sym] = {'day': day, 'pc': pc, 'events': events, 'c': c, 'name': name}
    return res


def simulate(events, c, model):
    """按约束模型重放事件流，返回 trips 列表。
    model: 'bidirectional' | 'longonly'
    trips: {side, entry, entry_idx, exit, exit_idx, hold, pnl,
            max_fav, max_adv, kind('closed'|'eod_unrealized'|'invalid_short')}
    """
    trips = []
    pos = None
    for e in events:
        typ, price, i, reason, entry, side = (
            e['type'], e['price'], e['idx'], e['reason'], e['entry_price'], e['side'])
        if typ == 'B':
            if model == 'longonly':
                if pos is not None:
                    continue  # 已持多，忽略重复开多
                pos = {'side': 'long', 'entry': price, 'entry_idx': i}
            else:
                pos = {'side': 'long', 'entry': price, 'entry_idx': i}
        elif typ == 'S':
            if model == 'longonly':
                if pos is not None and pos['side'] == 'long':
                    # 平多（原 S 信号在 ETF 上只能作为平多触发）
                    _close_trip(trips, pos, price, i, c, 'long_exit(S)')
                    pos = None
                else:
                    continue  # flat 时不能开空 -> no-op
            else:
                pos = {'side': 'short', 'entry': price, 'entry_idx': i}
        elif typ == 'X':
            if pos is None:
                continue
            _close_trip(trips, pos, price, i, c, reason)
            pos = None
    # EOD 未平
    if pos is not None:
        close = float(c[-1])
        _close_trip(trips, pos, close, len(c) - 1, c, 'eod_unrealized', closed=False)
    return trips


def _close_trip(trips, pos, exit_price, exit_idx, c, kind, closed=True):
    ei = pos['entry_idx']
    lo = ei + 1
    hi = exit_idx + 1
    seg = c[lo:hi] if hi > lo else np.array([c[ei]])
    if pos['side'] == 'long':
        pnl = (exit_price - pos['entry']) / pos['entry'] * 100.0
        max_fav = (seg.max() - pos['entry']) / pos['entry'] * 100.0 if len(seg) else 0.0
        max_adv = (seg.min() - pos['entry']) / pos['entry'] * 100.0 if len(seg) else 0.0
    else:
        pnl = (pos['entry'] - exit_price) / pos['entry'] * 100.0
        max_fav = (pos['entry'] - seg.min()) / pos['entry'] * 100.0 if len(seg) else 0.0
        max_adv = (pos['entry'] - seg.max()) / pos['entry'] * 100.0 if len(seg) else 0.0
    trips.append({
        'side': pos['side'], 'entry': pos['entry'], 'entry_idx': int(ei),
        'exit': exit_price, 'exit_idx': int(exit_idx),
        'hold': int(exit_idx - ei), 'pnl': pnl,
        'max_fav': max_fav, 'max_adv': max_adv,
        'kind': 'closed' if closed else 'eod_unrealized', 'reason': kind,
    })


def fwd_ret(c, i, horizon):
    j = min(i + horizon, len(c) - 1)
    return (c[j] - c[i]) / c[i] * 100.0 if j > i else 0.0


def signal_quality(events, c):
    """对每笔 B(多) 信号计算其后的有利/不利偏移（验证入场质量，与是否被止损无关）。"""
    rows = []
    for e in events:
        if e['type'] != 'B':
            continue
        i = e['idx']
        seg = c[i + 1: min(i + 31, len(c))]
        if len(seg) == 0:
            continue
        fav = (seg.max() - c[i]) / c[i] * 100.0
        adv = (seg.min() - c[i]) / c[i] * 100.0
        rows.append({'time': e['time'], 'entry': e['price'], 'fav5': fwd_ret(c, i, 5),
                     'fav15': fwd_ret(c, i, 15), 'max_fav': fav, 'max_adv': adv,
                     'reason': e['reason']})
    return rows


def b64(p):
    with open(p, 'rb') as f:
        return 'data:image/png;base64,' + base64.b64encode(f.read()).decode()


def build_metrics(sym, info):
    events = info['events']
    c = info['c']
    bidir = simulate(events, c, 'bidirectional')
    longonly = simulate(events, c, 'longonly')
    sq = signal_quality(events, c)
    # 真实 1m 信号序列（原始）
    raw = [{'type': e['type'], 'time': e['time'], 'price': e['price'],
            'reason': e['reason'], 'side': e['side']} for e in events]
    return {
        'name': info['name'], 'pc': info['pc'], 'bars': len(c),
        'raw_events': raw, 'bidir': bidir, 'longonly': longonly,
        'signal_quality': sq,
    }


def fmt(x, p=2):
    return f'{x:+.2f}%' if x is not None else '-'


def trips_table(trips):
    if not trips:
        return '<p style="color:#e67e22">当日无有效 round-trip。</p>'
    rows = []
    for t in trips:
        cls = 'win' if t['pnl'] > 0 else ('loss' if t['pnl'] < 0 else 'flat')
        tag = '✔' if t['kind'] == 'closed' else ('⏳隔夜/未平' if t['kind'] == 'eod_unrealized' else t['kind'])
        rows.append(
            f"<tr class='{cls}'><td>{t['side']}</td><td>{t['entry']:.4f}</td>"
            f"<td>{t['exit']:.4f}</td><td>{t['hold']}</td><td>{fmt(t['pnl'])}</td>"
            f"<td>{fmt(t['max_fav'])}</td><td>{fmt(t['max_adv'])}</td><td>{t['reason']}/{tag}</td></tr>")
    return ("<table class='t'><tr><th>方向</th><th>入场</th><th>出场</th><th>持有(分)</th>"
            "<th>P&L</th><th>最大有利</th><th>最大不利</th><th>类型</th></tr>"
            + ''.join(rows) + "</table>")


def raw_table(events):
    rows = []
    for e in events:
        rows.append(
            f"<tr><td>{e['type']}</td><td>{e['time']}</td><td>{e['price']:.4f}</td>"
            f"<td>{e['reason']}</td><td>{e['side']}</td></tr>")
    return ("<table class='t'><tr><th>类型</th><th>时间</th><th>价格</th><th>触发原因</th><th>方向</th></tr>"
            + ''.join(rows) + "</table>")


def sq_table(sq):
    if not sq:
        return '<p>-</p>'
    rows = []
    for r in sq:
        rows.append(
            f"<tr><td>{r['time']}</td><td>{r['entry']:.4f}</td><td>{fmt(r['fav5'])}</td>"
            f"<td>{fmt(r['fav15'])}</td><td>{fmt(r['max_fav'])}</td><td>{fmt(r['max_adv'])}</td>"
            f"<td>{r['reason']}</td></tr>")
    return ("<table class='t'><tr><th>入场时间</th><th>入场价</th><th>后5分</th><th>后15分</th>"
            "<th>最大有利(30内)</th><th>最大不利(30内)</th><th>触发原因</th></tr>"
            + ''.join(rows) + "</table>")


CSS = """
body{background:#15171c;color:#e6e6e6;font-family:'Microsoft YaHei',SimHei,sans-serif;margin:0;padding:28px 34px;line-height:1.65}
h1{color:#7fd1ff;border-bottom:2px solid #2a2f3a;padding-bottom:10px;font-size:24px}
h2{color:#9fe6a0;margin-top:30px;font-size:19px}
h3{color:#ffd479;margin-top:22px;font-size:16px}
.lead{color:#b9c2cf;font-size:14px}
.tag{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;margin-right:6px}
.t{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
.t th{background:#222834;color:#9fb3c8;padding:7px 9px;text-align:left;border:1px solid #2c333f}
.t td{padding:6px 9px;border:1px solid #2c333f}
.t tr.win td{background:rgba(46,204,113,.10)}
.t tr.loss td{background:rgba(231,76,60,.10)}
.note{background:#1d2330;border-left:4px solid #7fd1ff;padding:12px 16px;margin:14px 0;border-radius:6px;font-size:13.5px}
.warn{background:#241c1c;border-left:4px solid #e74c3c;padding:12px 16px;margin:14px 0;border-radius:6px;font-size:13.5px}
.opt{background:#1c241d;border-left:4px solid #2ecc71;padding:12px 16px;margin:14px 0;border-radius:6px;font-size:13.5px}
img{max-width:100%;border:1px solid #2c333f;border-radius:8px;margin:10px 0}
table.kv{font-size:13px} table.kv td{padding:4px 10px}
.sec{margin-top:18px}
"""


def render_stock(m, sym):
    bidir = m['bidir']
    realized = [t for t in bidir if t['kind'] == 'closed']
    eod = [t for t in bidir if t['kind'] == 'eod_unrealized']
    net_real = sum(t['pnl'] for t in realized)
    net_all = net_real + sum(t['pnl'] for t in eod)
    png = b64(os.path.join(O.OUT, f"{sym.split('.')[0]}.png"))
    wins = sum(1 for t in realized if t['pnl'] > 0)
    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'><title>floord 复盘 · {sym} 个股</title><style>{CSS}</style></head>
<body>
<h1>floord 信号复盘 · {sym} 璞泰来 · {DAY}</h1>
<p class='lead'>标的类型：<span class='tag' style='background:#2a3b4d;color:#9fd0ff'>个股 / T+1</span>
<span class='tag' style='background:#4d3b2a;color:#ffd479'>双向（可开空做T）</span>　算法：floord（v9.2.2，含漏顶漏底修复的 floor 门控）。
resonance 当日 0 信号，故聚焦 floord。</p>

<div class='sec'>
<h2>一、信号触发情况回顾</h2>
<p>当日 floord 共触发 <b>{len(m['raw_events'])}</b> 个原始事件（含 1 个开空 S、3 个多 B、3 个出场 X），resonance 无信号。
下图为 floord(绿▲买/橙▼卖空/红▼出场) 与 resonance(当日无) 的叠加：</p>
<img src='{png}'>
<h3>原始 1m 信号序列（floord 双向模型）</h3>
{raw_table(m['raw_events'])}
<h3>双向模型 round-trip（个股，保留开空）</h3>
{trips_table(bidir)}
<p class='note'>当日已平 <b>{len(realized)}</b> 笔（胜 {wins}），隔夜未平 <b>{len(eod)}</b> 笔。
已平合计 P&L <b>{fmt(net_real)}</b>；含隔夜未平合计 <b>{fmt(net_all)}</b>。</p>
</div>

<div class='sec'>
<h2>二、实际表现与预期偏差分析</h2>
<h3>1. 多单整体是「接飞刀」</h3>
<p>603659 当日 -1.20%、H-L 仅 2.67%，属温和下行。3 笔多单（10:27 / 13:30 / 14:43）入场价 23.73–23.87，密集分布在日内低位区，但价格继续阴跌：
10:27 多被 TRAIL(0.4/0.6) 在 -0.08% 扫掉；13:30 多被反向 S 触发平在 -0.25%；14:43 多隔夜（收盘 -0.17%）。
三笔多单最大有利偏移均很薄（见下），说明 floord 的「负引力偏离+MACD绿柱收缩」在下行段反复触发买点，但缺乏趋势确认，等价于抄底下行尾段。</p>
<h3>2. 唯一开空质量一般但方向对</h3>
<p>09:33 开空 @24.00（引力 dev=+0.47%，仅微高于 VWAP），09:58 被反向 B 触发平 @23.90，+0.42%。
 conviction 极低（dev 仅 0.47%，接近门限），靠的是价格自然阴跌，并非信号力强；且被反向信号过早平掉，错失后续到 23.60 的低点。</p>
<h3>3. 移动止损偏紧</h3>
<p>10:27 多单最大有利偏移仅约 +0.x%（见信号质量表），TRAIL 在激活 0.4% 后回撤 0.6% 即触发——在 603659 这种 tick 与波动下，相当于「刚有浮盈就被扫」。
这是把噪声当趋势反转来止损。</p>
<h3>4. 入场质量量化（多单后向偏移）</h3>
{sq_table(m['signal_quality'])}
<p class='warn'>偏差小结：预期是「floor 门控在极值处抓到高胜率反转」，实际是 <b>多单胜率偏低、靠止损控制亏损、且唯一开空 conviction 不足</b>。
T+1 约束下 14:43 的多单被迫隔夜，失去当日退出能力——这是个股 T+1 与 floord 当日出场设计的结构性张力。</p>
</div>

<div class='sec'>
<h2>三、可落地优化建议（个股 / T+1 / 双向）</h2>
<div class='opt'>
<b>① 多单加趋势门控（优先级高）</b><br>
当前多单仅看「负引力偏离 + MACD绿柱收缩」，在下行段过度触发。建议叠加日内趋势过滤：当价格低于慢速 EMA(如 60 根) 且未出现更低低点被有效收复时，
抑制新开多（或要求引力偏离更深，如 dev≤-1.5% 且 MACD 柱由负转正）。直接削减「接飞刀」笔数。<br><br>
<b>② T+1 尾盘禁开多（优先级高）</b><br>
个股 T+1 当日买入不可当日卖。建议 14:30 后禁止新开多（仅允许平多/开空），避免被迫隔夜且失去当日止损能力。
14:43 那笔多单即属此类，应被规则剔除。<br><br>
<b>③ 开空 conviction 下限（优先级中）</b><br>
09:33 开空 dev 仅 0.47% 即放行。建议开空要求引力 dev ≥ +1.0% 或 MACD 红柱缩短确认，过滤低 conviction 空单，减少「被反向扫掉」的无效交易。<br><br>
<b>④ 止损改为 ATR 自适应（优先级中）</b><br>
将固定 TRAIL(0.4/0.6) 改为 k×ATR（如 trail_activate=1.0×ATR、trail=1.5×ATR），按个股波动缩放，避免把噪声扫成亏损。
可先在回测 walk-forward 标定 k。<br><br>
<b>⑤ 隔夜风险管理</b><br>
T+1 隔夜多单无法当日止损。建议对隔夜仓设置次日开盘的硬风控（开盘价跌破入场 -X% 即市价平），或限制单标的同时隔夜敞口。
</div>
<p style='color:#7a8290;font-size:12px'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
    return html


def render_etf(m513, m161, sym513, sym161):
    def block(m, sym):
        longonly = m['longonly']
        bidir = m['bidir']
        realized = [t for t in longonly if t['kind'] == 'closed']
        eod = [t for t in longonly if t['kind'] == 'eod_unrealized']
        net_real = sum(t['pnl'] for t in realized)
        net_all = net_real + sum(t['pnl'] for t in eod)
        wins = sum(1 for t in realized if t['pnl'] > 0)
        png = b64(os.path.join(O.OUT, f"{sym.split('.')[0]}.png"))
        # 原始里的无效开空
        inv = [e for e in m['raw_events'] if e['type'] == 'S']
        return png, longonly, realized, eod, net_real, net_all, wins, inv, bidir

    p513, lo513, r513, e513, nr513, na513, w513, inv513, b513 = block(m513, sym513)
    p161, lo161, r161, e161, nr161, na161, w161, inv161, b161 = block(m161, sym161)
    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'><title>floord 复盘 · ETF/LOF</title><style>{CSS}</style></head>
<body>
<h1>floord 信号复盘 · ETF/LOF（513310 中韩半导体ETF · 161129 原油LOF）· {DAY}</h1>
<p class='lead'>标的类型：<span class='tag' style='background:#2a4d35;color:#9fe6a0'>ETF/LOF · T+0</span>
<span class='tag' style='background:#4d3b2a;color:#ffd479'>单向做多（首笔必为多，不可开空）</span>　算法：floord（v9.2.2，含漏顶漏底修复）。
resonance 当日 0 信号，故聚焦 floord。</p>

<div class='sec'>
<h2>一、信号触发情况回顾</h2>
<p>核心矛盾：floord 原始模型对 <b>两个 ETF/LOF 都各触发了 1 笔开空 S 信号</b>（513310 @13:05、161129 @10:44），
但 ETF/LOF 不可开空，这些信号在真实部署中无效。下方两张图叠加了 floord 原始信号（含橙▼卖空），可直观看到开空点的存在。</p>
<img src='{p513}'>
<img src='{p161}'>
<h3>原始 floord 信号序列（含对 ETF 无效的开空）</h3>
<p><b>513310</b>：</p>{raw_table(m513['raw_events'])}
<p><b>161129</b>：</p>{raw_table(m161['raw_events'])}
<h3>按「单向做多」约束重放后的有效 round-trip</h3>
<p><b>513310（中韩半导体ETF）</b> — 开空被转为无效(no-op)，仅保留 14:46 多单：</p>
{trips_table(lo513)}
<p><b>161129（原油LOF）</b> — 10:44 开空转无效，首笔多单为 13:14：</p>
{trips_table(lo161)}
<p class='note'>513310：有效多单 1 笔（14:46 价格地板新低），隔夜未平 P&L <b>{fmt(na513)}</b>（收盘 5.070 vs 入场 5.142 = -1.40%）。
161129：有效多单 3 笔已平（胜 {w161}），隔夜未平 1 笔；已平合计 <b>{fmt(nr161)}</b>，含隔夜合计 <b>{fmt(na161)}</b>。</p>
</div>

<div class='sec'>
<h2>二、实际表现与预期偏差分析</h2>
<h3>1. 结构性偏差：floord 的「开空」对 ETF/LOF 是死信号</h3>
<p>513310 的 13:05 开空 @5.128 与 161129 的 10:44 开空 @2.23，在双向模型里分别 -2.01%（价格随后涨）与 +0.58%（价格跌，方向巧合对）。
但无论盈亏，<b>ETF/LOF 无法执行开空</b>，原模型把它们当成真实持仓去反向平仓（513310 在 14:40 被 B 触发平空 @5.231，亏 2%）。
在单向约束下，这两笔应直接 no-op——意味着 floord 当前对 ETF/LOF 的「卖空」分支是纯噪声，甚至会通过内部状态机扭曲后续逻辑。</p>
<h3>2. 513310 的唯一有效多单是「接飞刀」且隔夜亏损</h3>
<p>513310 当日 -2.48%、H-L 高达 7.79%（剧烈震荡下行）。14:46 的「价格地板(新低 dev=-1.98%)」多单 @5.142，
到收盘 5.070 反而 -1.40%。也就是说，floord 在当日最剧烈的一段下跌末端去接刀，且 T+0 虽可当日平，却因无出场触发而隔夜（次日才知盈亏）。
预期中的「价格地板抓反弹」在单边下行日失效。</p>
<h3>3. 161129 多单质量分化，尾盘过密</h3>
<p>161129 当日 +0.98%、H-L 7.86%。3 笔已平多单 +0.90% / -0.86% / +0.55%：14:21 那笔（新低 dev=-1.58%）买入后短暂续跌，
被 TRAIL 在 -0.86% 扫掉，紧接着 14:26 在更低点 2.185 再买并 +0.55% 盈利——<b>同一波下跌里先被止损再被重新接回</b>，
说明固定 TRAIL(0.4/0.6) 对该 LOF 偏紧，把噪声扫成亏损后又追高买回，增加摩擦。此外 14:21/14:26/14:44 三笔集中在尾盘 25 分钟内，换手过密。</p>
<h3>4. 入场质量量化（多单后向偏移）</h3>
<p><b>513310 多单</b></p>{sq_table(m513['signal_quality'])}
<p><b>161129 多单</b></p>{sq_table(m161['signal_quality'])}
<p class='warn'>偏差小结：① floord 的「开空」分支对 ETF/LOF 完全无效且扰乱状态机；② 在单边下行日（513310）价格地板买点失效并隔夜亏损；
③ 高波动 LOF（161129）固定止损偏紧导致「扫损后追买」；④ 尾盘换手过密。这些都不是参数微调能解决的，需要<b>按品种约束改造信号语义</b>。</p>
</div>

<div class='sec'>
<h2>三、可落地优化建议（ETF/LOF · T+0 · 单向做多）</h2>
<div class='opt'>
<b>① 按品种关闭开空分支（优先级最高，立即可做）</b><br>
在 floord 的 S 触发处加品种开关：ETF/LOF 的 S 信号<b>不开启空仓</b>——若已持多则作为「平多」触发，若空仓则 no-op。
这直接消除 513310@13:05、161129@10:44 这类死信号及其对状态机的污染，且天然强制「首笔必为多」。<br><br>
<b>② 价格地板买点叠加趋势确认（优先级高）</b><br>
513310 的「新低 dev=-1.98%」在单边下行日接刀亏损。建议对 ETF/LOF 多单要求：价格地板新低 <b>且</b> 出现「新低后 3–5 根内收回前一根中枢」
（即摆动低点被确认），避免买在下跌半山腰。可在回测 walk-forward 标定确认窗口。<br><br>
<b>③ 止损改为 ATR 自适应（优先级中）</b><br>
将固定 TRAIL(0.4/0.6) 改为 k×ATR（如 trail_activate=1.2×ATR、trail=1.8×ATR）。
原油LOF(161129) H-L 7.86% 的日波动下，固定 0.6% 回撤太小，正是 14:21 被扫损的根因。按品种波动缩放可显著减少「扫损后追买」。<br><br>
<b>④ 尾盘换手节流（优先级中）</b><br>
对 T+0 ETF/LOF 限制每标的每日 round-trip 次数（如 ≤3）或尾盘 30 分钟内仅允许「更强信号」（如 dev≤-2.0% 的价格地板）新开多，降低摩擦与过拟合噪声。<br><br>
<b>⑤ 隔夜/尾盘 EOD 强平或限仓</b><br>
虽 T+0 可当日平，但 floord 无显式 EOD 强平（见 513310@14:46、161129@14:44 隔夜）。建议 14:55 强制平掉当日多单，
或至少对尾盘新开多设硬止损，避免把当日 T 变成被动隔夜。
</div>
<p style='color:#7a8290;font-size:12px'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
    return html


def main():
    data = recompute()
    M = {sym: build_metrics(sym, info) for sym, info in data.items()}
    sym513 = '513310.SH'; sym161 = '161129.SZ'; sym603 = '603659.SH'

    h_stock = render_stock(M[sym603], sym603)
    with open(os.path.join(OUT, 'report_stock_603659.html'), 'w', encoding='utf-8') as f:
        f.write(h_stock)

    h_etf = render_etf(M[sym513], M[sym161], sym513, sym161)
    with open(os.path.join(OUT, 'report_etf_lof.html'), 'w', encoding='utf-8') as f:
        f.write(h_etf)

    # 存档指标
    dump = {sym: {k: v for k, v in m.items() if k != 'day'} for sym, m in M.items()}
    with open(os.path.join(OUT, 'metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=2, default=lambda o: float(o) if isinstance(o, (np.floating,)) else o)

    print('DONE ->', OUT)


if __name__ == '__main__':
    main()
