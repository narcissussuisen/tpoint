#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_compare_html.py — 渲染 strict vs floor 今日实际走势对比报告 (peer-comparison 布局,
符合 quant-backtest-lab Iron rule: 经 render_dashboard + 官方模板, 禁止手写落地页)。

输入: output/compare_strict_floor_summary.json + output/compare_strict_floor_2026-07-20.csv
输出: output/compare_strict_floor_2026-07-20.html
"""
import os
import sys
import json
import csv

sys.path.insert(0, r'C:\Users\YZP\.workbuddy\plugins\marketplaces\experts\plugins\strategy-backtest-expert\skills\quant-backtest-lab\reference')
from render_dashboard import render_dashboard  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT_DIR = os.path.join(ROOT, 'output')
TEMPLATE = r'C:\Users\YZP\.workbuddy\plugins\marketplaces\experts\plugins\strategy-backtest-expert\skills\quant-backtest-lab\reference\dashboard_template.html'

MODE_LABEL = {
    'strict': 'strict · 生产默认(早盘)',
    'floor': 'floor · 拟flip(全日)',
}
SYM_NAME = {'161129.SZ': '原油LOF', '688347.SH': '华虹宏力'}


def f2(x):
    if x is None:
        return '—'
    try:
        return f'{float(x):.2f}'
    except (TypeError, ValueError):
        return str(x)


def fpct(x):
    if x is None:
        return '—'
    try:
        return f'{float(x)*100:.1f}%'
    except (TypeError, ValueError):
        return str(x)


def load_rows():
    path = os.path.join(OUT_DIR, 'compare_strict_floor_2026-07-20.csv')
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def build_curves(rows):
    curves = {}
    for mode in ('strict', 'floor'):
        mr = [r for r in rows if r['mode'] == mode]
        mr.sort(key=lambda r: (r['sym'], r['time']))
        cum = 0.0
        pts = []
        for r in mr:
            v = r['fwd12_recalc%']
            if v not in ('', None):
                cum += float(v)
                pts.append({'date': f"{SYM_NAME.get(r['sym'], r['sym'])} {r['time']}", 'value': round(cum, 3)})
        curves[mode] = pts
    return curves


def build_report():
    with open(os.path.join(OUT_DIR, 'compare_strict_floor_summary.json'), encoding='utf-8') as f:
        blob = json.load(f)
    bm = blob['by_mode']
    rows = load_rows()
    curves = build_curves(rows)

    # ---- metric_table: strict vs floor ----
    def g(m, k):
        return bm[m].get(k)
    table_rows = []
    def row(metric, fn):
        table_rows.append({'metric': metric, 'values': [{'main': fn('strict')}, {'main': fn('floor')}]})
    row('信号数', lambda m: f"{g(m,'n_signals')} (B{g(m,'nB')}/S{g(m,'nS')})")
    row('覆盖时段', lambda m: '早盘 09:30-11:30' if m == 'strict' else '全日 09:30-15:00')
    row('B准确率@12min', lambda m: fpct(g(m, 'B_acc12')))
    row('S准确率@12min', lambda m: fpct(g(m, 'S_acc12')))
    row('均B前收@12min', lambda m: f2(g(m, 'B_mean_fwd12')) + '%')
    row('均S前收@12min', lambda m: f2(g(m, 'S_mean_fwd12')) + '%')
    row('每信号|前收|@12min', lambda m: f2(g(m, 'mean_abs_fwd12')) + '%')

    # ---- custom_html: 关键发现 + 明细表 + 口径调和 + 免责 ----
    strict_s = bm['strict']
    floor_s = bm['floor']
    key_html = f"""
    <div class="bt-custom-gate-note">
      <p><b>关键发现（今日单日样本，n 小，仅供参考）:</b></p>
      <ol>
        <li><b>floor 的卖出信号质量明显弱于 strict。</b> floor 的 S 准确率@12min 仅 {fpct(floor_s['S_acc12'])}（均S前收 {f2(floor_s['S_mean_fwd12'])}%），
            即多数 floor 卖点之后价格反而继续上行 —— <b>"卖飞"</b>。strict 的 S 准确率@12min 高达 {fpct(strict_s['S_acc12'])}（均S前收 {f2(strict_s['S_mean_fwd12'])}%），卖点质量更高。</li>
        <li><b>原因：floor 额外放开了"价格天花板(新高+超买)"单因子卖点。</b> 在<b>趋势性上涨日</b>（今日 161129/688347 午后均走强），"新高+偏离≥1.5%"会持续成立、并非真顶，
            导致 floor 在下午新增大量卖点却卖在半山腰。strict 的卖点需 MACD 背离，更克制。</li>
        <li><b>floor 的买点略优于 strict</b>（B准确率 {fpct(floor_s['B_acc12'])} vs {fpct(strict_s['B_acc12'])}），信号量也更多（{floor_s['n_signals']} vs {strict_s['n_signals']}）—— floor 提供更多的"可操作点"，但其中卖点是短板。</li>
      </ol>
    </div>"""

    recon_html = """
    <div class="bt-custom-gate-note">
      <p><b>与沙箱结论的口径调和（重要，勿混读）:</b></p>
      <ol>
        <li><b>本页是"信号触发后的原始前向收益"（无成本、无次根K滞后、单日小样本）</b>，用于直观看信号是否踩对实际走势；它会<b>高估</b>实盘可捕获收益。</li>
        <li><b>沙箱（106只×历史1m，次根K成交+双边0.02%成本）结论</b>是 floor 的"每信号净T / 均净T%"最高、strict 最弱，且 md 因子反向预测 → 那是从"做T回合配对净收益"视角，与今日"信号方向质量"视角不同，二者可同时成立：
            floor 靠更多（含部分卖飞）的回合在聚合上拿到更高净T，但单看卖点方向质量今日不如 strict。</li>
        <li><b>覆盖不对称</b>：strict 仅早盘（12信号），floor 全日（22信号），直接比信号数不公平；本页指标已分列覆盖时段，请据此解读。</li>
        <li><b>单日 n 极小</b>（strict 6B/6S，floor 13B/9S），严格模式下胜率置信区间极宽，不能据一日推翻沙箱跨样本结论。flip 决策仍应以沙箱 OOS 切分 + 多日验证为准。</li>
      </ol>
    </div>"""

    # 明细表
    detail_rows = []
    for r in sorted(rows, key=lambda x: (x['mode'], x['sym'], x['time'])):
        f6 = r['fwd6_recalc%']; f12 = r['fwd12_recalc%']; f24 = r['fwd24_recalc%']
        def col(v):
            if v in ('', None):
                return '—'
            vv = float(v)
            color = '#f85149' if vv > 0 else ('#3fb950' if vv < 0 else '#8b949e')
            return f'<span style="color:{color}">{vv:+.2f}%</span>'
        detail_rows.append(
            f"<tr><td>{r['mode']}</td><td>{SYM_NAME.get(r['sym'], r['sym'])}</td>"
            f"<td>{r['time']}</td><td>{r['dir']}</td><td>{r['price']}</td>"
            f"<td>{col(f6)}</td><td>{col(f12)}</td><td>{col(f24)}</td></tr>")
    detail_html = f"""
    <div class="bt-custom-gate-note">
      <p><b>逐信号明细（前向收益 = 信号棒收盘 → N 根后收盘，因果）</b> 颜色: <span style="color:#f85149">红=涨</span> / <span style="color:#3fb950">绿=跌</span>（A股惯例）。</p>
      <table class="bt-custom-gate-tbl">
        <thead><tr><th>模式</th><th>标的</th><th>时间</th><th>方向</th><th>价格</th><th>6min</th><th>12min</th><th>24min</th></tr></thead>
        <tbody>{''.join(detail_rows)}</tbody>
      </table>
    </div>"""

    banner = """
    <div class="bt-custom-gate-banner">
      <b>对照分析报告</b> — strict(生产默认) vs floor(拟flip)。数据只读: strict 来自早盘复盘HTML, floor 来自本会话隔离脚本, 实际走势来自今日1m行情。与生产推送隔离, 不构成投资建议。
    </div>"""

    disclaimer = """
    <div class="bt-custom-gate-note" style="color:#8b949e;font-size:12px;border-top:1px solid #30363d;padding-top:10px;margin-top:10px">
      ⚠️ 免责声明: 本页为 tpoint 做T策略的 strict/floor 对照研究产物, 基于今日单日行情与简化前向收益口径, 含小样本偏差与方法学局限,
      不构成任何投资建议或收益承诺。实盘决策请以生产引擎实时信号 + 多日/OOS 验证为准, 并自担风险。
    </div>"""

    report_data = {
        "meta": {
            "strategy_name": "tpoint strict vs floor 今日走势对照",
            "market": "china_a",
            "generated_at": "",
            "report_kind": "strategy",
        },
        "summary": {
            "total_return_pct": None,
            "max_drawdown_pct": None,
            "win_rate_pct": None,
            "total_trades": bm['floor']['n_signals'] + bm['strict']['n_signals'],
            "sharpe": None,
        },
        "equity_curve": [],
        "pnl_curve": [],
        "drawdown_curve": [],
        "trade_history": [],
        "ui": {
            "subtitle": "strict(生产) vs floor(拟flip) · 今日实际走势对照",
            "active_tab": "comparison",
            "tabs": [{"id": "comparison", "label": "模式对照"}],
            "language": "zh",
        },
        "modules": [
            {
                "type": "line_chart",
                "tab": "comparison",
                "title": "累计前向收益(12min)曲线 — 若逐信号跟单的漂移",
                "subtitle": "按时间序累加每信号12min前向收益%; strict仅早盘, floor全日。仅示意踩点质量, 未计成本",
                "series": [
                    {"name": MODE_LABEL[m], "points": curves[m]} for m in ('strict', 'floor')
                ],
            },
            {
                "type": "metric_table",
                "tab": "comparison",
                "title": "核心指标对比 (前向收益口径一致)",
                "subtitle": "strict=早盘12信号 / floor=全日22信号",
                "columns": ["指标", "strict", "floor"],
                "rows": table_rows,
            },
            {
                "type": "custom_html",
                "tab": "comparison",
                "title": "关键发现 / 明细 / 口径调和 / 免责",
                "width": "full",
                "html": banner + key_html + recon_html + detail_html + disclaimer,
            },
        ],
    }
    return report_data


def main():
    rd = build_report()
    out = os.path.join(OUT_DIR, 'compare_strict_floor_2026-07-20.html')
    render_dashboard(rd, output_path=out, template_path=TEMPLATE)
    print(f'[HTML] 渲染完成: {out}')


if __name__ == '__main__':
    main()
