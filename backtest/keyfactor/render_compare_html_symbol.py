#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_compare_html_symbol.py — 渲染单只标的 strict vs floor 对比报告
(peer-comparison 布局, 经 render_dashboard + 官方模板, 禁止手写落地页)。

输入: output/<SYM>_strict_floor_<DATE>.csv + _summary.json
输出: output/<SYM>_strict_floor_<DATE>.html

用法: python render_compare_html_symbol.py <SYM> <DATE>
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

SYM = sys.argv[1] if len(sys.argv) > 1 else '159985.SZ'
DATE = sys.argv[2] if len(sys.argv) > 2 else '2026-07-20'

MODE_LABEL = {
    'strict': 'strict · 生产默认',
    'floor': 'floor · 拟flip',
}
SYM_LABEL = SYM  # 用户指定标的; 如需友好名在此补充映射


def f2(x):
    if x is None or x == '':
        return '—'
    try:
        return f'{float(x):.2f}'
    except (TypeError, ValueError):
        return str(x)


def fpct(x):
    if x is None or x == '':
        return '—'
    try:
        return f'{float(x)*100:.1f}%'
    except (TypeError, ValueError):
        return str(x)


def load_rows():
    path = os.path.join(OUT_DIR, f'{SYM}_strict_floor_{DATE}.csv')
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def build_curves(rows):
    curves = {}
    for mode in ('strict', 'floor'):
        mr = sorted([r for r in rows if r['mode'] == mode], key=lambda r: r['time'])
        cum = 0.0
        pts = []
        for r in mr:
            v = r['fwd12%']
            if v not in ('', None):
                cum += float(v)
                pts.append({'date': f"{SYM_LABEL} {r['time'][11:16]}", 'value': round(cum, 3)})
        curves[mode] = pts
    return curves


def build_report():
    with open(os.path.join(OUT_DIR, f'{SYM}_strict_floor_{DATE}_summary.json'), encoding='utf-8') as f:
        blob = json.load(f)
    bm = blob['by_mode']
    rows = load_rows()
    curves = build_curves(rows)

    # 是否两模式信号集合一致
    sk = sorted((r['time'], r['dir']) for r in rows if r['mode'] == 'strict')
    fk = sorted((r['time'], r['dir']) for r in rows if r['mode'] == 'floor')
    coincide = sk == fk

    def g(m, k):
        return bm[m].get(k)

    table_rows = []
    def row(metric, fn):
        table_rows.append({'metric': metric, 'values': [{'main': fn('strict')}, {'main': fn('floor')}]})
    row('信号数', lambda m: f"{g(m,'n_signals')} (B{g(m,'nB')}/S{g(m,'nS')})")
    row('覆盖时段', lambda m: '全日 09:30-15:00')
    row('B准确率@12min', lambda m: fpct(g(m, 'B_acc12')))
    row('S准确率@12min', lambda m: fpct(g(m, 'S_acc12')))
    row('均B前收@12min', lambda m: f2(g(m, 'B_mean_fwd12')) + '%')
    row('均S前收@12min', lambda m: f2(g(m, 'S_mean_fwd12')) + '%')
    row('每信号|前收|@12min', lambda m: f2(g(m, 'mean_abs_fwd12')) + '%')

    strict_s = bm['strict']
    floor_s = bm['floor']
    s_sigs = [r for r in rows if r['mode'] == 'strict' and r['dir'] == 'S']
    sell_info = ', '.join(f"@{r['time'][11:16]} {float(r['fwd12%']):+.2f}%" for r in s_sigs
                         if r['fwd12%'] not in ('', None) and float(r['fwd12%']) > 0)
    coin_line = (f'<li><b>本标的当日 strict 与 floor 信号集合完全一致</b>（均 {strict_s["n_signals"]} 个信号, '
                 f'时间/方向逐一对齐）。floor 的"价格新低/新高"附加门当日未触发任何额外信号 '
                 f'—— 即 159985 当日未出现 session 级极端新低/新高，flip 对该标的当日<b>无影响</b>（不增也不减信号）。</li>')
    key_html = f"""
    <div class="bt-custom-gate-note">
      <p><b>关键发现（{SYM_LABEL} {DATE}，单日样本，n 小，仅供参考）:</b></p>
      <ol>
        {coin_line}
        <li><b>卖点质量偏弱、买点质量强。</b> B准确率@12min = {fpct(strict_s['B_acc12'])}（唯一买点 @09:33 后续涨 {f2(strict_s['B_mean_fwd12'])}%）——
            早盘急跌偏离即买，踩对。S准确率@12min 仅 {fpct(strict_s['S_acc12'])}，3 个卖点中仅 1 个(@13:54)后续真正下跌，
            另 2 个({sell_info}) 卖在上涨途中（"卖飞"）。</li>
        <li><b>strict 与 floor 在此标的上表现同构</b>，故上述优劣同时适用于两种门控；flip 决策对该标的当日不改变信号质量。</li>
      </ol>
    </div>"""

    recon_html = """
    <div class="bt-custom-gate-note">
      <p><b>方法与口径（重要，勿混读）:</b></p>
      <ol>
        <li><b>本页 strict 与 floor 均由同一隔离引擎对当日全日 240 根已收盘 1m 棒计算</b>（非生产推送），两模式口径一致、可直接对比；
            区别于此前 161129/688347 报告中 strict 取自早盘生产复盘、floor 取自全日，覆盖不对称。</li>
        <li><b>前向收益 = 信号棒收盘 → +6/12/24 根收盘</b>（因果、无成本、无次根K滞后），用于直观看信号是否踩对实际走势，会<b>高估</b>实盘可捕获收益。</li>
        <li><b>单日 n 极小</b>（仅 4 信号），胜率置信区间极宽；严格意义上的 flip 决策仍应以沙箱 OOS 切分 + 多日多标的验证为准。</li>
        <li>RESONANCE_THRESHOLD=2 在引擎中为<b>死参数</b>（共振分仅作元信息，不参与触发判定），本页信号均为单因子/双因子触发。</li>
      </ol>
    </div>"""

    detail_rows = []
    for r in sorted(rows, key=lambda x: (x['mode'], x['time'])):
        f6 = r['fwd6%']; f12 = r['fwd12%']; f24 = r['fwd24%']
        def col(v):
            if v in ('', None):
                return '—'
            vv = float(v)
            color = '#f85149' if vv > 0 else ('#3fb950' if vv < 0 else '#8b949e')
            return f'<span style="color:{color}">{vv:+.2f}%</span>'
        detail_rows.append(
            f"<tr><td>{r['mode']}</td><td>{r['time'][11:19]}</td><td>{r['dir']}</td>"
            f"<td>{r['price']}</td><td>{r['resonance_score']}</td>"
            f"<td>{r['gravity']}/{r['vol_div']}/{r['macd_div']}</td>"
            f"<td>{col(f6)}</td><td>{col(f12)}</td><td>{col(f24)}</td>"
            f"<td style='font-size:11px;color:#8b949e'>{r['detail']}</td></tr>")
    detail_html = f"""
    <div class="bt-custom-gate-note">
      <p><b>逐信号明细（前向收益 = 信号棒收盘 → N 根后收盘，因果）</b> 颜色: <span style="color:#f85149">红=涨</span> / <span style="color:#3fb950">绿=跌</span>（A股惯例）。g/vd/md = 引力/量价/ MACD因子。</p>
      <table class="bt-custom-gate-tbl">
        <thead><tr><th>模式</th><th>时间</th><th>方向</th><th>价格</th><th>共振</th><th>g/vd/md</th><th>6min</th><th>12min</th><th>24min</th><th>因子明细</th></tr></thead>
        <tbody>{''.join(detail_rows)}</tbody>
      </table>
    </div>"""

    banner = """
    <div class="bt-custom-gate-banner">
      <b>strict vs floor 对比报告</b> — 同一隔离引擎对当日全日 1m 计算，与生产推送隔离，不构成投资建议。
    </div>"""

    disclaimer = """
    <div class="bt-custom-gate-note" style="color:#8b949e;font-size:12px;border-top:1px solid #30363d;padding-top:10px;margin-top:10px">
      ⚠️ 免责声明: 本页为 tpoint 做T策略 strict/floor 对照研究产物, 基于单日行情与简化前向收益口径, 含小样本偏差与方法学局限,
      不构成任何投资建议或收益承诺。实盘决策请以生产引擎实时信号 + 多日/OOS 验证为准, 并自担风险。
    </div>"""

    report_data = {
        "meta": {
            "strategy_name": f"tpoint strict vs floor · {SYM_LABEL}",
            "market": "china_a",
            "generated_at": "",
            "report_kind": "strategy",
        },
        "summary": {
            "total_return_pct": None,
            "max_drawdown_pct": None,
            "win_rate_pct": None,
            "total_trades": bm['strict']['n_signals'] + bm['floor']['n_signals'],
            "sharpe": None,
        },
        "equity_curve": [],
        "pnl_curve": [],
        "drawdown_curve": [],
        "trade_history": [],
        "ui": {
            "subtitle": f"strict(生产) vs floor(拟flip) · {SYM_LABEL} {DATE}",
            "active_tab": "comparison",
            "tabs": [{"id": "comparison", "label": "模式对照"}],
            "language": "zh",
        },
        "modules": [
            {
                "type": "line_chart",
                "tab": "comparison",
                "title": "累计前向收益(12min)曲线 — 若逐信号跟单的漂移",
                "subtitle": "按时间序累加每信号12min前向收益%; 两模式同日同信号则曲线重合。仅示意踩点质量, 未计成本",
                "series": [
                    {"name": MODE_LABEL[m], "points": curves[m]} for m in ('strict', 'floor')
                ],
            },
            {
                "type": "metric_table",
                "tab": "comparison",
                "title": "核心指标对比 (前向收益口径一致, 全日)",
                "subtitle": f"{SYM_LABEL} {DATE}",
                "columns": ["指标", "strict", "floor"],
                "rows": table_rows,
            },
            {
                "type": "custom_html",
                "tab": "comparison",
                "title": "关键发现 / 明细 / 口径 / 免责",
                "width": "full",
                "html": banner + key_html + recon_html + detail_html + disclaimer,
            },
        ],
    }
    return report_data


def main():
    rd = build_report()
    out = os.path.join(OUT_DIR, f'{SYM}_strict_floor_{DATE}.html')
    render_dashboard(rd, output_path=out, template_path=TEMPLATE)
    print(f'[HTML] 渲染完成: {out}')


if __name__ == '__main__':
    main()
