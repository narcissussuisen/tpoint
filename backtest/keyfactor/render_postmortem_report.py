# -*- coding: utf-8 -*-
"""渲染 7/24 floor 信号复盘 + 历史对照 HTML 报告。"""
import os, json, csv

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
OUT = os.path.join(ROOT, 'output', 'postmortem_20260724')

S = json.load(open(os.path.join(OUT, 'summary.json'), encoding='utf-8'))

# 7/24 信号明细
sig_rows = list(csv.DictReader(open(os.path.join(OUT, 'signals_0724.csv'), encoding='utf-8-sig')))
# 7/24 roundtrip
trip_rows = list(csv.DictReader(open(os.path.join(OUT, 'day_roundtrips_0724.csv'), encoding='utf-8-sig')))
# 历史敏感性
sweep = list(csv.DictReader(open(os.path.join(OUT, 'sensitivity.csv'), encoding='utf-8-sig')))

H = S['history']
D = S['day0724']
hist_err = (1 - H['win_rate']) * 100
day_err = (1 - D['win_rate']) * 100

def pct(x):
    return f"{x*100:.1f}%" if isinstance(x, float) else str(x)

# ---- 信号明细表 ----
sig_html = "<table border='1' cellspacing='0' cellpadding='5'>"
sig_html += ("<tr><th>标的</th><th>类型</th><th>时间</th><th>价格</th>"
             "<th>fwd6</th><th>fwd12</th><th>fwd24</th><th>fwd48</th><th>fwd120</th></tr>")
for r in sig_rows:
    def fmt(v):
        return '-' if v == '' or v is None else f"{float(v):+.3f}%"
    sig_html += (f"<tr><td>{r['sym']}</td><td>{r['type']}</td><td>{r['time']}</td>"
                 f"<td>{r['price']}</td>{fmt(r['fwd6'])}{fmt(r['fwd12'])}"
                 f"{fmt(r['fwd24'])}{fmt(r['fwd48'])}{fmt(r['fwd120'])}</tr>")
sig_html += "</table>"

# ---- 7/24 配对盈亏表 ----
trip_html = "<table border='1' cellspacing='0' cellpadding='5'>"
trip_html += ("<tr><th>标的</th><th>方向</th><th>入场</th><th>入场价</th>"
              "<th>出场</th><th>出场价</th><th>出场原因</th><th>盈亏</th></tr>")
for r in trip_rows:
    pnl = float(r['pnl_pct'])
    col = '#2ecc71' if pnl > 0 else '#e74c3c'
    trip_html += (f"<tr><td>{r['sym']}</td><td>{r['side']}</td><td>{r['entry_time']}</td>"
                  f"<td>{r['entry_price']}</td><td>{r['exit_time']}</td><td>{r['exit_price']}</td>"
                  f"<td>{r['exit_reason']}</td>"
                  f"<td style='color:{col}'>{pnl:+.3f}%</td></tr>")
trip_html += "</table>"

# ---- 对照表 ----
vs_html = "<table border='1' cellspacing='0' cellpadding='5'>"
vs_html += ("<tr><th>指标</th><th>历史基线(20日,108笔)</th><th>7/24 当日(4笔)</th><th>偏差</th></tr>")
vs_html += (f"<tr><td>胜率</td><td>{pct(H['win_rate'])}</td><td>{pct(D['win_rate'])}</td>"
            f"<td style='color:#e74c3c'>-{ (H['win_rate']-D['win_rate'])*100:.1f}pp</td></tr>")
vs_html += (f"<tr><td>错误率</td><td>{hist_err:.1f}%</td><td>{day_err:.1f}%</td>"
            f"<td style='color:#e74c3c'>+{day_err-hist_err:.1f}pp</td></tr>")
vs_html += (f"<tr><td>平均盈亏</td><td>{H['avg_pnl']:+.4f}%</td><td>"
            f"{'-'}</td><td>-</td></tr>")
vs_html += (f"<tr><td>盈亏比(PF)</td><td>{H['profit_factor']}</td><td>-</td>"
            f"<td>历史 PF<1, 本就微亏</td></tr>")
vs_html += (f"<tr><td>平均盈利</td><td>{H['avg_win']:+.4f}%</td><td>-</td><td>-</td></tr>")
vs_html += (f"<tr><td>平均亏损</td><td>{H['avg_loss']:+.4f}%</td><td>-</td>"
            f"<td>亏幅&gt;盈幅, 期望为负</td></tr>")
vs_html += "</table>"

# ---- 敏感性表 ----
sw_html = "<table border='1' cellspacing='0' cellpadding='5'>"
sw_html += ("<tr><th>配置</th><th>笔数</th><th>胜率</th><th>平均盈亏</th>"
            "<th>平均盈利</th><th>平均亏损</th><th>盈亏比PF</th></tr>")
for r in sweep:
    pf = float(r['profit_factor'])
    pcol = '#2ecc71' if pf >= 1 else '#e74c3c'
    sw_html += (f"<tr><td>{r['config']}</td><td>{r['n']}</td><td>{float(r['win_rate'])*100:.1f}%</td>"
                f"<td>{float(r['avg_pnl']):+.4f}%</td><td>{float(r['avg_win']):+.4f}%</td>"
                f"<td>{float(r['avg_loss']):+.4f}%</td>"
                f"<td style='color:{pcol}'>{pf:.2f}</td></tr>")
sw_html += "</table>"

html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>161129/513310 2026-07-24 floor 信号复盘</title></head>
<body style='background:#1e1e1e;color:#ddd;font-family:SimHei,sans-serif;line-height:1.6;padding:24px;max-width:1100px;margin:auto'>
<h1>161129 / 513310 · 2026-07-24 floor 信号复盘与历史对照</h1>
<p style='color:#aaa'>口径：实盘同款 <code>core/miji_alpha</code> floor 门控 + 内联移动止损(0.4/0.6)。
历史基线 = 两标的 2026-06-26~07-23 共 20 个交易日、108 笔配对回合（每日重置仓位，与 monitor 一致）。
7/24 当日信号 = <code>data/push_audit.jsonl</code> 飞书推送审计（你盘中收到的真实信号）。</p>

<h2>一、当日信号与实际表现对比</h2>
<p><b>161129 原油LOF(易方达原油QDII-LOF)</b>：O=2.136 H=2.293 L=2.126 C=2.157，日内振幅 7.82%，
日涨 +0.98%；上午脉冲冲高 +6.09%（午前收 2.266），<b>午后持续回落</b>。3 笔 floor 买入全部落在午后下行段。</p>
<p><b>513310 中韩半导体ETF</b>：O=5.199 H=5.452 L=5.058 C=5.07，振幅 7.58%，日跌 -2.48%，
<b>全天单边偏弱</b>（午前 -1.54% / 午后 -2.48%）。14:55 的买入接在当日下行末端。</p>
<h3>7/24 真实配对盈亏</h3>
{trip_html}
<p style='color:#9b59b6'>关键事实澄清：你点名的 <b>13:12 买入</b> 实际是<b>盈利</b>的（+1.09%，13:24 移动止损出场）。
拖累当天的是另外三笔——14:21(-0.45%)、14:43(-1.60%, 持有至收盘)、513310 14:55(-0.43%, 持有至收盘)。
此前"对不上"是旧复盘图<b>引擎选错+漏画出场</b>所致，并非该信号本身错误。</p>
<h3>逐信号前向收益（方向验证）</h3>
{sig_html}

<h2>二、错误概率量化与历史对照</h2>
{vs_html}
<p>7/24 当日 4 笔配对 <b>仅 1 笔盈利（13:12），错误率 75%</b>；而历史基线错误率约 45.4%。
即当日错误率较历史 <b>抬升约 +30pp，胜率几乎腰斩</b>（54.6%→25.0%）。需强调：7/24 仅 4 笔样本，属个案偏差，
但方向与"历史 PF=0.95 本就微亏"一致——floor 在这两只票上本就是<b>边际微负期望</b>策略，差日子里会被放大。</p>

<h2>三、根因诊断（四维度）</h2>
<p><b>1. 市场环境（主因）</b>：7/24 是弱/震荡市。161129 走"上午脉冲+下午回落"，513310 全天单边下跌。
floor 的 B 信号本质是<b>均值回归</b>（价格偏离 VWAP + MACD 绿柱收缩），依赖价格回补；
在午后下行趋势里反复抄底=接飞刀，3 笔下午买全陷在回落段。</p>
<p><b>2. 信号触发条件</b>：floor B 仅要求"引力 + MACD 绿柱收缩"，<b>无任何趋势/波动率/市场状态过滤</b>。
下行市中"跌多了+MACD 收敛"会高频触发，信号密度高但质量低（与 200 只票回测结论一致：floor 信号多于 strict）。</p>
<p><b>3. 数据源准确性</b>：monitor 日志显示 7/24 当天 161129 <b>上午大半段"无行情数据/compute exception"</b>，
13:12 买入恰发生在数据恢复后不久，可能建立在残缺/重连后的部分行情上；且实盘实时 mootdx 与复盘收盘 CSV
在触发 bar 上存在 1–2 分钟偏移（已在上一轮对账确认），影响精确触发但不改变方向判定。</p>
<p><b>4. 策略参数</b>：① <b>移动止损过紧</b>（trail_activate 0.4% / trail_pct 0.6%）——
在 LOF/ETF 这类每分波动 σ≈0.33% 的品种上，价格正常噪声即可触发 -0.6% 止损，常在回补前被扫掉；
② <b>尾盘无禁开约束</b>：14:43、14:55 两笔临近收盘买入，<b>没有时间回补即被强制按收盘了结</b>（14:43 亏 -1.60%）。</p>

<h2>四、可落地优化方案（按根因对应）</h2>
<table border='1' cellspacing='0' cellpadding='5'>
<tr><th>根因</th><th>优化方案</th><th>参数/逻辑改动</th><th>作用</th></tr>
<tr><td>移动止损过紧</td><td>放宽移动止损</td><td>trail_pct 0.6%→1.0%（或改为 ATR 基准：止损=入场-1.5×ATR）</td><td>减少噪声止损，给回归留出空间</td></tr>
<tr><td>尾盘接飞刀</td><td>尾盘禁开</td><td>14:30 后不再开新仓（close-only）</td><td>消除"无时间回补"的强制亏损</td></tr>
<tr><td>无市场状态过滤</td><td>弱势 regime 抑制</td><td>当日跌幅&gt;阈值或价格在 VWAP 下且趋势向下时，B 需叠加更强确认（量价背离/RSI 超卖）</td><td>下行市少发边际信号</td></tr>
<tr><td>数据源残缺</td><td>行情完整性闸门</td><td>开盘后若 10:00 前 bar 数不足 / 检测到缺口，暂停信号直到 feed 稳定（参考 monitor 已有 freshness 判定）</td><td>避免重连后部分行情触发</td></tr>
<tr><td>信号质量</td><td>（慎用）提门槛</td><td>见下方 D 方案：仅强信号(size≥4)入场——<b>实测反而更差，不推荐单用</b></td><td>过度过滤会牺牲样本与收益</td></tr>
</table>

<h2>五、预期改进效果（基于 20 日历史敏感性回测）</h2>
{sw_html}
<p><b>结论与推荐配置</b>：</p>
<ul>
<li><b>方案 A（放宽移动止损 0.4/1.0）</b>：PF 从 0.95→<b>1.05</b>，期望转正（+0.024%），是最有效的单点修复——
直接解决"噪声止损"。</li>
<li><b>方案 C（A + 尾盘禁开 14:30）</b>：PF=<b>1.06</b>、平均盈亏 +0.031%，为综合最优，且笔数仅小幅下降（108→97）。</li>
<li><b>方案 D（仅强信号+放宽+禁尾盘）</b>：PF 反而跌到 <b>0.84</b>——证明"单纯砍信号数量"是误区，
问题在止损宽度而非信号过多，过度过滤只会更糟。</li>
</ul>
<p>预期：落地 <b>C 配置（trail_pct=1.0 + 14:30 禁开）</b>后，两标的 floor 策略由微亏转为微盈
（历史 PF≈1.06，胜率维持 ~55%）；对 7/24 这类差日，尾盘两笔（14:43、14:55）将被直接屏蔽，
当日 4 笔中的 2 笔亏损可避免。仍须注意：即便优化，胜率仅略高于五成、平均亏损仍约 -1.1%，
该策略属<b>边际型做T</b>，不能指望其单独稳定盈利，需配合仓位与标的选择。</p>

<p style='color:#888'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""

with open(os.path.join(OUT, 'report.html'), 'w', encoding='utf-8') as f:
    f.write(html)
print('written ->', os.path.join(OUT, 'report.html'))
