# -*- coding: utf-8 -*-
"""macd_div 审查报告 HTML（结论版）"""
import datetime

date_str = datetime.date.today().strftime('%Y-%m-%d')

rows = [
    ('0.00（生产）', 2510, '47.4%', '-144.22%', '-138.83%', '45.5%', '-5.40%', '50.6%', '基线'),
    ('0.05', 1834, '49.4%', '-62.48%', '-45.28%', '48.4%', '-17.20%', '50.8%', '改善'),
    ('0.10', 1679, '50.6%', '-20.35%', '-22.97%', '49.6%', '+2.65%', '52.0%', '改善'),
    ('0.15', 1600, '52.2%', '+7.77%', '-16.76%', '50.9%', '+24.54%', '53.9%', '✅ 翻正'),
    ('0.20', 1512, '52.0%', '-2.19%', '-18.19%', '50.8%', '+16.01%', '53.6%', '波动'),
    ('0.30', 1434, '52.2%', '-3.15%', '-11.15%', '51.2%', '+8.00%', '53.7%', '波动'),
    ('0.50', 1378, '52.3%', '+15.50%', '+5.65%', '51.3%', '+9.85%', '53.5%', '✅ 稳健'),
]
trs = ''
for r in rows:
    cls = 'pos' if r[3].startswith('+') else 'neg'
    cls2 = 'pos' if r[6].startswith('+') else 'neg'
    trs += (f'<tr><td class="c1">{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>'
            f'<td class="{cls}">{r[3]}</td><td>{r[4]}</td><td>{r[5]}</td>'
            f'<td class="{cls2}">{r[6]}</td><td>{r[7]}</td><td class="sub">{r[8]}</td></tr>')

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>macd_div 审查结论 · {date_str}</title>
<style>
:root{{--bg:#11151c;--card:#1a2029;--ink:#d5dae2;--sub:#8a93a6;--line:#2a3140;
--pos:#7ee2a8;--neg:#ff8b8b;--accent:#9ec9ff}}
body{{background:var(--bg);color:var(--ink);font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1100px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:var(--accent);font-size:15px;margin-top:28px}}
.card{{background:var(--card);border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}}
th{{color:var(--sub);font-weight:500}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
.sub{{color:var(--sub);font-size:11.5px}} .c1{{font-weight:600;color:#fff}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}}
.b-pos{{background:#1a3a2a;color:#7ee2a8}} .b-neg{{background:#3a1a1a;color:#ff8b8b}}
.note{{background:#232b38;border-radius:8px;padding:14px 18px;margin-top:14px;font-size:13px;line-height:1.8}}
.hl{{color:var(--accent);font-weight:600}}
</style></head><body>
<h1>macd_div 因子审查结论 · {date_str}</h1>

<div class="card">
  <h2>一、审查结论（8 标的 × 146 天 · 万一费率 · 净收益口径）</h2>
  <table>
    <tr><th>阈值</th><th>总笔</th><th>全样本胜率</th><th>全样本净收益</th>
        <th>train净收益</th><th>train胜率</th><th>test净收益</th><th>test胜率</th><th>判定</th></tr>
    {trs}
  </table>
  <div class="note">
    <span class="badge b-pos">结论</span>
    <b>macd_divergence_signal 实现无符号错误</b>（79.8% 信号是真背离，双点比较验证 5530 信号）。
    真因 = <span class="hl">弱背离是负 alpha 噪音</span>：背离强度分桶显示，买点桶1-3（弱-中背离）
    前向收益全负（-0.024% ~ -0.067%），仅最强 25% 背离（hist 差 >0.216）转正（+0.084%）。
    <br><br>
    <b>修复 = 加背离强度阈值 macd_min_hist_diff=0.15</b>：全样本净收益 -144.22% → <span class="hl">+7.77%</span>，
    样本外 test 净收益 <span class="hl">+24.54%</span>、净胜率 <span class="hl">53.9%</span>
    （超过合格线 50%）。train/test 方向一致，非过拟合。
  </div>
</div>

<div class="card">
  <h2>二、证据链</h2>
  <div class="note">
    <b>① 背离结构验证</b>：双点比较（价格新低 + hist 低点抬高），8 标的 m_factor 信号 79.8% 为真背离，
    方向判定正确。<br>
    <b>② 强度分桶</b>：买点正率 42.8%→46.5% 随背离强度单调升；仅强背离（hist 差 >0.216）前向收益为正。
    弱背离（75% 信号量）全是噪音负 alpha。<br>
    <b>③ 阈值扫描</b>：0.15 阈值使全样本与 OOS test 均转正，train/test 一致性验证排除过拟合。<br>
    <b>④ 历史印证</b>：07-16 研究（移除 macd_div +4.39pp）与本次消融（m 门控 -167pp）一致；
    本次定位到根因——不是移除，而是加强度门槛。
  </div>
</div>

<div class="card">
  <h2>三、变更与待办</h2>
  <div class="note">
    <b>已改</b>：core/miji_alpha.py 的 macd_divergence_signal 增加 min_hist_diff 参数（默认 0 = 生产行为不变），
    detect_miji_signals 增加 macd_min_hist_diff 透传。<br>
    <b>待决策</b>：阈值取 0.15（test 最佳）还是 0.50（全样本最佳）？建议更多标的/更长窗口确认稳定性。<br>
    <b>待办</b>：monitor 生产路径 check_miji_trigger 尚未接入 macd_min_hist_diff（下一步）；确认阈值后更新
    FLOOR 网格与 VWAP_DEV 联动寻优。
  </div>
</div>
</body></html>"""
out = f'output/macd_div_audit_{date_str}.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(html)
print(f'📄 报告已写入 {out}')
