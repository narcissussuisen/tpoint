#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_gateway_dashboard.py — 通过 render_dashboard() + dashboard_template.html
渲染三种 MACD 门控沙箱对比仪表盘 (符合 quant-backtest-lab 的 Iron rule:
所有 HTML 必须经 render_dashboard 渲染, 禁止手写导航/落地页)。

采用 skill 规定的 peer-comparison 布局:
  - line_chart: 三模式累计净T%曲线 (同台对比, 不抬升任一为主线)
  - metric_table: 三列(KPI 对比)
  - custom_html: 因子归因 + 方法论警示 + SANDBOX 隔离声明 + 免责声明

注意: 本任务为分钟级(1m)做T对比, 属 skill 默认框架的 out-of-scope(仅支持日频以下);
故不使用 overview_chart(组合NAV语义) 与标准 trades_table(需 entry/exit 日期与股本),
而用通用模块表达, 避免对 1m 回合指标的错误呈现。
"""
import os, sys, json
sys.path.insert(0, r'C:\Users\YZP\.workbuddy\plugins\marketplaces\experts\plugins\strategy-backtest-expert\skills\quant-backtest-lab\reference')
from render_dashboard import render_dashboard
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'output')
TEMPLATE = r'C:\Users\YZP\.workbuddy\plugins\marketplaces\experts\plugins\strategy-backtest-expert\skills\quant-backtest-lab\reference\dashboard_template.html'

MODE_LABEL = {
    'strict': 'strict · 生产默认',
    'off': 'off · 纯引力',
    'floor': 'floor · 价格地板',
}
MODE_ORDER = ['strict', 'off', 'floor']

# 每条曲线上限点数: 三模式合计需远低于 skill 的 5000 白屏阈值
MAX_POINTS_PER_CURVE = 1500


def build_curve(mode):
    eq = pd.read_csv(os.path.join(OUT_DIR, f'sandbox_gateway_{mode}_equity.csv'))
    pts = [{'date': str(r['date']), 'value': float(r['cum_net_pct'])} for _, r in eq.iterrows()]
    n = len(pts)
    if n > MAX_POINTS_PER_CURVE:
        step = (n - 1) / (MAX_POINTS_PER_CURVE - 1)
        idxs = sorted({0, n - 1} | {int(round(i * step)) for i in range(MAX_POINTS_PER_CURVE)})
        pts = [pts[i] for i in idxs]
    return pts


def build_report():
    with open(os.path.join(OUT_DIR, 'sandbox_gateway_summary.json'), encoding='utf-8') as f:
        blob = json.load(f)
    meta = blob['meta']
    modes = blob['modes']

    curves = {m: build_curve(m) for m in MODE_ORDER}

    # KPI 对比表
    def g(m, k):
        return modes[m].get(k)
    def f4(x):
        if x is None:
            return '—'
        try:
            return f'{float(x):.4f}'
        except (TypeError, ValueError):
            return str(x)
    def fwd(m, key):
        d = g(m, key)
        v = d.get('12') if isinstance(d, dict) else None
        return f4(v) + '%'
    rows = []
    def row(metric, fn):
        rows.append({'metric': metric, 'values': [{'main': fn(m)} for m in MODE_ORDER]})
    row('信号数', lambda m: f"{g(m,'n_signals'):,}")
    row('B / S', lambda m: f"{g(m,'nB'):,} / {g(m,'nS'):,}")
    row('方向准确率', lambda m: f"{g(m,'dir_accuracy')*100:.1f}%")
    row('B后12min fwd均值', lambda m: fwd(m, 'mean_fwd_B'))
    row('S后12min fwd均值', lambda m: fwd(m, 'mean_fwd_S'))
    row('T回合数', lambda m: f"{g(m,'n_roundtrip'):,}")
    row('均净T%', lambda m: f"{g(m,'mean_net_T'):.3f}%")
    row('T胜率', lambda m: f"{g(m,'win_rate_T')*100:.1f}%")
    row('每信号净T', lambda m: f"{g(m,'net_T_per_signal'):.4f}")
    row('累计净T%', lambda m: f"{g(m,'final_cum_net_pct'):,.0f}%")

    # 因子归因 (custom_html)
    def f4(x):
        if x is None:
            return '—'
        try:
            return f'{float(x):.4f}'
        except (TypeError, ValueError):
            return str(x)

    fac_cells = []
    for m in MODE_ORDER:
        fa = modes[m].get('factor_attribution', {})
        g_on = fa.get('g', {}).get('mean_fwd12_on')
        g_off = fa.get('g', {}).get('mean_fwd12_off')
        md_on = fa.get('md', {}).get('mean_fwd12_on')
        md_off = fa.get('md', {}).get('mean_fwd12_off')
        fac_cells.append(f"""
          <tr><td>{MODE_LABEL[m]}</td>
              <td>{f4(g_on)}</td>
              <td>{f4(g_off)}</td>
              <td style="color:#f85149">{f4(md_on)}</td>
              <td style="color:#3fb950">{f4(md_off)}</td></tr>""")

    attribution_html = f"""
    <div class="bt-custom-gate-note">
      <p>买点(引力触发B) 后12min前向收益均值: 引力因子命中 vs 未命中, 以及 <b>MACD背离(md)因子命中 vs 未命中</b>。
      关键发现: <b style="color:#f85149">三模式下 md 命中时 fwd12 均为负、未命中时为正</b> —— 即本样本中 MACD 背离因子呈<b>反向预测</b>,
      strict 模式强制要求 md, 反而筛掉了表现更好的子集。</p>
      <table class="bt-custom-gate-tbl">
        <thead><tr><th>模式</th><th>引力命中 fwd12</th><th>引力未命中 fwd12</th><th>md命中 fwd12</th><th>md未命中 fwd12</th></tr></thead>
        <tbody>{''.join(fac_cells)}</tbody>
      </table>
    </div>"""

    caveats_html = """
    <div class="bt-custom-gate-note">
      <p><b>方法论警示 (必读):</b></p>
      <ol>
        <li><b>成交假设:</b> T回合按"信号K收盘触发 → 次根K收盘成交"模拟(1根K滞后)+ 双边成本(单边0.02%)。若按信号价当日成交会显著高估(接近机械均值回归套利, 胜率虚高)。</li>
        <li><b>均值回归 tautology:</b> 引力信号本就在价格偏离VWAP处触发, 配对其自身B/S天然捕获回归, 故"做T收益"含方法学内生性, <b>不能直接当作可实盘复制的净alpha</b>。T胜率80–94%实为该内生动量的映射, 非真实可捕获胜率。</li>
        <li><b>信号方向准确率仅45–47%</b> (≈抛硬币, 略低于随机), 说明三模式均<b>未展现稳健方向alpha</b>; 单看T净收益会被上述内生性放大。</li>
        <li><b>分段处理:</b> 每只标的CSV含多交易日, 已按 trade_date 独立分段跑引擎, 避免VWAP跨日累积污染与每日信号上限跨日共享。</li>
        <li><b>样本偏差:</b> 106只标的(优先级6+随机100)的历史1m, 未做样本外时间切分, 含前视风险; 基金/ETF与股票混用, 复权口径未逐源校验。</li>
        <li><b>容量/成本:</b> 假设单一仓位顺序成交, 未计并发容量、滑点与冲击成本; 高频多回合假设理想撮合。</li>
      </ol>
    </div>"""

    banner_html = """
    <div class="bt-custom-gate-banner">
      <b>SANDBOX · 沙箱回测</b> — 与现生产监控/推送完全隔离。仅读 keyfactor_data/1m, 仅写 output/, 不读生产配置/数据源。
      本结果为研究产物, 不构成投资建议。
    </div>"""

    disclaimer_html = """
    <div class="bt-custom-gate-note" style="color:#8b949e;font-size:12px;border-top:1px solid #30363d;padding-top:10px;margin-top:10px">
      ⚠️ 免责声明: 本页面为 tpoint 做T策略的沙箱研究产物, 与现生产配置隔离。所有数字基于历史离线1m数据与简化成交假设,
      含模型内生性与样本偏差, 不构成任何投资建议或收益承诺。实盘决策请以生产引擎实时信号为准, 并自担风险。
    </div>"""

    report_data = {
        "meta": {
            "strategy_name": "tpoint 三风格MACD门控沙箱对比",
            "market": "china_a",
            "generated_at": meta.get('generated_at', ''),
            "report_kind": "strategy",
        },
        "summary": {
            "total_return_pct": modes['floor'].get('final_cum_net_pct'),
            "max_drawdown_pct": None,
            "win_rate_pct": modes['floor'].get('win_rate_T') * 100,
            "total_trades": modes['floor'].get('n_roundtrip'),
            "sharpe": None,
        },
        "equity_curve": [],
        "pnl_curve": [],
        "drawdown_curve": [],
        "trade_history": [],
        "ui": {
            "subtitle": "三模式 peer-comparison (分钟级做T, 框架out-of-scope故用通用模块)",
            "active_tab": "comparison",
            "tabs": [{"id": "comparison", "label": "模式对比"}],
            "language": "zh",
        },
        "modules": [
            {
                "type": "line_chart",
                "tab": "comparison",
                "title": "累计净T%曲线 (三模式同台对比)",
                "subtitle": "次根K成交 + 双边成本; 曲线为按时间序累加的回合净收益%",
                "series": [
                    {"name": MODE_LABEL[m], "points": curves[m]} for m in MODE_ORDER
                ],
            },
            {
                "type": "metric_table",
                "tab": "comparison",
                "title": "核心指标对比",
                "subtitle": "三模式横向对比 (strict=生产默认)",
                "columns": ["指标", "strict", "off", "floor"],
                "rows": rows,
            },
            {
                "type": "custom_html",
                "tab": "comparison",
                "title": "隔离声明 / 因子归因 / 方法论",
                "width": "full",
                "html": banner_html + attribution_html + caveats_html + disclaimer_html,
            },
        ],
    }
    return report_data


def main():
    rd = build_report()
    out = os.path.join(OUT_DIR, 'index.html')
    render_dashboard(rd, output_path=out, template_path=TEMPLATE)
    print(f'[HTML] 模板渲染完成: {out}')


if __name__ == '__main__':
    main()
