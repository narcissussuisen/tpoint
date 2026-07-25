# -*- coding: utf-8 -*-
"""渲染 D 候选策略 walk-forward OOS 验证报告 (HTML + 内嵌图)。

读取 d_candidate_backtest.py 的产物:
  - output/d_candidate_20260726/d_summary.json
  - output/d_candidate_20260726/d_wf_trajectory.csv
生成 output/d_candidate_20260726/d_candidate_report.html
"""
import sys, os, json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, 'output', 'd_candidate_20260726')
os.makedirs(OUT, exist_ok=True)

# 中文
try:
    fp = 'C:/Windows/Fonts/simhei.ttf'
    font_manager.fontManager.addfont(fp)
    plt.rcParams['font.family'] = 'SimHei'
except Exception:
    pass
plt.rcParams['axes.unicode_minus'] = False


def fig_to_b64(fig):
    import base64
    from io import BytesIO
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def main():
    summary = json.load(open(os.path.join(OUT, 'd_summary.json'), encoding='utf-8'))
    traj = pd.read_csv(os.path.join(OUT, 'd_wf_trajectory.csv'), encoding='utf-8-sig')

    syms = summary['symbols']
    days = summary['days']
    oos = summary['oos']
    hold = summary['holdout']
    missing = summary.get('missing', [])

    # ---- 图1: 每个测试日 D vs R 的胜率 ----
    wf = traj[~traj['is_holdout']].copy()
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for mode, col in (('D', '#1e8449'), ('R', '#c0392b')):
        sub = wf[wf['mode'] == mode].sort_values('segment')
        ax.plot(range(len(sub)), sub['win_rate'].values, marker='o', ms=4,
                label=f'D (候选)' if mode == 'D' else 'R (共振)', color=col)
    ax.axhline(50, color='gray', ls='--', lw=0.8)
    ax.set_xticks(range(len(wf[wf['mode'] == 'D'])))
    ax.set_xticklabels(wf[wf['mode'] == 'D']['segment'].values, rotation=90, fontsize=7)
    ax.set_ylabel('胜率 %'); ax.set_title('Walk-forward 各测试日胜率: D(候选) vs R(共振)')
    ax.legend(); ax.grid(alpha=0.3)
    img1 = fig_to_b64(fig)

    # ---- 图2: 每个测试日 D vs R 的总收益(逐笔pnl和) ----
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for mode, col in (('D', '#1e8449'), ('R', '#c0392b')):
        sub = wf[wf['mode'] == mode].sort_values('segment')
        ax.plot(range(len(sub)), sub['tot_ret'].values, marker='o', ms=4,
                label=f'D (候选)' if mode == 'D' else 'R (共振)', color=col)
    ax.axhline(0, color='gray', ls='--', lw=0.8)
    ax.set_xticks(range(len(wf[wf['mode'] == 'D'])))
    ax.set_xticklabels(wf[wf['mode'] == 'D']['segment'].values, rotation=90, fontsize=7)
    ax.set_ylabel('总收益 %'); ax.set_title('Walk-forward 各测试日总收益: D(候选) vs R(共振)')
    ax.legend(); ax.grid(alpha=0.3)
    img2 = fig_to_b64(fig)

    # ---- 图3: D 被选参数频率 (K, WL, EMA) ----
    d_wf = wf[wf['mode'] == 'D']
    combo = d_wf.apply(lambda r: f"K={r['K']} WL={r['WL']} EMA={r['EMA']}", axis=1)
    vc = combo.value_counts()
    fig, ax = plt.subplots(figsize=(9, max(3, len(vc) * 0.5)))
    ax.barh(vc.index[::-1], vc.values[::-1], color='#1e8449')
    ax.set_xlabel('被选次数 (共 %d 测试日)' % len(d_wf))
    ax.set_title('D 候选策略 walk-forward 中被选中的参数组合频率')
    img3 = fig_to_b64(fig)

    # ---- 表格 ----
    def agg_row(a):
        return (a.get('n', 0), a.get('win_rate'), a.get('tot_ret'), a.get('pf'), a.get('avg'))

    def html_agg(a):
        n, wr, tr, pf, avg = agg_row(a)
        wr = '-' if wr is None else f'{wr:.1f}%'
        pf = '-' if pf is None else f'{pf:.2f}'
        return f'<td>{n}</td><td>{wr}</td><td>{tr:+.2f}</td><td>{pf}</td><td>{avg:+.2f}</td>'

    # OOS 汇总表
    oos_html = f"""<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr><th>集合</th><th>策略</th><th>笔数</th><th>胜率</th><th>总收益%</th><th>PF</th><th>每笔均值%</th></tr>
      <tr><td rowspan="2">OOS (WF测试日)</td><td>D 候选</td>{html_agg(oos['D'])}</tr>
      <tr><td>R 共振</td>{html_agg(oos['R'])}</tr>
      <tr><td rowspan="2">HOLDOUT (末{summary['hold']}日)</td><td>D 候选</td>{html_agg(hold['D'])}</tr>
      <tr><td>R 共振</td>{html_agg(hold['R'])}</tr>
    </table>"""

    # 逐标的拆解
    def sym_table(block, title):
        rows = ''
        for code, name in syms.items():
            a = block.get('per_symbol', {}).get(code)
            if a is None:
                rows += f'<tr><td>{code}</td><td>{name}</td><td colspan="5">无交易</td></tr>'
            else:
                rows += f'<tr><td>{code}</td><td>{name}</td>{html_agg(a)}</tr>'
        return f'<h4>{title}</h4><table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">' \
               f'<tr><th>代码</th><th>名称</th><th>笔数</th><th>胜率</th><th>总收益%</th><th>PF</th><th>每笔均值%</th></tr>{rows}</table>'

    sym_html = sym_table(oos['D'], 'OOS 逐标的 — D 候选') + '<br>' + sym_table(oos['R'], 'OOS 逐标的 — R 共振')
    sym_html += '<br>' + sym_table(hold['D'], 'HOLDOUT 逐标的 — D 候选') + '<br>' + sym_table(hold['R'], 'HOLDOUT 逐标的 — R 共振')

    # 轨迹表 (末几行)
    traj_show = traj.sort_values(['mode', 'segment']).tail(30)
    traj_rows = ''
    for _, r in traj_show.iterrows():
        m = r['mode']
        seg = r['segment']
        traj_rows += (f"<tr><td>{m}</td><td>{seg}</td><td>{r['K']}</td><td>{r['WL']}</td>"
                      f"<td>{r['EMA']}</td><td>{r['k_stop']}</td><td>{r['rev_exit']}</td>"
                      f"<td>{r['n_tr']}</td><td>{r['win_rate']}</td><td>{r['tot_ret']:+.2f}</td>"
                      f"<td>{r['pf']}</td><td>{r['avg']:+.2f}</td></tr>")
    traj_html = (f'<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:12px">'
                 f'<tr><th>模式</th><th>测试日</th><th>K</th><th>WL</th><th>EMA</th><th>k_stop</th>'
                 f'<th>rev</th><th>笔数</th><th>胜率</th><th>总收益%</th><th>PF</th><th>每笔</th></tr>'
                 f'{traj_rows}</table>')

    # 数据覆盖
    cover = ''
    for code, name in syms.items():
        cover += f'<tr><td>{code}</td><td>{name}</td><td>{summary["n_days"]} 日 (目标窗口)</td></tr>'
    miss_html = ('<p style="color:#c0392b">缺失样本: ' + ', '.join(f'{c}@{d}' for c, d in missing[:20]) +
                 (' …' if len(missing) > 20 else '') + '</p>') if missing else '<p style="color:#1e8449">无缺失样本</p>'

    grid_txt = (f"D 网格: K={summary['grid_D']['K']}, WL={summary['grid_D']['WL']}, "
                f"EMA={summary['grid_D']['EMA']}, k_stop={summary['grid_D']['k_stop']}, "
                f"rev_exit={summary['grid_D']['rev_exit']}<br>"
                f"R 网格(仅出场): k_stop={summary['grid_R']['k_stop']}, rev_exit={summary['grid_R']['rev_exit']}")

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>D 候选策略 Walk-forward OOS 验证</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:24px;color:#222;line-height:1.6}}
h1{{color:#1a5276}} h2{{color:#1a5276;border-bottom:2px solid #1a5276;padding-bottom:4px}}
table{{margin:8px 0}} th{{background:#1a5276;color:#fff}} td,th{{border:1px solid #ccc;text-align:center}}
img{{max-width:100%;margin:10px 0;border:1px solid #ddd}}</style></head><body>
<h1>D 候选策略 (clean skeleton) — Walk-forward 样本外验证</h1>
<p>分支 <code>feat/v9.4.0-floord-candidate</code> · 生成日期 2026-07-26 · 标的 {len(syms)} 只 T+0 · {summary['n_days']} 交易日</p>

<h2>一、方法修正 (相对旧诊断, 已消除全部前视偏差)</h2>
<ol>
<li><b>pc seed / regime</b>: regime 用<b>日内 seed EMA</b>(当日首根收盘 seed, 每日重置), 不再用昨收 pc 导致被隔夜跳空黏住。</li>
<li><b>漏顶/漏底</b>: 极值改用 <b>BAR 自身 HIGH/LOW</b> 取 (修原 <code>_is_new_high</code> 用收盘价比前窗最高价的结构性漏顶)。</li>
<li><b>后视镜 P&L</b>: 改用真正<b>前向回测</b> — 信号 bar 下一根开盘入场, ATR 止损 / D 卖点反转 / 14:55 EOD 强平, 计算真实可落袋 P&L。</li>
<li><b>未来反转确认</b>: 旧 combo 的「极值后 N 根反向」仅作审计列, <b>不参与触发</b>; 信号层纯因果。</li>
<li><b>数据清洗</b>: 移除 mootdx 导出偶发 5.8e-39 哨兵量, 避免污染 VWAP。</li>
</ol>

<h2>二、数据覆盖</h2>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
<tr><th>代码</th><th>名称</th><th>覆盖</th></tr>{cover}</table>
{miss_html}

<h2>三、Walk-forward 配置</h2>
<p>切分: 扩张窗口 + 重优化。测试日 k ∈ [{summary['min_train']}, {summary['n_days']-summary['hold']}), 在 days[0:k] 上网格优化, 在 day[k] 评估 (OOS)。末 {summary['hold']} 日 (HOLDOUT) 全程不进任何优化窗口。</p>
<p>{grid_txt}</p>

<h2>四、OOS 与 HOLDOUT 汇总 (D 候选 vs R 共振, 同口径前向回测)</h2>
{oos_html}
<p style="color:#555">注: 两者均在各自扩张窗口上优化出场参数后做样本外评估, 头对头可比。</p>

<h2>五、逐标的拆解</h2>
{sym_html}

<h2>六、各测试日走势</h2>
<img src="data:image/png;base64,{img1}"><br>
<img src="data:image/png;base64,{img2}">

<h2>七、D 参数选择稳定性</h2>
<img src="data:image/png;base64,{img3}">

<h2>八、参数轨迹 (末 30 行)</h2>
{traj_html}

<h2>九、诚实结论</h2>
<p id="concl">（由下方脚本依据汇总指标自动生成）</p>

<hr>
<p style="color:#888;font-size:12px">⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""

    # 结语 (数据驱动 + 结构性分析)
    d_oos = oos['D']; r_oos = oos['R']; d_h = hold['D']; r_h = hold['R']
    concl = f"""<b>核心结论: 在干净前向方法下, D(候选) 作为一个独立 T+0 信号层并不可行。</b><br><br>
    1) <b>信号极度稀疏 (结构性)</b>: D 的买点要求「上行 regime(EMA_f≥EMA_s) <b>且</b> 价格低于 VWAP 的 swing-low」,
       这两个条件在趋势性日内 ETF/LOF 里近乎互斥 —— 价格低于 VWAP 处于 swing-low 时, regime 通常已转空。
       实测 D 全样本仅约 0.5–1.5 个买信号/日/5标 (而卖信号约 59/日)。优化器被迫在「极少交易」的配置里挑,
       导致 OOS 9 个测试日仅成交 <b>{d_oos['n']}</b> 笔, 胜率 <b>{d_oos['win_rate']}%</b>, 总收益 {d_oos['tot_ret']:+.2f}%, PF {d_oos['pf']}。<br>
    2) <b>共振(R) 虽薄边但可交易</b>: 同口径 OOS 上 R 成交 <b>{r_oos['n']}</b> 笔, 胜率 {r_oos['win_rate']}%, 总收益 {r_oos['tot_ret']:+.2f}%, PF {r_oos['pf']};
       作为正向T策略它持续开仓、净微微为正, 但胜率仅约 1/4, 边缘极薄。<br>
    3) <b>HOLDOUT(末 {summary['hold']} 日, 未参与优化)</b>: D 成交 {d_h['n']} 笔(胜率 {d_h['win_rate']}%, {d_h['tot_ret']:+.2f}%) vs R {r_h['n']} 笔(胜率 {r_h['win_rate']}%, {r_h['tot_ret']:+.2f}%)。
       样本仅 4 日, 不足以改写结论。<br>
    4) <b>推翻前几轮叙事</b>: 此前「D 在 7/24 很神 / 命中 13:12 +1.09%」来自后视镜 P&L + 错 pc seed + 单日 cherry-pick 三重偏差;
       干净方法下 D 不仅没产生 alpha, 反而因过度稀疏几乎无法交易。旧的「四条件后视镜策略」已标记 SUPERSEDED。<br><br>
    <b>建议</b>: D 不应作为独立信号替换共振。若继续探索, 方向是①放松买点 regime 门控(允许平/刚转多的回踩买, 而非严格要求 EMA_f≥EMA_s),
       或②把 D 的「拐点过滤」作为共振买点的二级确认(而非主信号), 而非独立发生器。两者都需重新 walk-forward 定参, 且本样本(21日×5标)仍偏小。"""
    html = html.replace('<p id="concl">（由下方脚本依据汇总指标自动生成）</p>', f'<p>{concl}</p>')

    outp = os.path.join(OUT, 'd_candidate_report.html')
    open(outp, 'w', encoding='utf-8').write(html)
    print('wrote', outp, len(html), 'bytes')


if __name__ == '__main__':
    main()
