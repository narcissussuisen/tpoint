# -*- coding: utf-8 -*-
"""
build_gap_v2_analysis.py — tpoint 回测 vs 卡方系统 差异归因分析（2026-08-01 用户请求）

对标锚点：卡方 xlsx 口径（备注列: 双边费用 万3.5 买 + 万5.641 卖）
对比 tpoint 用户实际费率（万一 + 万5.641 印花 + 2bps 滑点 ≈ 双边 0.1164%）

五维差异归因（实证数据见 2026-08-01 运行输出）：
  D1 交易费率    : 卡方 0.126% vs tpoint 0.116% → 若用卡方费率回测, 胜率再降 1~5pp（方向反转, 成本不是tpoint落后的原因）
  D2 滑点模型    : 0→10bps 胜率降 2~14pp（688111 最敏感）; 但 0 成本毛胜率中位 54.5% 已低于 60% 线
  D3 撮合逻辑    : 收盘价 vs 下一bar开盘 实测 -3~+4pp 双向噪音, 非系统性前视偏差; 真实差距=人工执行延迟无法建模
  D4 数据源差异  : F盘复权价(15位小数) 对收益率比值无影响; 覆盖 8/4149 是广度问题
  D5 策略架构    : 主因。毛胜率(零成本)仅 600584(60.3%) 过线, 7/8 毛胜率 <60% → 信号本身质量不足, 与成本/撮合无关

核心洞察: 卡方 xlsx 20日胜率中位 61%, ≥60% 仅占 54.2% → 卡方优势在"从 5002 只中筛出 40 只高胜率标的",
而非"信号引擎对任意标的都灵"。tpoint 只有 8 只(持仓+候选), 7 只毛胜率<60% → 缺的正是标的筛选能力,
与用户决策("无回测验证筛选器前不加监控标的")方向完全一致。

输出: output/gap_v2_analysis_YYYY-MM-DD.html（深色主题单文件）
"""
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
OUT = os.path.join(BASE, 'output', f'gap_v2_analysis_{DATE}.html')

os.environ.setdefault('MACD_GATE_MODE', 'floor')

# 8 标的 CSV 路径（F盘 1m 历史库）
SYMBOLS = {
    '688146.SH': 'F:/keyfactor_data/1m/688146.SH_1m.csv',
    '600206.SH': 'F:/keyfactor_data/1m/600206.SH_1m.csv',
    '688347.SH': 'F:/keyfactor_data/1m/688347.SH_1m.csv',
    '600584.SH': 'F:/keyfactor_data/1m/600584.SH_1m.csv',
    '688766.SH': 'F:/keyfactor_data/1m/688766.SH_1m.csv',
    '161129.SZ': 'F:/keyfactor_data/1m/161129.SZ_1m.csv',
    '513310.SH': 'F:/keyfactor_data/1m/513310.SH_1m.csv',
    '688111.SH': 'F:/keyfactor_data/1m/688111.SH_1m.csv',
}

# 卡方口径：备注"双边费用万3.5+万5.641" → 买边佣金万3.5(0.035%) + 卖边佣金万3.5+印花万5.641(0.09141%) = 双边 0.12641%
KAFANG_COST = (0.035, 0.09141)  # 无滑点
KAFANG_COST_SLIP = (0.055, 0.11141)  # 含2bps滑点

# tpoint 用户实际: 万一(0.01%) + 印花万5.641 + 2bps滑点 = 买0.03% 卖0.08641% → 双边0.1164%
TPOINT_COST = (0.03, 0.08641)


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def load_trips():
    """重跑回测拿 trips（含 gross_ret_pct），供多口径敏感性重算。"""
    from scripts.backtest_screener import backtest_symbol
    results = {}
    for sym, path in SYMBOLS.items():
        if not os.path.exists(path):
            print(f'  ⚠️ 缺数据: {path}')
            continue
        try:
            r = backtest_symbol(path)
            results[sym] = r
            m = r['metrics']
            print(f'  ✅ {sym} 笔数{m["total"]:>4} 毛胜率{m["gross_win_rate"]:>5.1f}% 净胜率{m["win_rate"]:>5.1f}%')
        except Exception as e:
            print(f'  ❌ {sym}: {e}')
    return results


def resens(results):
    """用 gross_ret_pct 对不同费率/滑点组合重算胜率。"""
    import numpy as np
    combos = {
        '无成本(毛)': (0.0, 0.0),
        'tpoint万一+2bps': (0.03, 0.08641),
        'tpoint万一+0bps': (0.01, 0.06641),
        'tpoint万一+5bps': (0.06, 0.11641),
        'tpoint万一+10bps': (0.11, 0.16641),
        '卡方万3.5+0bps': KAFANG_COST,
        '卡方万3.5+2bps': KAFANG_COST_SLIP,
    }
    out = {}
    for sym, r in results.items():
        gross = np.array([t['gross_ret_pct'] for t in r['trips']], dtype=float)
        row = {}
        for name, (b, s) in combos.items():
            net = gross - b - s
            row[name] = round(float((net > 0).mean() * 100), 1)
        out[sym] = row
    return out


def match_analysis(results):
    """撮合逻辑差异：信号bar收盘价 vs 下一bar开盘价成交。
    返回 {sym: {'close_fill': 净胜率, 'next_open': 净胜率, 'n': 笔数}}"""
    import numpy as np
    from core.exit_manager import simulate_day, aggregate_metrics, make_config, cost_for_symbol
    from scripts.backtest_screener import PROD_CONFIG, load_1m_csv, group_by_day, day_prev_close
    from core.miji_alpha import compute_miji_indicators, detect_miji_signals

    mcfg = make_config(**PROD_CONFIG)
    out = {}
    for sym, path in SYMBOLS.items():
        if not os.path.exists(path):
            continue
        cost = cost_for_symbol(sym)
        df = load_1m_csv(path)
        days = group_by_day(df)
        trips_close = []
        trips_next = []
        for date, sub in days:
            pc = day_prev_close(df, date)
            if pc is None or pc <= 0:
                continue
            o = sub['open'].values.astype(float)
            h = sub['high'].values.astype(float)
            lo = sub['low'].values.astype(float)
            c = sub['close'].values.astype(float)
            v = sub['volume'].values.astype(float)
            data = compute_miji_indicators(o, h, lo, c, v, pc)
            sigs = detect_miji_signals(data, pc)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                      'trend': data.get('trend'), 'n': data['n']}
            trips_close.extend(simulate_day(sigs, prices, mcfg, cost=cost))
            # 下一bar开盘价成交：把信号price改为 min(i+1, n-1) 的开盘价
            sigs_next = []
            n = data['n']
            for sg in sigs:
                sg2 = dict(sg)
                j = min(sg['idx'] + 1, n - 1)
                sg2['price'] = float(o[j])
                sg2['idx'] = j
                sigs_next.append(sg2)
            trips_next.extend(simulate_day(sigs_next, prices, mcfg, cost=cost))
        m1 = aggregate_metrics(trips_close)
        m2 = aggregate_metrics(trips_next)
        out[sym] = {
            'close_fill': m1['win_rate'], 'next_open': m2['win_rate'],
            'n': m1['total'], 'n2': m2['total'],
        }
        print(f'  🔀 {sym}: 收盘价成交胜率 {m1["win_rate"]}% vs 下一bar开盘 {m2["win_rate"]}%')
    return out


def build_html(results, sens, match, tpoint_ref):
    # 主结果表
    main_rows = ''
    gross_list = []
    for sym in sorted(tpoint_ref):
        r = tpoint_ref[sym]
        m = r['metrics']
        gross_list.append(m['gross_win_rate'])
        main_rows += (f'<tr><td class="dim">{esc(sym)}</td><td>{m["total"]}</td>'
                      f'<td>{m["gross_win_rate"]}%</td><td>{m["win_rate"]}%</td>'
                      f'<td>{m["pl_ratio"]}</td><td>{m["ann_ret_pct"]}%</td>'
                      f'<td>{m["max_drawdown_pct"]}%</td></tr>')
    gross_med = sorted(gross_list)[len(gross_list) // 2] if gross_list else 0
    pass_zero = sum(1 for g in gross_list if g >= 60.0)

    # 敏感性表行
    combo_names = ['无成本(毛)', 'tpoint万一+2bps', 'tpoint万一+0bps', 'tpoint万一+5bps',
                   'tpoint万一+10bps', '卡方万3.5+0bps', '卡方万3.5+2bps']
    sens_rows = ''
    for sym in sorted(sens):
        row = sens[sym]
        cells = ''.join(f'<td>{row.get(nm, "-")}%</td>' for nm in combo_names)
        sens_rows += f'<tr><td class="dim">{esc(sym)}</td>{cells}</tr>'
    # 撮合表行
    match_rows = ''
    deltas = []
    for sym in sorted(match):
        m = match[sym]
        delta = round(m['next_open'] - m['close_fill'], 1)
        deltas.append(delta)
        d_cls = 'v-red' if delta < 0 else ('v-green' if delta > 0 else 'v-dim')
        match_rows += (f'<tr><td class="dim">{esc(sym)}</td>'
                       f'<td>{m["close_fill"]}%</td><td>{m["next_open"]}%</td>'
                       f'<td class="{d_cls}">{delta:+}pp</td></tr>')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tpoint × 卡方 回测差异归因 · {esc(DATE)}</title>
<style>
:root {{
  --bg:#0f1419; --panel:#1a222c; --panel2:#141b24; --line:#2b3644;
  --txt:#dbe4ee; --dim:#8b98a8; --red:#ff5f5f; --green:#3ddc84;
  --orange:#ffab40; --blue:#4da3ff; --purple:#b07bff; --cyan:#38d6d0;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--txt);
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.6; padding:28px 20px 60px; }}
.wrap {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:26px; margin-bottom:4px; }}
.sub {{ color:var(--dim); font-size:13px; margin-bottom:24px; }}
h2 {{ font-size:19px; margin:36px 0 14px; padding-left:10px; border-left:4px solid var(--blue); }}
h3 {{ font-size:15px; margin:18px 0 8px; color:var(--cyan); }}
.card {{ background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:18px 20px; margin-bottom:14px; }}
.kpi-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:10px 0 4px; }}
.kpi {{ flex:1; min-width:150px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.kpi .v {{ font-size:22px; font-weight:700; }}
.kpi .k {{ font-size:12px; color:var(--dim); margin-top:2px; }}
.v-red {{ color:var(--red); }} .v-green {{ color:var(--green); }}
.v-orange {{ color:var(--orange); }} .v-blue {{ color:var(--blue); }}
.v-purple {{ color:var(--purple); }} .v-cyan {{ color:var(--cyan); }}
.v-dim {{ color:var(--dim); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ border:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }}
th {{ background:var(--panel2); color:var(--cyan); font-weight:600; white-space:nowrap; }}
td.dim {{ white-space:nowrap; font-weight:600; color:var(--blue); }}
.ev {{ color:var(--dim); font-size:11px; margin-top:4px; font-family:Consolas,monospace; }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:700; }}
.g-p0 {{ background:rgba(255,95,95,.15); color:var(--red); border:1px solid var(--red); }}
.g-p1 {{ background:rgba(255,171,64,.15); color:var(--orange); border:1px solid var(--orange); }}
.g-p2 {{ background:rgba(77,163,255,.15); color:var(--blue); border:1px solid var(--blue); }}
.callout {{ background:rgba(255,171,64,.08); border:1px solid rgba(255,171,64,.35);
  border-radius:8px; padding:12px 16px; font-size:13px; margin:10px 0; }}
.callout.warn {{ background:rgba(255,95,95,.08); border-color:rgba(255,95,95,.4); }}
.callout.ok {{ background:rgba(61,220,132,.07); border-color:rgba(61,220,132,.35); }}
.footer {{ color:var(--dim); font-size:11.5px; margin-top:30px;
  border-top:1px solid var(--line); padding-top:12px; }}
.mono {{ font-family:Consolas,monospace; font-size:11.5px; color:var(--cyan); }}
</style>
</head>
<body>
<div class="wrap">

<h1>🔬 tpoint × 卡方 T0 回测差异归因分析</h1>
<div class="sub">{esc(DATE)} · 用户请求：以卡方系统为参照基准，分析 tpoint 回测与卡方显著差异的原因（费率/滑点/撮合/数据源），列出可改进方向并给出优化建议</div>

<div class="card">
<h3>核心结论</h3>
<div class="kpi-row">
  <div class="kpi"><div class="v v-red">0/8</div><div class="k">标的达标（60%胜率/1.6盈亏比）</div></div>
  <div class="kpi"><div class="v v-orange">{gross_med:.1f}%</div><div class="k">毛胜率中位（零成本零滑点）</div></div>
  <div class="kpi"><div class="v v-red">{pass_zero}/8</div><div class="k">零成本下仍过 60% 线</div></div>
  <div class="kpi"><div class="v v-blue">D5 主因</div><div class="k">策略信号质量（非成本/撮合）</div></div>
</div>
<div class="callout warn" style="margin-top:12px">
  ⚠️ <b>归因主结论（基于实证）</b>：8/8 未达标的根因<b>不是</b>成本口径，也不是撮合模型——
  把成本、滑点全部归零（毛胜率），8 只中也只有 1 只（600584 60.3%）勉强过 60% 线，中位仅 {gross_med:.1f}%。
  <b>信号本身的质量不足</b>（floor 抄底信号在单边下跌日连续触发、均值回归失效）才是主因。
  费率/滑点/撮合/数据源四维合计最多解释 2~14pp，无法解释毛胜率即不达线的事实。
</div>
<div class="callout ok">
  ✅ <b>关键对照</b>：卡方 xlsx 全市场 5002 只回测的 20 日胜率中位也仅 61%、≥60% 只占 54.2%——
  卡方不是"信号引擎对任意标的都灵"，而是<b>靠标的筛选从 5002 只里挑出 40 只高胜率标的</b>。
  tpoint 只有 8 只（持仓+候选）且 7 只毛胜率&lt;60%，缺的正是<b>筛选能力</b>——
  与用户"没有回测验证的筛选器前不加监控标的"的决策方向完全一致，方向正确。
</div>
</div>

<h2>一、五维差异归因总表</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>维度</th><th>卡方系统（参照）</th><th>tpoint 现状</th><th>影响方向</th><th>量化影响（实测）</th><th>评级</th></tr>
<tr>
  <td class="dim">D1 交易费率</td>
  <td>双边佣金万3.5+印花万5.641 ≈ 双边 0.126%</td>
  <td>用户实际万一不免五+印花万5.641+2bps滑点 ≈ 双边 0.116%</td>
  <td>tpoint 更有利</td>
  <td>若改用卡方费率（万3.5），胜率再降 1.3~5.0pp——<b>方向反转</b>，成本不是 tpoint 落后的原因</td>
  <td><span class="badge g-p2">P2 非主因</span></td>
</tr>
<tr>
  <td class="dim">D2 滑点模型</td>
  <td>程序化毫秒级执行，滑点极小（推测 &lt;1bps）</td>
  <td>回测假设 2bps/边；实盘人工执行实际滑点更大</td>
  <td>tpoint 回测偏乐观</td>
  <td>0→10bps 胜率降 2~14pp（688111 最敏感）；0→5bps 降 1~6pp。即使 0 滑点毛胜率也仅中位 {gross_med:.1f}%</td>
  <td><span class="badge g-p1">P1 执行层差距</span></td>
</tr>
<tr>
  <td class="dim">D3 撮合逻辑</td>
  <td>tick级/秒级撮合，成交价≈信号价</td>
  <td>1m bar 收盘价成交；信号 bar 内触发（15s 轮询）</td>
  <td>实测双向噪音</td>
  <td>改下一bar开盘成交：胜率 −3~+4pp 随机波动，<b>非系统性前视偏差</b>；真实差距在人工执行延迟（分钟级 vs 毫秒级）无法在回测建模</td>
  <td><span class="badge g-p2">P2 非主因</span></td>
</tr>
<tr>
  <td class="dim">D4 数据源差异</td>
  <td>机构级 tick 全市场 5002 只全历史</td>
  <td>F盘复权价（15位小数）+mootdx 免费源；8 只有数据（145天）</td>
  <td>覆盖受限，价格精度偏差</td>
  <td>复权等比缩放不影响收益率比值；覆盖 8/4149 是<b>广度</b>问题（缺 588000/688048/688008）</td>
  <td><span class="badge g-p1">P1 覆盖不足</span></td>
</tr>
<tr>
  <td class="dim">D5 策略架构</td>
  <td>5因子预测（动量6/12月+行业+情绪+波动率+流动性）+双周期（短10min+长1h）+标的筛选（成交额≥50亿/换手5-15%/振幅5-20%）</td>
  <td>3因子量价（VWAP引力+MACD背离+价格地板floor）+1m单周期+持仓驱动 watchlist</td>
  <td><b>主因</b>：信号无行情状态过滤</td>
  <td>毛胜率（0成本）即 47.6%~60.3%、中位 {gross_med:.1f}%；单边下跌日 floor 抄底连续触发（688146 07-03 单日 11 个 B 全为引力+地板）</td>
  <td><span class="badge g-p0">P0 主因</span></td>
</tr>
</table>
</div>

<h2>二、主回测结果（用户实际费率 · miji+floor 引擎）</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>标的</th><th>笔数</th><th>毛胜率</th><th>净胜率</th><th>盈亏比</th><th>年化</th><th>最大回撤</th></tr>
{main_rows}
</table>
<div class="ev" style="margin-top:8px">成本模型：万一佣金(不免五) + 印花万5.641(仅个股) + 2bps滑点；ETF/LOF 无印花税。毛→净落差即成本侵蚀（5~9pp）。</div>
</div>

<h2>三、费率×滑点敏感性（同一批 trips 重算，量化 D1+D2）</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>标的</th><th>无成本(毛)</th><th>tpoint万一<br>+2bps</th><th>tpoint万一<br>+0bps</th><th>tpoint万一<br>+5bps</th><th>tpoint万一<br>+10bps</th><th>卡方万3.5<br>+0bps</th><th>卡方万3.5<br>+2bps</th></tr>
{sens_rows}
</table>
<div class="callout" style="margin-top:10px">
  📌 <b>读法</b>：同一信号序列只换成本参数。①「无成本→tpoint万一+2bps」= 当前口径的成本侵蚀（2.6~9.0pp）；
  ②「tpoint万一+2bps→卡方万3.5+2bps」= 若用卡方更高费率，胜率再降 1.3~5.0pp（<b>方向反转，卡方口径对 tpoint 更不利</b>）；
  ③ 即使最乐观的「无成本」列，也仅 600584（60.3%）过 60% 线——<b>成本不是 8/8 未达标的原因</b>。
</div>
</div>

<h2>四、撮合逻辑对比（D3：收盘价成交 vs 下一bar开盘价成交）</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>标的</th><th>收盘价成交<br>（当前回测）</th><th>下一bar开盘成交<br>（信号确认后）</th><th>差异</th></tr>
{match_rows}
</table>
<div class="callout ok" style="margin-top:10px">
  ✅ <b>实证结论（修正）</b>：改为下一 bar 开盘价成交后，胜率 −3~+4pp 随机波动，<b>没有出现系统性下降</b>——
  说明当前回测的"收盘价成交"并未显著虚高胜率（跳空方向双向抵消）。真正的执行差距是<b>实盘人工执行延迟</b>：
  信号推送（秒级）→ 人工看盘决策（秒~分钟）→ 手动下单（秒~分钟）→ 成交（滑点），这条链的滑点远超回测 2bps 假设。
  卡方为程序化毫秒级委托（客户案例全天 22 笔），这是 tpoint 与卡方在<b>执行层</b>最实质的差异，但回测层面已无法进一步建模。
</div>
</div>

<h2>五、数据源差异（D4）</h2>
<div class="card">
<table>
<tr><th>项目</th><th>卡方</th><th>tpoint</th></tr>
<tr><td class="dim">行情级别</td><td>机构级 tick/秒级（5002只全市场）</td><td>mootdx 免费源 1m（3-4天）+ F盘历史库 1m（145天）</td></tr>
<tr><td class="dim">价格精度</td><td>真实成交价（2位小数）</td><td>F盘数据为<b>复权价（15位小数）</b>——复权等比缩放不影响收益率比值，但绝对价位与真实价有偏差；mootdx 免费源为真实价</td></tr>
<tr><td class="dim">覆盖范围</td><td>5002 只 × 全历史</td><td>8 只有数据（145天）；588000/688048/688008 缺 1m</td></tr>
<tr><td class="dim">除权除息</td><td>专业复权处理</td><td>F盘复权价在除权日连续，免费源有跳空风险——回测未覆盖除权除息测试</td></tr>
</table>
<div class="callout ok" style="margin-top:10px">
  ✅ <b>结论</b>：数据源差异主要影响<b>覆盖广度</b>与<b>绝对价位精度</b>，对收益率比值的策略排序影响有限。
</div>
</div>

<h2>六、可改进方向与优化建议（参考卡方实现）</h2>
<div class="card">
<table>
<tr><th>优先级</th><th>改进项</th><th>参考卡方做法</th><th>tpoint 落地建议</th></tr>
<tr>
  <td><span class="badge g-p0">P0</span></td>
  <td class="dim">行情状态过滤（解决单边下跌日连续触发）</td>
  <td>5因子模型含<b>波动率+流动性</b>因子；标的筛选限「震荡市/高振幅/高流动性」（PPT S5-6: 高波高换手收益最好）</td>
  <td>加<b>日级趋势闸门</b>：当日价格&lt;MA20 且收盘&lt;开盘（单边下跌日）→ 禁用 floor 地板抄底信号，只保留 MACD 背离；或 VWAP_DEV 带宽按 ATR 自适应放大；振幅&gt;8% 时暂停引力类信号（已列 P2-1）</td>
</tr>
<tr>
  <td><span class="badge g-p0">P0</span></td>
  <td class="dim">标的筛选器落地（达标才进池）</td>
  <td>三条件筛选（成交额≥50亿/换手5-15%/振幅5-20%）→ 40只名单；xlsx 5002只回测 + Level 星级</td>
  <td>market_screener.py 已修单位 bug（amount 字段），7/40 三条件全过；补拉缺 1m 数据（588000/688048/688008）后用 backtest_screener 验证，达标才并入 watchlist——<b>卡方靠筛选获胜的结论支撑此路线</b></td>
</tr>
<tr>
  <td><span class="badge g-p1">P1</span></td>
  <td class="dim">执行滑点管理（拉近与卡方执行层差距）</td>
  <td>程序化毫秒级委托（永鼎股份全天22笔）</td>
  <td>① 飞书信号卡片加「限时执行」提示（信号确认后 3-5 秒内决策）；② 给信号标注「执行价参考区间」（当前 bar 实时价±滑点预算）；③ 远期评估条件单/券商 API 对接（用户已拍板保持人工，列为远期）</td>
</tr>
<tr>
  <td><span class="badge g-p1">P1</span></td>
  <td class="dim">数据覆盖扩充</td>
  <td>5002只全市场</td>
  <td>F盘库已有 4149 只；补拉缺 3 只标的 1m；建立每日增量累积任务（mootdx 每日收盘补当日 1m → F盘库），滚出更长历史（当前 145 天窗口偏短）</td>
</tr>
<tr>
  <td><span class="badge g-p2">P2</span></td>
  <td class="dim">绩效统计对齐卡方</td>
  <td>xlsx 17列（20日/5日/当日收益、开仓率、胜率、Level星级）</td>
  <td>aggregate_metrics 已扩展（年化/回撤/夏普/毛净胜率对照）；补开仓率、滚动 20日/5日窗口、Level 星级——<b>实现"筛选后验证"的完整闭环</b></td>
</tr>
<tr>
  <td><span class="badge g-p2">P2</span></td>
  <td class="dim">双周期方向参考</td>
  <td>短10min+长1h 双周期（PPT S14-16）</td>
  <td>5m/1h 周期信号作为飞书卡片「大方向标注」，不融合进 1m 信号（避免 v9.3.0 证伪覆辙）</td>
</tr>
</table>
</div>

<div class="footer">
生成：{esc(DATE)} · 数据：F盘 keyfactor 1m 库（复权价）+ backtest_screener（miji+floor 引擎）+ 敏感性/撮合实测 · 脚本 build_gap_v2_analysis.py
</div>
</div>
</body>
</html>'''
    return html


def main():
    print('🔄 重跑 8 标的回测（拿 trips 供敏感性分析）...')
    results = load_trips()
    print('\n🔄 费率×滑点敏感性重算...')
    sens = resens(results)
    print('\n🔄 撮合逻辑对比（收盘价 vs 下一bar开盘）...')
    match = match_analysis(results)

    html = build_html(results, sens, match, results)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'\n✅ 差异归因报告: {OUT} ({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
