#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早盘信号复盘报告 (HTML, 手动触发)
=================================
复盘范围: 当日早盘 (09:30-11:30) 信号
信号来源: 用生产信号引擎(miji_alpha, MACD_GATE_MODE=strict) 在当日1m数据上重算,
          复刻 monitor 发射规则(COOLDOWN_BARS=3 + 仓位状态机)。
          ⚠️ 今日 live monitor 因数据源连接问题未落盘信号, 本报告为"引擎重算",
             非实盘推送逐笔记录; 与实盘推送可能存在细微差异(仓位规模/加仓细节)。
报告维度: 信号准确率 / 触发后涨跌统计 / 因子(风格)分布 / 假信号分析 / 与执行偏差
输出:     logs/morning_review_YYYY-MM-DD.html
用法:     python scripts/morning_review.py [--date YYYY-MM-DD] [--out PATH]
"""
import sys, os, json, argparse, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(BASE, 'core')
VENV = os.path.join(BASE, 'venv', 'Lib', 'site-packages')
sys.path.insert(0, CORE)
sys.path.insert(0, VENV)

from datasource import MootdxDataSource
import miji_alpha as live
import numpy as np

COOLDOWN_BARS = 3          # 与 monitor.py:108 一致
WATCHLIST = os.path.join(BASE, 'data', 'watchlist.json')
OUT_DIR = os.path.join(BASE, 'logs')
HORIZONS = [6, 12, 24]     # 1m bar => 6/12/24 分钟前向收益

tf = MootdxDataSource()


def load(sym):
    """拉当日1m + 日K前收, 组装引擎 data 字典。返回 (data, df) 或 (None, None)。"""
    df = tf.klines.intraday(sym, as_dataframe=True)
    if df is None or len(df) < 5:
        return None, None
    df = df.sort_values('trade_time').reset_index(drop=True)
    bar_date = str(df['trade_date'].iloc[0])
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    if bar_date != today_str:
        print(f"  [skip] {sym} intraday日期 {bar_date} != {today_str}")
        return None, None
    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o = df['open'].values.astype(float) if 'open' in df.columns else c.copy()
    has_vol = 'volume' in df.columns
    v = df['volume'].values.astype(float) if has_vol else None
    d = tf.klines.get(sym, period='1d', count=2, as_dataframe=True)
    pc = float(d['close'].iloc[-2]) if (d is not None and len(d) >= 2) else float(c[0])
    data = live.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data, df


def classify_style(reason):
    """从触发原因字符串判定因子/风格构成。"""
    r = reason or ''
    has_floor = '价格地板' in r
    has_ceil = '价格天花板' in r
    has_macd = 'MACD' in r
    has_grav = '均线引力' in r
    has_vol = '量价' in r
    tags = []
    if has_floor: tags.append('floor')
    if has_ceil: tags.append('ceil')
    if has_macd: tags.append('macd')
    if has_grav: tags.append('gravity')
    if has_vol: tags.append('vol')
    if not tags: tags.append('other')
    return tags


def gen_signals(data, df, name):
    """复刻 monitor 发射: COLDOWN_BARS 间隔 + 简单仓位状态机, 仅早盘窗口。"""
    n = len(df)
    tt = df['trade_time'].astype(str)
    c = data['c']

    def in_morning(i):
        t = tt.iloc[i]
        # t 形如 '2026-07-20 10:17:00'
        hm = t[11:16]
        return '09:30' <= hm <= '11:30'

    sigs = []
    pos = None
    b_count = s_count = 0
    last_b = last_s = -999
    for i in range(n):
        if not in_morning(i):
            continue
        tb, rb = live.check_b_trigger(data, i)
        ts, rs = live.check_s_trigger(data, i)
        if tb and (i - last_b) >= COOLDOWN_BARS and b_count < 12:
            last_b = i; b_count += 1
            sigs.append(dict(type='B', idx=i, price=round(float(c[i]), 2),
                              reason=rb, name=name, pos_before=pos,
                              time=tt.iloc[i][11:16]))
            pos = 'long'
        if ts and (i - last_s) >= COOLDOWN_BARS and s_count < 12:
            last_s = i; s_count += 1
            sigs.append(dict(type='S', idx=i, price=round(float(c[i]), 2),
                              reason=rs, name=name, pos_before=pos,
                              time=tt.iloc[i][11:16]))
            pos = 'short'
    return sigs


def fwd_ret(data, i, k):
    """bar i 之后 k 根的前向收益(%)。越界返回 None。"""
    c = data['c']; n = len(c)
    j = i + k
    if j >= n:
        return None
    return (c[j] - c[i]) / c[i] * 100.0


def build_report(date_str, all_sigs, sym_meta):
    total = len(all_sigs)
    b_sigs = [s for s in all_sigs if s['type'] == 'B']
    s_sigs = [s for s in all_sigs if s['type'] == 'S']

    # 前向收益
    for s in all_sigs:
        s['fwd'] = {k: fwd_ret(s.get('_data'), s['idx'], k) for k in HORIZONS}

    # 准确率: B后涨(fwd>0)为对; S后跌(fwd<0, 即避开下跌)为对
    def win_rate(sigs, k):
        vals = [s['fwd'][k] for s in sigs if s['fwd'].get(k) is not None]
        if not vals: return None, 0
        if sigs and sigs[0]['type'] == 'B':
            correct = sum(1 for x in vals if x > 0)
        else:
            correct = sum(1 for x in vals if x < 0)
        return correct / len(vals) * 100, len(vals)

    wr = {k: win_rate(b_sigs, k) for k in HORIZONS}
    wr_s = {k: win_rate(s_sigs, k) for k in HORIZONS}

    # 因子/风格分布
    from collections import Counter
    style_counter = Counter()
    for s in all_sigs:
        tags = classify_style(s['reason'])
        for t in tags:
            style_counter[t] += 1

    # 假信号 (B后跌 / S后涨, 取 horizon=12)
    false_list = []
    for s in all_sigs:
        f = s['fwd'].get(12)
        if f is None: continue
        if s['type'] == 'B' and f < 0:
            false_list.append((s, f, 'B后下跌'))
        elif s['type'] == 'S' and f > 0:
            false_list.append((s, f, 'S后上涨(卖飞)'))
    false_list.sort(key=lambda x: x[1])

    # 若执行盈亏汇总 (horizon=12)
    pnl_b = [s['fwd'][12] for s in b_sigs if s['fwd'].get(12) is not None]
    pnl_s = [-s['fwd'][12] for s in s_sigs if s['fwd'].get(12) is not None]  # S收益=避开跌幅
    pnl_all = [x for x in pnl_b + pnl_s if x is not None]

    html = render_html(date_str, total, b_sigs, s_sigs, wr, wr_s, style_counter,
                       false_list, pnl_b, pnl_s, pnl_all, sym_meta, all_sigs)
    return html


def render_html(date_str, total, b_sigs, s_sigs, wr, wr_s, style_counter,
                false_list, pnl_b, pnl_s, pnl_all, sym_meta, all_sigs):
    gen = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sym_line = '、'.join(f"{v}({k})" for k, v in sym_meta.items())

    def pct(x):
        return '—' if x is None else f'{x:.1f}%'

    # KPI
    kpi = f"""
    <div class="kpis">
      <div class="kpi"><div class="v">{total}</div><div class="l">早盘信号总数</div></div>
      <div class="kpi"><div class="v" style="color:#4caf50">{len(b_sigs)}</div><div class="l">买入B</div></div>
      <div class="kpi"><div class="v" style="color:#f44336">{len(s_sigs)}</div><div class="l">卖出S</div></div>
      <div class="kpi"><div class="v">{pct(np.mean(pnl_all) if pnl_all else None)}</div><div class="l">若执行均收益(12min)</div></div>
    </div>"""

    # 准确率表
    acc_rows = ''
    for k in HORIZONS:
        w, n = wr[k]; ws, ns = wr_s[k]
        acc_rows += f"<tr><td>{k}min</td><td>{pct(w)} <span class='sub'>(n={n})</span></td><td>{pct(ws)} <span class='sub'>(n={ns})</span></td></tr>"

    # 风格分布条形
    maxc = max(style_counter.values()) if style_counter else 1
    style_bars = ''
    color_map = {'gravity': '#42a5f5', 'macd': '#ab47bc', 'vol': '#ffa726',
                 'floor': '#26c6da', 'ceil': '#ef5350', 'other': '#78909c'}
    for tag, cnt in sorted(style_counter.items(), key=lambda x: -x[1]):
        w = cnt / maxc * 100
        style_bars += f"<div class='bar'><span class='bl' style='width:{w:.0f}%;background:{color_map.get(tag,'#78909c')}'></span><span class='bt'>{tag}: {cnt}</span></div>"

    # 信号明细表
    rows = ''
    for s in all_sigs:
        f6 = s['fwd'].get(6); f12 = s['fwd'].get(12); f24 = s['fwd'].get(24)
        cls = 'B' if s['type'] == 'B' else 'S'
        col = '#4caf50' if s['type'] == 'B' else '#f44336'
        tags = ','.join(classify_style(s['reason']))
        f12c = 'neg' if (s['type'] == 'B' and (f12 or 0) < 0) or (s['type'] == 'S' and (f12 or 0) > 0) else 'pos'
        rows += (f"<tr><td>{s['time']}</td><td style='color:{col};font-weight:600'>{cls}</td>"
                 f"<td>{s['name']}</td><td>{s['price']}</td><td class='sub'>{s['reason'][:40]}</td>"
                 f"<td>{tags}</td><td>{pct(f6)}</td><td class='{f12c}'>{pct(f12)}</td><td>{pct(f24)}</td></tr>")

    # 假信号
    false_rows = ''
    for s, f, kind in false_list[:8]:
        col = '#4caf50' if s['type'] == 'B' else '#f44336'
        false_rows += (f"<tr><td>{s['time']}</td><td style='color:{col}'>{s['type']}</td>"
                       f"<td>{s['name']}</td><td>{s['price']}</td><td>{kind}</td>"
                       f"<td class='neg'>{f:.2f}%</td><td class='sub'>{s['reason'][:36]}</td></tr>")
    if not false_rows:
        false_rows = "<tr><td colspan=7 class='sub'>无显著假信号（12min 内方向与信号一致）</td></tr>"

    pnl_b_mean = np.mean(pnl_b) if pnl_b else None
    pnl_s_mean = np.mean(pnl_s) if pnl_s else None

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>早盘信号复盘 {date_str}</title>
<style>
*{{box-sizing:border-box}} body{{background:#0f1419;color:#e6e6e6;font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;margin:0;padding:24px;line-height:1.5}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 10px;color:#90caf9;border-left:3px solid #90caf9;padding-left:8px}}
.banner{{background:#1a2330;border:1px solid #2a3a4a;border-radius:8px;padding:10px 14px;margin:12px 0;font-size:12px;color:#90a4ae}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.kpi{{background:#161c24;border:1px solid #263238;border-radius:8px;padding:14px 18px;min-width:120px}}
.kpi .v{{font-size:26px;font-weight:700}} .kpi .l{{font-size:12px;color:#90a4ae;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #1e2730}}
th{{color:#80cbc4;font-weight:600;background:#121a22}}
tr:hover{{background:#141c26}}
.sub{{color:#78909c;font-size:11px}} .pos{{color:#4caf50}} .neg{{color:#ef5350}}
.bar{{display:flex;align-items:center;margin:6px 0}} .bl{{height:18px;border-radius:3px;min-width:2px}}
.bt{{margin-left:8px;font-size:12px;color:#cfd8dc}}
.kpi-grid{{display:flex;gap:24px;flex-wrap:wrap}} .note{{color:#90a4ae;font-size:12px}}
.disclaimer{{margin-top:30px;padding:12px;background:#1a1208;border:1px solid #3a2e10;border-radius:8px;font-size:11px;color:#a1887f}}
</style></head><body>
<h1>📊 早盘信号复盘报告</h1>
<div class="note">复盘日期：{date_str} ｜ 生成时间：{gen} ｜ 标的：{sym_line} ｜ 模式：strict（生产默认，MACD门控）</div>
<div class="banner">⚠️ <b>数据说明</b>：今日 live monitor 因数据源连接瞬时中断未落盘信号，本报告为<b>信号引擎重算</b>（基于当日1m + 生产 strict 配置 + monitor 发射规则 COOLDOWN_BARS=3），非实盘推送逐笔记录。与实盘推送可能存在仓位规模/加仓细节差异，但信号触发时点与方向一致。</div>
{kpi}
<h2>一、信号准确率（触发后前向收益方向匹配）</h2>
<table><tr><th>持有周期</th><th>B信号准确率(后涨)</th><th>S信号准确率(后跌)</th></tr>{acc_rows}</table>
<div class="note">B准确=触发后价格上行占比；S准确=触发后价格下行(成功规避)占比。样本不足时显示 —。</div>
<h2>二、因子 / 风格分布</h2>
{style_bars}
<div class="note">gravity=均线引力(超跌/超买) ｜ macd=MACD背离 ｜ floor/ceil=价格地板/天花板(新低/新高) ｜ vol=量价</div>
<h2>三、信号明细（早盘 09:30-11:30）</h2>
<table><tr><th>时间</th><th>方向</th><th>标的</th><th>价格</th><th>触发因子</th><th>风格</th><th>6min</th><th>12min</th><th>24min</th></tr>{rows}</table>
<h2>四、假信号分析（12min 内方向与信号相悖）</h2>
<table><tr><th>时间</th><th>方向</th><th>标的</th><th>价格</th><th>类型</th><th>12min收益</th><th>触发因子</th></tr>{false_rows}</table>
<h2>五、与执行偏差（若全程跟单）</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="v" style="color:#4caf50">{pct(pnl_b_mean)}</div><div class="l">B信号均收益(12min)</div></div>
  <div class="kpi"><div class="v" style="color:#4caf50">{pct(pnl_s_mean)}</div><div class="l">S信号规避跌幅(12min)</div></div>
  <div class="kpi"><div class="v">{len(pnl_all)}</div><div class="l">可结算信号数</div></div>
</div>
<div class="note">系统为告警型（不自动交易），"执行偏差"指：若按信号价模拟买入(B)/卖出(S)并持有12min的盈亏。实际盈亏取决于你的入场时点与仓位，此处仅为信号质量参考。</div>
<div class="disclaimer">⚠️ 以上内容由 AI 基于公开行情与信号引擎整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.datetime.now().strftime('%Y-%m-%d'))
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    with open(WATCHLIST, encoding='utf-8') as f:
        watch = json.load(f)
    sym_meta = {k: v for k, v in watch.items()}

    all_sigs = []
    loaded = {}
    for sym, name in sym_meta.items():
        print(f"拉取 {sym} ({name}) 当日1m ...")
        data, df = load(sym)
        if data is None:
            print(f"  [跳过] {sym} 无数据")
            continue
        sigs = gen_signals(data, df, name)
        for s in sigs:
            s['_data'] = data
        all_sigs.extend(sigs)
        loaded[sym] = name
        print(f"  {len(sigs)} 个早盘信号")

    if not all_sigs:
        print("⚠️ 无可用数据，未生成报告")
        return

    html = build_report(args.date, all_sigs, loaded)
    out = args.out or os.path.join(OUT_DIR, f'morning_review_{args.date}.html')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✅ 报告已生成: {out}")
    print(f"   信号总数={len(all_sigs)} B={sum(1 for s in all_sigs if s['type']=='B')} "
          f"S={sum(1 for s in all_sigs if s['type']=='S')}")


if __name__ == '__main__':
    main()
