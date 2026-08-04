# -*- coding: utf-8 -*-
"""生成"全市场通用性 + 阈值敏感性"HTML 报告（深色主题，tpoint 风格）。

数据源:
  output/market_generality_2026-08-01.json   (68 只跨板块抽样, mhd ∈ {0.0, 0.15})
  output/market_threshold_sweep_2026-08-01.json (68 只 × 10 阈值网格)
输出:
  output/generality_sensitivity_2026-08-01.html
"""
import base64
import html
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

SECTOR_CN = {
    'sh_main': '沪主板', 'sz_main': '深主板', 'chinext': '创业板',
    'star': '科创板', 'bse': '北交所', 'etf_lof': 'ETF/LOF',
}
OUT = os.path.join(BASE, 'output', 'generality_sensitivity_2026-08-01.html')


def esc(x):
    return html.escape(str(x))


def load(fname):
    with open(os.path.join(BASE, 'output', fname), encoding='utf-8') as fh:
        return json.load(fh)


def build_html(gen, sweep):
    # ============ 1) 8标的基线（来自记忆/消融报告） ============
    base8 = {
        'm0': {'ret': -144.22, 'wr': None, 'note': '8标的汇总净收益（m 门控全放行）'},
        'm015': {'ret': 7.77, 'wr': None, 'note': '8标的汇总净收益（mhd=0.15）'},
    }

    # ============ 2) 通用性：板块对比表 ============
    sec_rows = []
    for key, label in [('th0', '0.00'), ('th015', '0.15')]:
        for sector in SECTOR_CN:
            a = gen['summary']['by_sector'][key].get(sector, {})
            sec_rows.append((label, sector, a))

    ov = {}
    for key in ['th0', 'th015']:
        ov[key] = gen['summary']['overall'][key]

    # ============ 3) 敏感性表 ============
    sens_rows = []
    for r in sweep['rows']:
        sens_rows.append(r)

    # ============ 4) 敏感性分段 ============
    segs = sweep.get('segments', [])

    # ============ HTML ============
    h = []
    h.append('''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>因子通用性验证 + 阈值敏感性分析 · tpoint 2026-08-01</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2333;--line:#30363d;--fg:#e6edf3;
--mut:#8b949e;--acc:#58a6ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px} h2{font-size:17px;margin:28px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;text-align:center}
.kpi .v{font-size:26px;font-weight:700} .kpi .l{color:var(--mut);font-size:12px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;background:var(--card2)} td:first-child,th:first-child{text-align:left}
tr:hover td{background:rgba(88,166,255,.05)}
.pos{color:var(--ok);font-weight:600} .neg{color:var(--bad);font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:var(--card2);border:1px solid var(--line);margin-left:6px}
.verdict{background:rgba(63,185,80,.08);border:1px solid rgba(63,185,80,.4)}
.warnbox{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.4)}
.badbox{background:rgba(248,81,73,.08);border:1px solid rgba(248,81,73,.4)}
ol{padding-left:22px} li{margin:6px 0}
.small{color:var(--mut);font-size:12px}
.bar{height:10px;border-radius:5px;background:var(--card2);overflow:hidden;display:inline-block;vertical-align:middle;width:90px}
.bar i{display:block;height:100%;border-radius:5px}
</style></head><body>''')

    # ---- 标题 ----
    h.append(f'<h1>因子通用性验证 + 阈值敏感性分析</h1>')
    h.append('<div class="sub">tpoint 因子层信号质量优化 · 2026-08-01 · 数据源: F盘全市场 1m 历史库 (4149 只) · 抽样 68 只跨板块</div>')

    # ---- KPI ----
    h.append('<div class="grid">')
    for key, lab, prec in [('th0', 'mhd=0.0 总体净收益', 2), ('th015', 'mhd=0.15 总体净收益', 2)]:
        v = ov[key]['total_ret']
        cls = 'pos' if v > 0 else 'neg'
        h.append(f'<div class="kpi"><div class="v {cls}">{v:+.2f}%</div><div class="l">{lab}（68只 {ov[key]["total"]} 笔）</div></div>')
    # 改善幅度
    d = ov['th0']['total_ret'] - ov['th015']['total_ret']
    h.append(f'<div class="kpi"><div class="v pos">{abs(d):.0f}pp</div><div class="l">0.15 相对 0.0 净收益改善</div></div>')
    # 胜率
    h.append(f'<div class="kpi"><div class="v">{ov["th0"]["win_rate"]}%→{ov["th015"]["win_rate"]}%</div><div class="l">总体净胜率（0.0→0.15）</div></div>')
    h.append('</div>')

    # ---- 问题1：8标的筛选来源 ----
    h.append('<h2>问题① 8 标的是怎么筛出来的？是否具有全市场通用性？</h2>')
    h.append('<div class="card"><b>8 标的来源（证据链）</b><ol>'
             '<li><b>候选池 7 只</b>：2026-07-31 用户指定流程，按卡方三条件（换手率 5-15% / 成交额 / 波动）从全市场筛出，'
             '<span class="tag">中船特气</span><span class="tag">有研新材</span><span class="tag">华虹宏力</span>'
             '<span class="tag">长电科技</span><span class="tag">普冉股份</span> + 缺数据剔除 688048/688008</li>'
             '<li><b>watchlist 3 只</b>：161129 原油LOF / 513310 中韩半导体ETF / 688111 金山办公（588000 缺数据剔除）</li>'
             '<li><b>板块画像</b>：7/8 只集中在 <b>科创板/半导体产业链</b>，天然不具备全市场随机代表性'
             '（用户当时定位 tpoint 做 T 的标的本身偏 T+0 ETF/LOF + 半导体）</li>'
             '</ol><div class="small">→ 结论：8 标的 <b>不是</b> 全市场随机样本，是「半导体/科创板 + 少数 ETF」的偏置样本。'
             '其结论能否外推，必须用跨板块抽样验证——即下方结果。</div></div>')

    # ---- 问题1：通用性验证结果 ----
    h.append('<h2>通用性验证：68 只跨板块抽样结果</h2>')
    h.append('<div class="card"><table><tr><th>板块</th><th>样本数</th><th>mhd=0.0 净收益</th><th>mhd=0.0 胜率</th>'
             '<th>mhd=0.15 净收益</th><th>mhd=0.15 胜率</th><th>改善幅度</th></tr>')
    for sector in SECTOR_CN:
        a0 = gen['summary']['by_sector']['th0'].get(sector, {})
        a1 = gen['summary']['by_sector']['th015'].get(sector, {})
        n = gen['universe'] and sum(1 for u in gen['universe'] if u['sector'] == sector)
        d_ret = a0['total_ret'] - a1['total_ret']
        c0 = 'pos' if a0['total_ret'] > 0 else 'neg'
        c1 = 'pos' if a1['total_ret'] > 0 else 'neg'
        cd = 'pos' if d_ret > 0 else 'neg'
        # 改善比例 = 相对0.0的减亏
        pct = (d_ret / a0['total_ret'] * 100) if a0['total_ret'] < 0 else None
        pct_s = f'({pct:.0f}% 减亏)' if pct else ''
        h.append(f'<tr><td>{SECTOR_CN[sector]}</td><td>{n}</td>'
                 f'<td class="{c0}">{a0["total_ret"]:+.1f}%</td><td>{a0["win_rate"]:.1f}%</td>'
                 f'<td class="{c1}">{a1["total_ret"]:+.1f}%</td><td>{a1["win_rate"]:.1f}%</td>'
                 f'<td class="{cd}">{d_ret:+.1f}pp {pct_s}</td></tr>')
    a0 = ov['th0']; a1 = ov['th015']
    d_ret = a0['total_ret'] - a1['total_ret']
    pct = (d_ret / a0['total_ret'] * 100) if a0['total_ret'] < 0 else None
    pct_s = f'({pct:.0f}% 减亏)' if pct else ''
    h.append(f'<tr style="font-weight:700"><td>总体</td><td>68</td>'
             f'<td class="neg">{a0["total_ret"]:+.1f}%</td><td>{a0["win_rate"]:.1f}%</td>'
             f'<td class="neg">{a1["total_ret"]:+.1f}%</td><td>{a1["win_rate"]:.1f}%</td>'
             f'<td class="pos">{d_ret:+.1f}pp {pct_s}</td></tr>')
    h.append('</table></div>')

    # OOS
    h.append('<div class="card"><b>样本外验证（OOS，前70%train/后30%test）</b><table><tr><th>阈值</th><th>train 净收益</th>'
             '<th>train 胜率</th><th>test 净收益</th><th>test 胜率</th><th>方向一致性</th></tr>')
    for key, label in [('th0', '0.00'), ('th015', '0.15')]:
        # 从 sweep 或 gen 取 train/test 汇总
        tr = agg_from_gen(gen, key, 'train')
        te = agg_from_gen(gen, key, 'test')
        same = '✓ 一致' if (tr['total_ret'] > 0) == (te['total_ret'] > 0) else '✗ 反转'
        ctr = 'pos' if tr['total_ret'] > 0 else 'neg'
        cte = 'pos' if te['total_ret'] > 0 else 'neg'
        h.append(f'<tr><td>{label}</td><td class="{ctr}">{tr["total_ret"]:+.1f}%</td><td>{tr["win_rate"]:.1f}%</td>'
                 f'<td class="{cte}">{te["total_ret"]:+.1f}%</td><td>{te["win_rate"]:.1f}%</td><td>{same}</td></tr>')
    h.append('</table></div>')

    # 通用性结论
    h.append('''<div class="card verdict"><b>通用性结论（问题①答案）</b><ol>
<li><b>方向普适</b>：强度阈值 0.15 在全部 6 个板块都带来一致的改善（减亏 65-72%），
不是半导体板块特有现象 → 弱背离=负 alpha 噪音的判断<b>全市场成立</b>。</li>
<li><b>幅度不足</b>：但全市场样本上 0.15 只是「大幅减亏」，仍未转正（总体 -896%）。
8 标的转正（+7.77%）是「减亏 + 半导体板块 beta」的叠加，不是阈值本身的功劳。</li>
<li><b>根本问题</b>：68 只里仅 3 只（mhd=0.0）到 13 只（0.10）为正收益标的，
做 T 策略整体仍是负 alpha——强度阈值是「过滤器」不是「印钞机」，它减少了坏信号，但没有创造好信号。</li>
<li><b>对生产的含义</b>：接入 0.15 阈值可显著降低推送噪音，但 <b>不能</b> 承诺胜率达标；
达标仍需依赖入场质量（早盘 g 因子 + 地板模式）与标的池筛选。</li></ol></div>''')

    # ---- 问题2：阈值敏感性 ----
    h.append('<h2>问题② 阈值数值不同，会影响准确率吗？</h2>')
    h.append('<div class="card"><table><tr><th>阈值</th><th>样本量</th><th>full 净收益</th><th>full 胜率</th>'
             '<th>train 净收益</th><th>test 净收益</th><th>test 胜率</th><th>正收益标的</th><th>样本量变化</th></tr>')
    prev_n = None
    for r in sens_rows:
        v = r['full']['total_ret']
        cls = 'pos' if v > 0 else 'neg'
        n = r['full']['total']
        n_chg = f'{(n/prev_n-1)*100:+.0f}%' if prev_n else '—'
        prev_n = n
        ctr = 'pos' if r['train']['total_ret'] > 0 else 'neg'
        cte = 'pos' if r['test']['total_ret'] > 0 else 'neg'
        h.append(f'<tr><td>{r["threshold"]:.2f}</td><td>{n}</td>'
                 f'<td class="{cls}">{v:+.2f}%</td><td>{r["full"]["win_rate"]:.1f}%</td>'
                 f'<td class="{ctr}">{r["train"]["total_ret"]:+.2f}%</td>'
                 f'<td class="{cte}">{r["test"]["total_ret"]:+.2f}%</td><td>{r["test"]["win_rate"]:.1f}%</td>'
                 f'<td>{r["pos_sym_ratio"]:.0f}%</td><td class="small">{n_chg}</td></tr>')
    h.append('</table></div>')

    # 敏感性分段
    h.append('<div class="card"><b>敏感性分段（相邻阈值 full 净收益变化）</b><table><tr><th>区间</th>'
             '<th>full 净收益变化</th><th>full 胜率变化</th><th>test 净收益变化</th><th>敏感性判断</th></tr>')
    for s in segs:
        d = s['d_full_ret']
        dt = s['d_test_ret']
        sens = '敏感区' if abs(d) > 150 else ('过渡区' if abs(d) > 50 else '平台期')
        cls = 'bad' if sens == '敏感区' else ('warn' if sens == '过渡区' else 'ok')
        h.append(f'<tr><td>{s["from"]:.2f}→{s["to"]:.2f}</td>'
                 f'<td>{d:+.2f}pp</td><td>{s["d_full_wr"]:+.2f}pp</td><td>{dt:+.2f}pp</td>'
                 f'<td class="{cls}">{sens}</td></tr>')
    h.append('</table></div>')

    # 敏感性结论
    h.append('''<div class="card verdict"><b>敏感性结论（问题②答案）</b><ol>
<li><b>存在陡峭区与平台区</b>：0.0→0.05 是全市场最大的改善台阶（-2739%→-1013%），
之后 0.05→0.10→0.15 进入递减平台，0.15 之后曲线进一步趋平。</li>
<li><b>0.05~0.30 是安全平台</b>：该区间内净收益/胜率差异在几十 pp 内，选 0.10/0.15/0.20
结果差异不大 → 阈值不是「针尖」敏感，有小幅偏移不影响结论。</li>
<li><b>0.15 的稳健性</b>：test 段 47.0%（0.15）vs 43.4%（0.0）胜率改善，且与 train 方向一致；
8 标的与 68 只抽样两个样本上 0.15 都优于 0.0 → 不是单一样本过拟合。</li>
<li><b>建议</b>：取 <b>0.15</b>（8 标的 test 最优 + 全市场稳健平台中值），
回退容忍度 ±0.05（0.10~0.20 均可接受），避免在陡峭区边缘选点。</li></ol></div>''')

    # ---- 结论与下一步 ----
    h.append('<h2>总结论与后续动作</h2>')
    h.append('''<div class="card"><ol>
<li><b>问题①</b>：8 标的 = 卡方三条件候选池（半导体集中）+ watchlist，非全市场随机；
强度阈值的「方向」全市场普适（6 板块一致减亏 65-72%），但「转正」不普适（8 标的转正是板块 beta 叠加）。</li>
<li><b>问题②</b>：阈值存在 0.05~0.30 安全平台，0.15 处于平台中部、8 标的与全市场两样本均最优，
不是针尖敏感；选 0.15 并保留 0.10~0.20 容忍带。</li>
<li><b>待决策</b>：是否将 macd_min_hist_diff=0.15 接入 monitor 生产路径 check_miji_trigger（预计显著降低推送噪音）。</li>
<li><b>后续</b>：① VWAP_DEV 网格寻优；② 早盘 g 因子入场质量深挖；③ 达标标的池重新筛选（全市场回测）。</li>
</ol></div>''')

    h.append('<div class="small" style="margin-top:20px;color:var(--mut)">tpoint 因子层优化流水线 · 2026-08-01 · '
             '数据: F盘 keyfactor_data/1m · 引擎: miji_alpha floor模式 · 成本: 万一佣金+印花税/滑点2bps</div>')
    h.append('</body></html>')

    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(h))
    print(f'HTML 报告已生成 → {OUT}')
    return OUT


def agg_from_gen(gen, key, part):
    """从通用性验证结果重算某阈值某切分的总体聚合。"""
    from scripts.market_generality_check import agg_across
    ms = [gen['results'][key][s]['metrics'][part] for s in gen['results'][key]]
    return agg_across(ms)


if __name__ == '__main__':
    gen = load('market_generality_2026-08-01.json')
    sweep = load('market_threshold_sweep_2026-08-01.json')
    build_html(gen, sweep)
