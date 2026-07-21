#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_gateway_html.py — 把 compare_macd_gate.py 的 JSON 产物渲染为沙箱对比 HTML 仪表盘。
独立渲染脚本: 只读 output/sandbox_gateway_summary.json (+ 三个模式 _summary.json), 写出 HTML。
不影响生产。
"""
import os, json, argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'output')

MODE_LABEL = {
    'strict': 'strict · 生产默认(MACD背离必选)',
    'off': 'off · 纯引力(无视MACD)',
    'floor': 'floor · strict+价格地板/天花板',
}


def fmt(x, pct=False, nd=2):
    if x is None:
        return '—'
    if pct:
        return f'{x*100:.{nd}f}%'
    return f'{x:.{nd}f}'


def render(meta, modes):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    # 对比表行
    rows = []
    for m in ['strict', 'off', 'floor']:
        s = modes.get(m, {})
        rows.append(f"""
        <tr>
          <td class="mode">{MODE_LABEL[m]}</td>
          <td>{s.get('n_signals','—')}</td>
          <td>{s.get('nB','—')}/{s.get('nS','—')}</td>
          <td>{fmt(s.get('dir_accuracy'), pct=True)}</td>
          <td>{fmt(s.get('mean_fwd_B',{}).get(12))}</td>
          <td>{fmt(s.get('mean_fwd_S',{}).get(12))}</td>
          <td>{s.get('n_roundtrip','—')}</td>
          <td>{fmt(s.get('mean_net_T'))}</td>
          <td>{fmt(s.get('win_rate_T'), pct=True)}</td>
          <td>{fmt(s.get('net_T_per_signal'))}</td>
          <td>{fmt(s.get('final_cum_net_pct'))}%</td>
        </tr>""")

    # 因子归因面板
    fac_panels = []
    for m in ['strict', 'off', 'floor']:
        fa = modes.get(m, {}).get('factor_attribution', {})
        cells = []
        for key, label in [('g', '引力 g'), ('vd', '量价 vd'), ('md', 'MACD md')]:
            r = fa.get(key, {})
            cells.append(f"""
              <tr><td>{label}</td>
                  <td>{r.get('n_on','—')}</td>
                  <td>{fmt(r.get('mean_fwd12_on'))}</td>
                  <td>{r.get('n_off','—')}</td>
                  <td>{fmt(r.get('mean_fwd12_off'))}</td></tr>""")
        fac_panels.append(f"""
        <div class="panel">
          <h3>{MODE_LABEL[m]}</h3>
          <table class="mini">
            <thead><tr><th>因子</th><th>命中n</th><th>命中时fwd12</th><th>未命中n</th><th>未命中fwd12</th></tr></thead>
            <tbody>{''.join(cells)}</tbody>
          </table>
        </div>""")

    # 信号量柱 (nB/nS)
    def bar(val, maxv, color):
        w = (val / maxv * 100) if maxv else 0
        return f'<div class="bar" style="width:{w:.1f}%;background:{color}"></div>'
    maxsig = max((modes[m].get('n_signals', 0) for m in modes), default=1) or 1
    sig_bars = []
    for m in ['strict', 'off', 'floor']:
        s = modes.get(m, {})
        nB, nS = s.get('nB', 0), s.get('nS', 0)
        sig_bars.append(f"""
        <div class="sigrow">
          <div class="siglabel">{m}</div>
          <div class="sigbars">
            <div>B {nB} {bar(nB, maxsig, '#3fb950')}</div>
            <div>S {nS} {bar(nS, maxsig, '#f85149')}</div>
          </div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tpoint 三风格沙箱回测对比</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --bd:#30363d; --tx:#e6edf3; --mut:#8b949e;
          --grn:#3fb950; --red:#f85149; --yel:#d29922; --blu:#58a6ff; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--tx); font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
         margin:0; padding:24px; line-height:1.5; }}
  .banner {{ background:linear-gradient(90deg,#1f2d1f,#161b22); border:1px solid var(--grn); border-radius:10px;
            padding:14px 18px; margin-bottom:20px; }}
  .banner .tag {{ display:inline-block; background:var(--grn); color:#04130a; font-weight:700; padding:2px 10px;
                 border-radius:6px; font-size:12px; letter-spacing:1px; }}
  .banner h1 {{ margin:8px 0 4px; font-size:20px; }}
  .banner .meta {{ color:var(--mut); font-size:13px; }}
  h2 {{ font-size:16px; margin:26px 0 10px; border-left:3px solid var(--blu); padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--bd);
          border-radius:8px; overflow:hidden; font-size:13px; }}
  th,td {{ padding:9px 10px; text-align:center; border-bottom:1px solid var(--bd); }}
  th {{ background:#1c2230; color:var(--mut); font-weight:600; }}
  td.mode {{ text-align:left; color:var(--blu); font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  .panel {{ background:var(--card); border:1px solid var(--bd); border-radius:8px; padding:12px 14px; margin-bottom:12px; }}
  .panel h3 {{ margin:0 0 8px; font-size:14px; color:var(--blu); }}
  table.mini th, table.mini td {{ padding:6px 8px; font-size:12px; }}
  .grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }}
  .sigrow {{ display:flex; align-items:center; gap:10px; margin:6px 0; }}
  .siglabel {{ width:64px; color:var(--mut); font-size:12px; }}
  .sigbars {{ flex:1; }}
  .sigbars > div {{ display:flex; align-items:center; gap:8px; font-size:12px; margin:3px 0; }}
  .bar {{ height:14px; border-radius:4px; min-width:2px; }}
  .note {{ background:#1a1f29; border:1px dashed var(--yel); border-radius:8px; padding:12px 16px;
          font-size:13px; color:#d8c08a; }}
  .note b {{ color:var(--yel); }}
  .disc {{ margin-top:24px; font-size:12px; color:var(--mut); border-top:1px solid var(--bd); padding-top:12px; }}
  .hl {{ color:var(--yel); font-weight:700; }}
</style></head>
<body>
  <div class="banner">
    <span class="tag">SANDBOX · 沙箱回测</span>
    <h1>tpoint 三风格 MACD 门控回测对比</h1>
    <div class="meta">生成时间 {now} ｜ 范围: {meta.get('scope','')} ｜ 单边成本 {meta.get('trade_cost_pct_per_leg','')}% ｜ 前向窗口 {meta.get('horizons','')}</div>
    <div class="meta"><b style="color:var(--grn)">隔离声明:</b> {meta.get('isolation','')} — 本结果<b>不影响现生产</b>任何配置或推送。</div>
  </div>

  <h2>① 三模式核心指标对比</h2>
  <table>
    <thead><tr><th>模式</th><th>信号数</th><th>B/S</th><th>方向准确率</th>
      <th>B后fwd12均值</th><th>S后fwd12均值</th><th>T回合</th><th>均净T%</th>
      <th>T胜率</th><th>每信号净T</th><th>累计净T%</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div class="note">
    <b>怎么读:</b> 方向准确率 = B信号后12min价涨 且 S信号后12min价跌 的比例。
    均净T% = 每回合(次根K成交, 扣双边成本)净收益均值。每信号净T = 累计净T / 信号数,
    反映"多发信号是否真增收益"。<span class="hl">当前样本准确率勉强 45–47%, 仅略高于随机, 三模式均未展现稳定alpha</span> —
    详见下方方法论警示。
  </div>

  <h2>② 信号量分布 (B=绿 / S=红)</h2>
  <div class="panel">{''.join(sig_bars)}</div>

  <h2>③ 因子归因 (买点 fwd12 均值: 命中 vs 未命中)</h2>
  <div class="grid3">{''.join(fac_panels)}</div>
  <div class="note">
    <b>读图:</b> 若某因子"命中时 fwd12"显著高于"未命中时", 说明该因子确有选股/择时增量;
    若两者接近, 该因子在当前样本上无区分度。strict 模式 MACD(md) 命中应有更高 fwd12 才支撑其保守设计。
  </div>

  <h2>④ 方法论警示 (务必先看)</h2>
  <div class="note">
    <b>1. 成交假设:</b> T回合按"信号K收盘触发 → 次根K收盘成交"模拟, 已含 1 根K执行滞后与双边成本。
    若按信号价当日成交(无滞后)会显著高估收益(接近机械均值回归套利, 胜率虚高)。
    <br><b>2. 均值回归套利的 tautology:</b> 引力信号本就在价格偏离VWAP时触发, 配对其自身B/S天然捕获回归,
    故"做T收益"含方法学内生性, 不能直接当作可实盘复制的净alpha。
    <br><b>3. 分段处理:</b> 每只标的CSV含多交易日, 已按 trade_date 独立分段跑引擎, 避免VWAP跨日累积污染与每日信号上限被跨日共享。
    <br><b>4. 样本偏差:</b> 当前为 {meta.get('scope','')} 的历史1m; 未做样本外时间切分, 含前视风险。结论仅作风格差异参考, 非收益承诺。
    <br><b>5. 成本/容量:</b> 假设单边 {meta.get('trade_cost_pct_per_leg','')}%, 未计滑点/冲击成本; 高频多回合假设单一仓位顺序成交, 未计并发容量限制。
  </div>

  <div class="disc">
    ⚠️ 免责声明: 本页面为 tpoint 做T策略的<b>沙箱研究产物</b>, 与现生产监控/推送完全隔离。
    所有数字基于历史离线1m数据与简化成交假设, 含模型内生性与样本偏差, <b>不构成任何投资建议或收益承诺</b>。
    实盘决策请以生产引擎实时信号为准, 并自担风险。
  </div>
</body></html>"""
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=OUT_DIR)
    args = ap.parse_args()
    sum_path = os.path.join(args.out, 'sandbox_gateway_summary.json')
    with open(sum_path, encoding='utf-8') as f:
        blob = json.load(f)
    meta = blob.get('meta', {})
    modes = blob.get('modes', {})
    html = render(meta, modes)
    out_html = os.path.join(args.out, 'sandbox_gateway_compare.html')
    with open(out_html, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[HTML] 写出 {out_html} ({len(html)} bytes)')


if __name__ == '__main__':
    main()
