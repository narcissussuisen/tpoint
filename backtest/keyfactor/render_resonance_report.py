#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 output/resonance_v930_report.json 渲染成 HTML 对比报告。

输出: output/resonance_v930_report.html
"""
import json
import os

IN_JSON = os.path.join('output', 'resonance_v930_report.json')
OUT_HTML = os.path.join('output', 'resonance_v930_report.html')


def _bar(label, val, total, color):
    pct = val / total * 100 if total else 0
    return (f'<div class="bar-row"><span class="bar-label">{label}</span>'
            f'<div class="bar-wrap"><div class="bar" style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<span class="bar-val">{val:,} ({pct:.1f}%)</span></div>')


def render(json_path=IN_JSON, out_path=OUT_HTML):
    with open(json_path, 'r', encoding='utf-8') as f:
        report = json.load(f)

    cfg = report['config']
    agg = report['aggregate']
    per_stock = report.get('per_stock', {})

    modes = ['strict', 'floor', 'resonance']
    colors = {'strict': '#60a5fa', 'floor': '#34d399', 'resonance': '#f87171'}

    # 聚合表
    rows = []
    for mode in modes:
        a = agg[mode]
        rows.append(f"""
        <tr>
          <td><strong>{mode}</strong></td>
          <td>{a['n_signals']:,}</td>
          <td>{a['n_B']:,}</td>
          <td>{a['n_S']:,}</td>
          <td>{a['skill']['skill6']:+.4f}%</td>
          <td>{a['skill']['skill12']:+.4f}%</td>
          <td>{a['skill']['skill24']:+.4f}%</td>
        </tr>
        """)

    # 共振分数分布
    score_sections = []
    for mode in modes:
        a = agg[mode]
        total = a['n_signals']
        parts = []
        for t in ('B', 'S'):
            for score, cnt in sorted(a['score_dist'][t].items()):
                parts.append(_bar(f'{t} score={score}', cnt, total, colors[mode]))
        score_sections.append(f"""
        <div class="card">
          <h3>{mode} — 共振分数分布</h3>
          {''.join(parts)}
        </div>
        """)

    # 因子组合
    combo_sections = []
    for mode in modes:
        a = agg[mode]
        total = a['n_signals']
        parts = []
        for combo, counts in sorted(a['factor_breakdown'].items(), key=lambda x: -(x[1]['B'] + x[1]['S']))[:8]:
            cnt = counts['B'] + counts['S']
            parts.append(_bar(f"{combo} (B={counts['B']}, S={counts['S']})", cnt, total, colors[mode]))
        combo_sections.append(f"""
        <div class="card">
          <h3>{mode} — 因子组合 TOP8</h3>
          {''.join(parts)}
        </div>
        """)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>v9.3.0 三因子共振评估报告</title>
<style>
  :root {{
    --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
    --accent: #38bdf8; --border: #334155;
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 2rem; }}
  h1, h2, h3 {{ color: var(--accent); margin-top: 0; }}
  h1 {{ font-size: 1.8rem; border-bottom: 1px solid var(--border); padding-bottom: .5rem; }}
  .meta {{ color: var(--muted); margin-bottom: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.2rem; margin: 1.5rem 0; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
  th, td {{ padding: .6rem .8rem; border-bottom: 1px solid var(--border); text-align: right; }}
  th {{ text-align: left; color: var(--muted); font-weight: 600; }}
  td:first-child, th:first-child {{ text-align: left; }}
  .bar-row {{ display: flex; align-items: center; gap: .6rem; margin: .35rem 0; font-size: .9rem; }}
  .bar-label {{ width: 130px; color: var(--muted); flex-shrink: 0; }}
  .bar-wrap {{ flex: 1; background: #334155; border-radius: 4px; height: 14px; overflow: hidden; }}
  .bar {{ height: 100%; border-radius: 4px; }}
  .bar-val {{ width: 100px; text-align: right; color: var(--text); }}
  .note {{ color: var(--muted); font-size: .9rem; margin-top: 2rem; line-height: 1.6; }}
</style>
</head>
<body>
  <h1>v9.3.0 三因子共振评估报告</h1>
  <div class="meta">
    数据目录: {cfg['data_dir']} &nbsp;|&nbsp; 标的数: {cfg['n_symbols']} &nbsp;|&nbsp;
    RESONANCE_THRESHOLD={cfg['RESONANCE_THRESHOLD']} &nbsp;|&nbsp;
    VWAP_DEV={cfg['VWAP_DEV_BUY']}/{cfg['VWAP_DEV_SELL']} &nbsp;|&nbsp;
    VOL_EXPAND/SHRINK={cfg['VOL_EXPAND_RATIO']}/{cfg['VOL_SHRINK_RATIO']}
  </div>

  <h2>1. 三种模式聚合对比</h2>
  <table>
    <thead>
      <tr><th>模式</th><th>总信号</th><th>B</th><th>S</th><th>skill6</th><th>skill12</th><th>skill24</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>

  <h2>2. 共振分数分布</h2>
  <div class="grid">
    {''.join(score_sections)}
  </div>

  <h2>3. 因子组合构成</h2>
  <div class="grid">
    {''.join(combo_sections)}
  </div>

  <div class="note">
    <strong>说明</strong><br>
    • strict/floor 保持 vol_div 关闭；resonance 模式下强制启用 vol_div 以构成三因子。<br>
    • skill 定义：B 信号 fwd>0 为好，S 信号 fwd&lt;0 为好；数值为符号调整后的前向收益均值(%)。<br>
    • 早盘 i&lt;LOCAL_W({cfg['LOCAL_W']}) 均降级为 gravity-only，与生产行为一致。<br>
    • 本报告为研究分支输出，不用于生产决策。
  </div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  落地: {out_path}')


if __name__ == '__main__':
    render()
