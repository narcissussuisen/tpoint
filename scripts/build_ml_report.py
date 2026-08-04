# -*- coding: utf-8 -*-
"""汇编 T+0 优化研究报告 HTML（深色主题，tpoint 风格）

聚合：开源调研（output/research/open_source_survey.md）
      + 现状参数清单（docs/parameter_inventory.md）
      + ML 训练结果（output/ml_train_results.json）
      + 规则推荐（output/ml_rules.json）
      + 回测验证（若有）

输出：output/t0_optimization_research_2026-08-01.html
"""
import base64
import html
import json
import os
import re
import sys

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

OUT = os.path.join(BASE, 'output', 't0_optimization_research_2026-08-01.html')

SECTOR_CN = {'sh_main': '沪主板', 'sz_main': '深主板', 'chinext': '创业板',
             'star': '科创板', 'bse': '北交所', 'etf_lof': 'ETF/LOF'}
FEAT_CN = {
    'vwap_dev': 'VWAP偏离%', 'atr_pct': 'ATR/价%', 'dif': 'MACD DIF', 'dea': 'MACD DEA',
    'hist': 'MACD柱', 'hist_pct': 'MACD柱%', 'trend': '趋势', 'trend_strong': '强趋势',
    'rsi': 'RSI(14)', 'vol_ratio': '量比', 'temp': '温度', 'chg': '当日涨跌%',
    'pos_in_day': '日内位置', 'bar_idx_frac': 'bar位置',
    'macd5_dif': 'MACD5 DIF', 'macd5_hist': 'MACD5柱', 'macd15_dif': 'MACD15 DIF',
    'macd15_hist': 'MACD15柱', 'macd30_dif': 'MACD30 DIF', 'macd30_hist': 'MACD30柱',
    'macd60_dif': 'MACD60 DIF', 'macd60_hist': 'MACD60柱',
    'rsi_dist_30': 'RSI距30', 'rsi_dist_70': 'RSI距70', 'kdj_k': 'KDJ K', 'kdj_d': 'KDJ D',
    'kdj_j': 'KDJ J', 'atr_chan_up1': 'ATR上通道', 'atr_chan_dn1': 'ATR下通道',
    'mom_1': '1bar动量', 'mom_5': '5bar动量', 'mom_15': '15bar动量',
    'is_morning': '早盘', 'is_noon': '午盘', 'is_tail': '尾盘',
    'g_factor': '引力因子', 'v_factor': '量价因子', 'm_factor': 'MACD因子',
    'resonance': '共振分',
}


def esc(x):
    return html.escape(str(x))


def read_md_sections(md_path):
    """解析调研 md 的横向因子汇总表段落（粗提取）。"""
    if not os.path.isfile(md_path):
        return []
    with open(md_path, encoding='utf-8') as fh:
        content = fh.read()
    # 提取所有 markdown 表格
    tables = []
    for m in re.finditer(r'\|[^\n]+\|\n(\|[^\n]+\|)+\n((?:\|[^\n]+\|\n)+)', content):
        lines = m.group(0).strip().split('\n')
        rows = []
        for ln in lines:
            cells = [c.strip() for c in ln.strip('|').split('|')]
            if all(re.fullmatch(r':?-+:?', c) for c in cells):
                continue
            rows.append(cells)
        if len(rows) >= 2:
            tables.append(rows)
    return tables


def build_html(train_res, rules_res, survey_tables):
    h = []
    h.append('''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分时做T（T+0）量化策略系统性调研与优化报告 · tpoint 2026-08-01</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2333;--line:#30363d;--fg:#e6edf3;
--mut:#8b949e;--acc:#58a6ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:24px;max-width:1280px;margin:0 auto}
h1{font-size:23px;margin-bottom:4px} h2{font-size:17px;margin:30px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;text-align:center}
.kpi .v{font-size:24px;font-weight:700} .kpi .l{color:var(--mut);font-size:12px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;background:var(--card2)} td:first-child,th:first-child{text-align:left}
tr:hover td{background:rgba(88,166,255,.05)}
.pos{color:var(--ok);font-weight:600} .neg{color:var(--bad);font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:var(--card2);border:1px solid var(--line);margin-left:6px}
.verdict{background:rgba(63,185,80,.08);border:1px solid rgba(63,185,80,.4)}
.warnbox{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.4)}
ol{padding-left:22px} li{margin:5px 0}
.small{color:var(--mut);font-size:12px}
.bar{height:8px;border-radius:4px;background:var(--card2);display:inline-block;vertical-align:middle}
code{background:var(--card2);padding:1px 6px;border-radius:4px;font-size:12px}
</style></head><body>''')

    # ===== 标题 =====
    h.append('<h1>分时做T（T+0）量化策略系统性调研与优化报告</h1>')
    h.append('<div class="sub">tpoint · 2026-08-01 · 调研GitHub开源策略 → 梳理现状 → XGBoost因子研究 → 规则落地推荐</div>')

    # ===== KPI =====
    n_sample = train_res.get('n_samples', 0)
    best_auc_b = train_res['models'].get('B_xgb', {}).get('auc')
    best_auc_s = train_res['models'].get('S_xgb', {}).get('auc')
    h.append('<div class="grid">')
    h.append(f'<div class="kpi"><div class="v">{n_sample:,}</div><div class="l">ML 样本量（信号点）</div></div>')
    h.append(f'<div class="kpi"><div class="v">{best_auc_b if best_auc_b is not None else "—"}</div><div class="l">B信号 OOS AUC (XGB)</div></div>')
    h.append(f'<div class="kpi"><div class="v">{best_auc_s if best_auc_s is not None else "—"}</div><div class="l">S信号 OOS AUC (XGB)</div></div>')
    n_rec = len(rules_res.get('recommendations', []))
    h.append(f'<div class="kpi"><div class="v">{n_rec}</div><div class="l">规则参数推荐</div></div>')
    h.append('</div>')

    # ===== 1. 开源调研 =====
    h.append('<h2>① GitHub 开源 T+0 策略调研</h2>')
    h.append('<div class="card">')
    h.append('<b>已调研 8 个开源策略</b>（详见 <code>output/research/open_source_survey.md</code>）<ol>')
    for name in ['T0GridTrader（ATR动态网格+布林带）', 'xhuohai/T0T（多周期MACD+KDJ双背离）',
                 'day-wing-transaction（日内偏离度回归+MACD开关）', '掘金分时做T（偏离度动态阈值+量价背离）',
                 'CSDN MACD+RSI三重共振（RSI30/70+金叉+量比）', '聚宽小市值做T（涨回撤风控）',
                 '逻辑58分时T0（双点背离+二次金叉）', 'stock-intraday-trading（MA60钟摆模型）']:
        h.append(f'<li>{name}</li>')
    h.append('</ol>')
    # 横向因子汇总表（从调研 md 提取的表格）
    for t in survey_tables[:2]:
        h.append('<table><tr>' + ''.join(f'<th>{esc(c)}</th>' for c in t[0]) + '</tr>')
        for row in t[1:5]:
            h.append('<tr>' + ''.join(f'<td>{esc(c)}</td>' for c in row) + '</tr>')
        h.append('</table>')
    h.append('<div class="small" style="margin-top:8px">注：开源参数来自各仓库 README/源码（详见调研文档），未经独立验证；tpoint 对标列见下一节。</div></div>')

    # ===== 2. 现状梳理 =====
    h.append('<h2>② tpoint 现状梳理（与开源对照）</h2>')
    h.append('''<div class="card"><table><tr><th>因子</th><th>开源主流</th><th>tpoint 当前</th><th>差异</th></tr>
<tr><td>MACD 参数</td><td>12/26/9</td><td>12/26/9</td><td class="pos">一致</td></tr>
<tr><td>MACD 背离</td><td>双点比较（价格极值+MACD极值）</td><td>单点动能衰竭（简化）</td><td class="warn">可加强</td></tr>
<tr><td>背离强度</td><td>MACD+KDJ 双背离共振</td><td>min_hist_diff=0（全放行）</td><td class="warn">建议0.15+</td></tr>
<tr><td>RSI</td><td>14, 30/70 超买超卖</td><td>14（仅温度子因子）</td><td class="warn">无独立门控</td></tr>
<tr><td>KDJ</td><td>9/3/3 金叉死叉+背离</td><td>无</td><td class="bad">缺失</td></tr>
<tr><td>ATR</td><td>14, 网格间距3~5倍</td><td>14, 引力带0.6倍</td><td class="warn">用法不同</td></tr>
<tr><td>布林带</td><td>20, 上下轨触发</td><td>无</td><td class="bad">缺失</td></tr>
<tr><td>量能</td><td>量比1.5/0.8</td><td>1.2/0.8（已禁用）</td><td class="warn">已禁用</td></tr>
<tr><td>动态阈值</td><td>偏离度×(1+波动率)</td><td>固定 0.6/1.5</td><td class="warn">无自适应</td></tr>
<tr><td>多周期</td><td>月/周/日/分钟（5/15/30/60）</td><td>仅分钟级</td><td class="warn">无多周期</td></tr>
<tr><td>尾盘风控</td><td>14:30 禁新仓</td><td>仅 EOD 强平</td><td class="warn">部分缺失</td></tr>
<tr><td>止损</td><td>T0T 有止损+修正</td><td>仅移动止损 act0.4/trail0.6</td><td class="warn">无硬止损</td></tr>
</table><div class="small">完整参数表见 <code>docs/parameter_inventory.md</code>（33 项参数+成本模型）。</div></div>''')

    # ===== 3. ML 训练与评估 =====
    h.append('<h2>③ ML 因子研究（XGBoost vs LightGBM vs RandomForest）</h2>')
    h.append('<div class="card"><b>实验设计</b><ul>'
             '<li>样本：全市场 3351 只过滤后标的的信号点（B/S 分开建模，因子语义相反）</li>'
             '<li>标签：信号点后 20 根 1m bar 净收益（扣双边成本）&gt;0 → 二分类（与项目胜率口径一致）</li>'
             '<li>防泄漏：GroupKFold by 交易日（同组不跨折）+ Purged walk-forward（前70%train/后30%test）</li>'
             '<li>特征：34 个（基础14 + 多周期MACD/KDJ/动量/时段 17 + 信号上下文3），全部因果可用</li>'
             '</ul></div>')

    # 模型对比表
    h.append('<div class="card"><b>模型对比（Purged walk-forward OOS）</b><table><tr>'
             '<th>信号</th><th>模型</th><th>AUC</th><th>精确率</th><th>召回率</th><th>F1</th>'
             '<th>CV AUC</th><th>箱单调</th><th>top箱胜率</th></tr>')
    for sig in ['B', 'S']:
        for name in ['xgb', 'lgb', 'rf']:
            m = train_res['models'].get(f'{sig}_{name}')
            if not m:
                continue
            cls = 'pos' if (m.get('bin_monotonic') and m.get('top_bin_win', 0) > 0.5) else 'warn'
            h.append(f'<tr><td>{sig}</td><td>{name.upper()}</td>'
                     f'<td>{m["auc"]:.4f}</td><td>{m["precision"]:.3f}</td>'
                     f'<td>{m["recall"]:.3f}</td><td>{m["f1"]:.3f}</td>'
                     f'<td>{m.get("cv_auc") or "—"}</td>'
                     f'<td class="{cls}">{"✓" if m["bin_monotonic"] else "✗"}</td>'
                     f'<td>{m["top_bin_win"]:.1%}</td></tr>')
    h.append('</table><div class="small">箱单调 = 按预测概率分10箱，胜率随箱位单调递增；top箱胜率 &gt;50% 才算模型有交易价值。</div></div>')

    # 特征重要性
    h.append('<div class="card"><b>特征重要性 Top15（XGBoost gain + permutation 双法）</b>')
    for sig in ['B', 'S']:
        imp = train_res.get('feature_imp', {}).get(sig, {})
        tops = imp.get('top_features', [])[:15]
        if not tops:
            continue
        h.append(f'<div style="margin:10px 0 4px"><b>{sig} 信号</b></div>')
        h.append('<table><tr><th>排名</th><th>特征</th><th>中文</th><th>gain 重要性</th><th>重要性占比</th></tr>')
        gain = {k: float(v) for k, v in imp.get('gain', {}).items()}
        total = sum(gain.values()) or 1
        for i, f in enumerate(tops, 1):
            g = gain.get(f, 0)
            pct = g / total * 100
            h.append(f'<tr><td>{i}</td><td><code>{esc(f)}</code></td><td>{esc(FEAT_CN.get(f, f))}</td>'
                     f'<td>{g:.6f}</td><td>{pct:.1f}%</td></tr>')
        h.append('</table>')
    h.append('</div>')

    # ===== 4. 规则落地 =====
    h.append('<h2>④ ML→规则落地：分箱净收益表与参数推荐</h2>')
    recs = rules_res.get('recommendations', [])
    if recs:
        h.append('<div class="card"><b>参数推荐表</b><table><tr><th>参数</th><th>信号</th>'
                 '<th>基线胜率</th><th>最优区间</th><th>最优胜率</th><th>提升(lift)</th><th>单调性</th></tr>')
        for r in recs:
            cls = 'pos' if r['lift'] > 0.03 else ('warn' if r['lift'] > 0 else 'neg')
            h.append(f'<tr><td><code>{esc(r["param"])}</code></td><td>{r["sig_type"]}</td>'
                     f'<td>{r["base_win"]:.1%}</td><td>{esc(r["best_bin"])}</td>'
                     f'<td>{r["best_win"]:.1%}</td>'
                     f'<td class="{cls}">{r["lift"]:+.1%}</td>'
                     f'<td>{r["monotonic"]}</td></tr>')
        h.append('</table></div>')

    # 分箱明细（前6个特征）
    h.append('<div class="card"><b>关键特征分箱明细（B/S 合并展示前 6 个）</b>')
    shown = 0
    for key, v in rules_res.get('bins', {}).items():
        if shown >= 6:
            break
        sig, feat = key.split('_', 1)
        if feat not in ('hist_pct', 'vwap_dev', 'rsi', 'kdj_k', 'mom_5', 'chg'):
            continue
        h.append(f'<div style="margin:10px 0 4px"><b>{sig} · {esc(FEAT_CN.get(feat, feat))}</b> '
                 f'<span class="small">{esc(v["note"])}</span></div>')
        h.append('<table><tr><th>箱</th><th>区间</th><th>样本数</th><th>胜率</th></tr>')
        for row in v['rows'][:8]:
            cls = 'pos' if row['win_rate'] > 0.5 else 'neg'
            bin_label = str(row.get('_bin', row.get('rank', '')))
            h.append(f'<tr><td>{esc(bin_label)}</td><td>{esc(row.get("range", ""))}</td>'
                     f'<td>{row.get("n", 0)}</td><td class="{cls}">{row["win_rate"]:.1%}</td></tr>')
        h.append('</table>')
        shown += 1
    h.append('</div>')

    # ===== 5. 结论 =====
    h.append('<h2>⑤ 结论与落地建议</h2>')
    h.append('''<div class="card verdict"><ol>
<li><b>背离强度过滤（min_hist_diff）</b>：分箱验证 + 两轮消融均指向弱背离负 alpha，
建议 0.15（安全平台 0.10~0.20），待用户决策后接入 monitor check_miji_trigger。</li>
<li><b>VWAP_DEV</b>：当前 0.6 处于引力带合理区间，分箱验证后可微调。</li>
<li><b>KDJ 补充</b>：开源普遍使用 9/3/3 双背离共振，tpoint 缺失，ML 特征验证其重要性后决定是否加入。</li>
<li><b>RSI 独立门控</b>：开源 30/70 超买超卖，tpoint 仅温度子因子；若 ML 显示 RSI 特征重要，可加独立过滤。</li>
<li><b>尾盘风控</b>：is_tail 特征分箱若显示 14:30 后信号质量差，可加"尾盘禁新仓"规则。</li>
<li><b>硬止损</b>：T0T 等开源普遍有止损，tpoint 仅移动止损；可评估恢复 ATR 硬止损（当前关闭）。</li>
</ol></div>''')

    # ===== 附：数据与评估口径 =====
    h.append('<h2>附录：数据来源与评估口径</h2>')
    h.append('''<div class="card"><table><tr><th>项</th><th>说明</th></tr>
<tr><td>数据源</td><td>F盘 F:\\keyfactor_data\\1m\\ 全市场 1m 历史库（4149 只，tickflow 格式，单标的 21~146 天）</td></tr>
<tr><td>回测区间</td><td>2025-12 ~ 2026-07（按标的覆盖 20-146 个交易日，OOS 按日期前70%/后30%切分）</td></tr>
<tr><td>标的过滤</td><td>交易日≥30 / 日均成交额≥2000万 / 价格3-100元 / 一字bar≤30% / 涨停日≤20% / 剔除ST、北交所、ETF → 3351 只</td></tr>
<tr><td>成本模型</td><td>万一佣金 + 印花税万5.641(个股卖)/0(ETF) + 滑点2bps；个股双边≈0.116%</td></tr>
<tr><td>标签</td><td>信号点后 N=20 根 1m bar 净收益（扣双边成本）>0；N 敏感性 10/20/30/60 对照</td></tr>
<tr><td>评估口径</td><td>ML：AUC/精确率/召回率/F1/分箱单调性；交易：净胜率/盈亏比/最大回撤/夏普（aggregate_metrics）</td></tr>
<tr><td>防泄漏</td><td>GroupKFold by date + Purged walk-forward + 特征全因果（仅用信号时刻及以前数据）</td></tr>
</table></div>''')

    h.append('<div class="small" style="margin-top:20px;color:var(--mut)">tpoint 因子层优化 · 2026-08-01 · '
             '引擎 miji_alpha floor 模式 · 全部脚本见 scripts/ml_*.py · 不构成投资建议</div>')
    h.append('</body></html>')

    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(h))
    print(f'HTML 报告已生成 → {OUT}')
    return OUT


def main():
    train_path = os.path.join(BASE, 'output', 'ml_train_results.json')
    rules_path = os.path.join(BASE, 'output', 'ml_rules.json')
    survey_path = os.path.join(BASE, 'output', 'research', 'open_source_survey.md')
    train_res = json.load(open(train_path, encoding='utf-8')) if os.path.isfile(train_path) else {}
    rules_res = json.load(open(rules_path, encoding='utf-8')) if os.path.isfile(rules_path) else {}
    survey_tables = read_md_sections(survey_path)
    build_html(train_res, rules_res, survey_tables)


if __name__ == '__main__':
    main()
