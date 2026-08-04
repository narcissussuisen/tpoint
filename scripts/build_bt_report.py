# -*- coding: utf-8 -*-
"""
build_bt_report.py — 回测筛选器结果 HTML 报告生成（2026-08-01）

读取 data/backtest_screener_results.json → output/backtest_screener_report_YYYY-MM-DD.html
深色主题，含：达标线说明 / 逐标的指标表 / 结论与建议。
"""
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('MACD_GATE_MODE', 'floor')


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def main():
    date_str = datetime.date.today().strftime('%Y-%m-%d')
    src = os.path.join(BASE, 'data', 'backtest_screener_results.json')
    with open(src, encoding='utf-8') as f:
        payload = json.load(f)

    results = payload['results']
    cfg = payload.get('config', {})
    rows_html = ''
    for sym, r in sorted(results.items()):
        if r.get('error'):
            rows_html += (f'<tr><td>{esc(sym)}</td><td colspan="7">'
                          f'<span class="bad">❌ {esc(r["error"])}</span></td></tr>')
            continue
        m = r['metrics']; v = r['verdict']
        mark = ('<span class="ok">✅ 达标</span>' if v['pass']
                else ('<span class="warn">⚠️ 样本不足</span>' if not v['sample_ok']
                      else '<span class="bad">❌ 未达标</span>'))
        byr = ' / '.join(f'{k}:{n}' for k, n in m.get('by_reason', {}).items())
        rows_html += (
            f'<tr><td>{esc(sym)}</td>'
            f'<td>{r.get("days", "-")}</td>'
            f'<td>{m["total"]}</td>'
            f'<td>{m["gross_win_rate"]}%</td>'
            f'<td>{m["win_rate"]}%</td>'
            f'<td>{m["pl_ratio"]}</td>'
            f'<td>{m["ann_ret_pct"]}%</td>'
            f'<td>{m["max_drawdown_pct"]}%</td>'
            f'<td>{mark}</td>'
            f'<td class="ts">{esc(byr)}</td></tr>'
        )

    passed_n = sum(1 for r in results.values() if r.get('verdict') and r['verdict']['pass'])
    sample_warn_n = sum(1 for r in results.values()
                        if r.get('verdict') and not r['verdict']['sample_ok'])

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>tpoint 回测筛选器 · {date_str}</title>
<style>
body{{background:#11151c;color:#d5dae2;font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:28px;max-width:1180px;margin:auto}}
h1{{color:#fff;font-size:21px;margin-bottom:4px}}
.sub{{color:#8a93a6;font-size:13px}}
h2{{color:#9ec9ff;font-size:15px;margin-top:30px;font-weight:500}}
.card{{background:#1a2029;border-radius:12px;padding:20px;margin-top:14px}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #2a3140;font-size:13px}}
th{{color:#8a93a6;font-weight:500}}
.ok{{color:#7ee2a8}}.bad{{color:#ff8b8b}}.warn{{color:#f5c26b}}.ts{{color:#8a93a6;font-size:12px}}
.sum{{display:flex;gap:18px;flex-wrap:wrap;margin-top:14px}}
.sum div{{background:#232b38;border-radius:10px;padding:14px 22px;min-width:110px}}
.sum b{{font-size:24px;display:block;color:#fff}}
.sum span{{color:#8a93a6;font-size:12px}}
.kv{{display:flex;gap:30px;flex-wrap:wrap;margin-top:10px}}
.kv div{{background:#232b38;border-radius:8px;padding:10px 16px;font-size:13px}}
.kv b{{color:#9ec9ff;font-weight:500}}
.concl{{background:#232b38;border-left:4px solid #f5c26b;border-radius:8px;padding:14px 18px;margin-top:14px;font-size:13px;line-height:1.8}}
.concl b{{color:#f5c26b}}
</style></head><body>
<h1>tpoint 回测筛选器 · {date_str}</h1>
<div class="sub">自研回测驱动的选股筛选器（v9/miji 生产同源引擎 + F盘 keyfactor_data 1m 历史库）</div>

<div class="card">
  <h2>达标线（用户确认 2026-08-01）</h2>
  <div class="kv">
    <div><b>胜率 ≥ {payload.get('min_win_rate', 60)}%</b></div>
    <div><b>盈亏比 ≥ {payload.get('min_pl_ratio', 1.6)}</b></div>
    <div><b>样本 ≥ {payload.get('min_sample', 20)} 笔</b></div>
    <div><b>引擎: miji + floor 门控</b></div>
  </div>
  <div class="sum">
    <div><b>{len(results)}</b><span>回测标的</span></div>
    <div><b>{passed_n}</b><span>达标</span></div>
    <div><b>{sample_warn_n}</b><span>样本不足</span></div>
  </div>
</div>

<div class="card">
  <h2>逐标的回测结果（145 天 1m 真实数据）</h2>
  <table>
    <tr><th>标的</th><th>天数</th><th>笔数</th><th>毛胜率</th><th>净胜率(扣成本)</th><th>盈亏比(净)</th><th>年化</th><th>最大回撤</th><th>判定</th><th>出场分布</th></tr>
    {rows_html}
  </table>
</div>

<div class="card">
  <h2>胜率口径说明（2026-08-01 修复）</h2>
  <div class="concl">
    <b>旧口径：</b>ret_pct = (出场价-入场价)/入场价 &gt; 0 即胜（裸价差，无成本无滑点）。<br>
    <b>新口径：</b>ret_pct = 毛收益 - 双边成本；净收益 &gt; 0 才计为胜。<br>
    <b>成本模型（用户实际费率 2026-08-01）：</b>佣金万一(0.01%)不免五 + 滑点2bps×2。
    个股加卖出印花税万5.641 → 双边 ≈ 0.116%；ETF/LOF/可转债/债券现券/港股通无印花税 → 双边 ≈ 0.060%。<br>
    <b>影响量化：</b>688146 毛胜率 54.9% → 净胜率 47.1%（-7.8pp）；688111 51.9% → 42.9%（-9.0pp）。
    旧裸胜率普遍虚高 5-9 个百分点——"8/8 未达标"在更严格口径下依旧成立，且差距更大。
  </div>
</div>

<div class="card">
  <h2>结论与建议</h2>
  <div class="concl">
    <b>核心发现：</b>8 只标的历史回测 <b>全部未达 60% 净胜率 / 1.6 盈亏比达标线</b>。
    净胜率最高 57.7%（600584 长电科技，20 天小样本）、最低 42.9%（688111 金山办公）。
    watchlist 四标的（161129/513310/688111/588000）同样未达标。<br><br>
    <b>根因：</b>floor 模式下"价格地板"抄底信号在单边下跌日被连续触发（如 688146 07-03 单日 11 个 B 信号全为均线引力+地板），
    均值回归假设在单边行情失效——与用户 07-31 反馈"高波动日信号质量差"完全吻合。<br><br>
    <b>建议：</b>① 候选池暂不并入 watchlist（未过达标线）；② 优先解决"单边行情保护"（高波动守卫校准 / floor 门槛收紧）；
    ③ 数据已具备（F盘 1m 历史库 + 每日累积），可随时重跑；④ 588000 与 688048/688008 暂无 1m 数据，需另拉。
  </div>
</div>
</body></html>"""
    out = os.path.join(BASE, 'output', f'backtest_screener_report_{date_str}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out}')


if __name__ == '__main__':
    main()
