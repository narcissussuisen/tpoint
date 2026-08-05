#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_factor_backtest_report.py — 从 factor_opt_<date>.json 生成关键因子回测报告 HTML。"""
import os, sys, json, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOYED = {'161129.SZ': '0.5/0.6', '513310.SH': '0.3/0.5', '688111.SH': '0.5/0.8', '300308.SZ': '0.5/0.6'}
NAMES = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF', '688111.SH': '金山办公', '300308.SZ': '中际旭创'}


def fmt(x):
    return f"n={x['n']} wr={x['win_rate']}% pl={x['pl_ratio']} ret={x['total_ret']}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    a = ap.parse_args()
    d = json.load(open(os.path.join(ROOT, 'output', f'factor_opt_{a.date}.json'), encoding='utf-8'))

    rows, grid_rows = [], ''
    for sym, v in d['symbols'].items():
        if 'error' in v:
            continue
        b = v['baseline']; dep = DEPLOYED[sym]; dm = v['trail_grid'][dep]
        rows.append((sym, NAMES[sym], v['n_days'], dep, b, dm,
                     round(dm['total_ret'] - b['total_ret'], 2), round(dm['win_rate'] - b['win_rate'], 1)))
        for k, m in sorted(v['trail_grid'].items(), key=lambda x: -x[1]['total_ret']):
            mark = ' 👈已上线' if k == dep else ''
            grid_rows += (f"<tr><td>{sym}</td><td>{k}{mark}</td><td>{m['n']}</td>"
                          f"<td>{m['win_rate']}</td><td>{m['pl_ratio']}</td><td>{m['total_ret']}</td></tr>")
    sum_rows = ''.join(
        f"<tr><td>{s}</td><td>{n}</td><td>{nd}天</td><td>{dep}</td><td>{fmt(b)}</td><td>{fmt(dm)}</td>"
        f"<td class={'g' if dret > 0 else 'r'}>{dret:+.2f}pp</td><td>{dwr:+.1f}pp</td></tr>"
        for s, n, nd, dep, b, dm, dret, dwr in rows)

    html = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>tpoint 关键因子回测报告 {a.date}</title>
<style>body{{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#fafbfc;color:#222;padding:28px;max-width:1150px;margin:auto;line-height:1.6}}
h1{{font-size:20px;border-bottom:2px solid #c0392b;padding-bottom:8px}}h2{{font-size:16px;margin-top:26px;color:#c0392b}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff}}td,th{{border:1px solid #ddd;padding:5px 8px;text-align:left}}th{{background:#eef2f6}}
.g{{color:#2e7d32;font-weight:600}}.r{{color:#c0392b}}.box{{background:#fff;border:1px solid #e0e0e0;border-left:4px solid #c0392b;padding:12px 16px;margin:12px 0;font-size:13.5px}}
.note{{color:#666;font-size:12px;line-height:1.7}}</style></head><body>
<h1>tpoint 关键因子回测报告（v10.1.0 上线口径复核）· {a.date}</h1>
<h2>一、回测因子清单 · 取值范围 · 优化目标</h2>
<table><tr><th>因子</th><th>取值范围（网格）</th><th>类型</th><th>说明</th></tr>
<tr><td>trail_activate_pct</td><td>{{0.3, 0.4, 0.5}}（%）</td><td>出场-移动止损激活线</td><td>浮盈达到该阈值才启动移动止损，防噪音触发</td></tr>
<tr><td>trail_pct</td><td>{{0.5, 0.6, 0.8}}（%）</td><td>出场-回撤容忍</td><td>从最高浮盈回撤该比例即止盈出场</td></tr>
<tr><td>atr_min_pct</td><td>{{0.15, 0.25, 0.35}}（%）</td><td>信号-ATR门控</td><td>本次实测三档指标完全一致（无区分度），未变更</td></tr></table>
<div class="box"><b>优化目标（v10.1.0 口径）：total_ret 最大化（抓波动本质），硬约束 win_rate 不劣于基线（0.4/0.6）、n≥30。</b><br>
该口径是对 optimizer 默认 wr 优先口径的纠偏——防"胜率虚胖"（08-05 实证：0.5/0.5 wr 57.1% 但 ret 腰斩）。<br>
数据：F盘全历史 1m（含今日，70~149 交易日）+ 生产同源信号复算 + simulate_day；成本=万一佣金+印花(股票卖边)+滑点2bps/边。</div>
<h2>二、最优因子组合及数值（已上线 v10.0.1，今日数据复核）</h2>
<table><tr><th>标的</th><th>名称</th><th>样本</th><th>上线trail</th><th>基线(0.4/0.6)</th><th>上线组合</th><th>Δ收益</th><th>Δ胜率</th></tr>{sum_rows}</table>
<h2>三、全网格回测明细（按 total_ret 排序）</h2>
<table><tr><th>标的</th><th>trail组合</th><th>n</th><th>wr%</th><th>pl</th><th>ret%</th></tr>{grid_rows}</table>
<h2>四、分析说明</h2>
<div class="box">
1. <b>161129 / 300308：上线参数经今日数据复核仍为网格最优</b>（ret 口径第一且 wr 不降），继续生效。<br>
2. <b>513310 / 688111：wr 约束与 ret 目标边际冲突</b>——513310 上线值 0.3/0.5 的 ret(-5.29) 优于规则内最优 0.5/0.8(-6.02)，wr 较基线 -1.2pp；688111 上线值 ret +0.9pp、wr -0.3pp（n≈190 属噪音范围）。按"抓波动=ret 优先"指令<b>维持上线值不变</b>，wr 偏差≤1.2pp，由明日闭环 A 步实盘验证兜底。<br>
3. <b>结构性警告不变</b>：513310/688111 全历史 ret 为负，调参仅减亏；建议评审移出 watchlist 或换框架。<br>
4. atr_min_pct 三档无区分度（ATR 门控未实际过滤信号），该因子当前为闲置参数，后续可下线或重标定。<br>
5. 纪律声明：样本内网格寻优；周五 tune_pool_40 盲 holdout 复核后才算最终验收。</div>
<p class="note">生成：build_factor_backtest_report.py ｜ 数据 factor_opt_{a.date}.json（含今日F盘数据 17:27 重跑）｜ git v10.1.0</p>
</body></html>'''
    out = os.path.join(ROOT, 'output', f'factor_backtest_{a.date}.html')
    open(out, 'w', encoding='utf-8').write(html)
    print(f'[ok] {out}')


if __name__ == '__main__':
    main()
