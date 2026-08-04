#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""build_daily_report_html.py — 合并「每日复盘 + 每日对账」为单一自包含 HTML（2026-08-04 新增）

背景：原流水线分别生成 review_<D>.html 与 reconcile_<D>.html 两个文件并分别推送，
用户要求合并为一份完整报告（不分成两个独立文件）。本脚本在两份产物基础上合并：
  Part A = 每日复盘（信号清单/有效性/绩效/行情图，图表 base64 内嵌保留）
  Part B = 每日对账（生产vs回测 信号对比/推送明细/round-trip）
衔接：统一深色头部 + 目录锚点导航（#partA/#partB），两部分原始内容与样式全部保留。
样式冲突处理：review 与 reconcile 有 5 个同名类（bad/foot/ok/sub/warn），
Part B 的 CSS 选择器统一加 #partB 前缀做命名空间隔离，Part A 样式原样。

CLI：python build_daily_report_html.py [YYYY-MM-DD]（缺省=今天）
产物：output/daily_report_<D>.html
"""
import os
import re
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')


def extract(path):
    """返回 (css_list, body_inner)。"""
    h = open(path, encoding='utf-8').read()
    css = re.findall(r'<style[^>]*>(.*?)</style>', h, re.S)
    m = re.search(r'<body[^>]*>(.*)</body>', h, re.S)
    return css, (m.group(1) if m else h)


def namespace_css(css, scope):
    """给每条规则的选择器加 scope 前缀（#partB），@media 内层简单展开处理。"""
    out = []
    for rule in css.split('}'):
        if '{' not in rule:
            continue
        sel, body = rule.split('{', 1)
        sel = sel.strip()
        if not sel:
            continue
        if sel.startswith('@'):  # @media/@keyframes 等：不嵌套前缀，原样保留
            out.append(sel + '{' + body + '}')
            continue
        parts = [scope + ' ' + s.strip() for s in sel.split(',') if s.strip()]
        out.append(', '.join(parts) + ' {' + body + '}')
    return '\n'.join(out)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
    css_a, body_a = extract(os.path.join(OUT, f'review_{date}.html'))
    css_b, body_b = extract(os.path.join(OUT, f'reconcile_{date}.html'))
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    base_css = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.masthead{max-width:1100px;margin:0 auto;padding:28px 20px 8px;border-bottom:2px solid #30363d}
.masthead h1{font-size:22px;color:#e6edf3}
.masthead .meta{color:#8b949e;font-size:12px;margin-top:6px}
.toc{max-width:1100px;margin:14px auto;padding:0 20px;display:flex;gap:10px;flex-wrap:wrap}
.toc a{color:#58a6ff;text-decoration:none;border:1px solid #30363d;border-radius:6px;padding:6px 14px;background:#161b22;font-size:13px}
.toc a:hover{background:#1f6feb;color:#fff}
.part-title{max-width:1100px;margin:26px auto 0;padding:10px 20px;font-size:17px;color:#e6edf3;border-left:4px solid #1f6feb;background:#161b22}
hr.sep{max-width:1100px;margin:26px auto;border:none;border-top:1px dashed #30363d}
"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 每日复盘+对账 合并报告 {date}</title>
<style>{base_css}</style>
<style>{''.join(css_a)}</style>
<style>{namespace_css(''.join(css_b), '#partB')}</style>
</head><body>
<div class="masthead">
  <h1>tpoint 每日复盘 + 对账 · 合并报告</h1>
  <div class="meta">{date} ｜ 生成于 {now} ｜ Part A=每日复盘（floor引擎复算+实盘投递实况+信号标注图） ｜ Part B=生产vs回测对账（simulate_day同源round-trip，万一+印花+滑点2bps）</div>
</div>
<div class="toc">
  <a href="#partA">A · 每日复盘</a>
  <a href="#partA-0">A0 · 实盘投递实况</a>
  <a href="#partA-5">A5 · 行情图标注</a>
  <a href="#partB">B · 每日对账</a>
  <a href="#partB-4">B4 · 实盘推送明细</a>
  <a href="#partB-6">B6 · Round-trip 配对</a>
</div>
<div class="part-title" id="partA">Part A · 每日复盘报告</div>
{body_a}
<hr class="sep">
<div class="part-title">Part B · 生产 vs 回测 对账报告</div>
<div id="partB">{body_b}</div>
</body></html>"""
    html = html.replace('<h2>〇、今日实盘投递实况', '<h2 id="partA-0">〇、今日实盘投递实况', 1)
    html = html.replace('<h2>五、当日行情图', '<h2 id="partA-5">五、当日行情图', 1)
    html = html.replace('<h2>四、实盘推送明细', '<h2 id="partB-4">四、实盘推送明细', 1)
    html = html.replace('<h2>六、Round-trip 配对明细', '<h2 id="partB-6">六、Round-trip 配对明细', 1)

    path = os.path.join(OUT, f'daily_report_{date}.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ {path}（{os.path.getsize(path)//1024}KB）')


if __name__ == '__main__':
    main()
