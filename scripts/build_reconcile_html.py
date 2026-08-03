#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""build_reconcile_html.py — 生产vs回测 对账报告 HTML 生成器（R0 基建 · 2026-08-03）

解决问题：对账产物 reconcile_<date>.json 是纯 JSON（代码形态），飞书推送可读性差。
本脚本将其渲染为自包含深色主题 HTML（信号对比清单 + 统计数据 + 差异标注 + round-trip），
由 run_daily_review.bat 第7步调用并经 push_feishu_html.py 上传飞书云空间发链接。

数据源（全部已存在，口径与 prod_vs_bt_reconcile.py 一致）：
  output/reconcile_<date>.json   对账结果（per-sym + pool）
  output/review_<date>.json      复盘复算信号明细（rows）
  data/roundtrip/<date>.jsonl    round-trip 配对记录（source=live|recalc）
  data/push_audit.jsonl          实盘推送审计（明细源之一）

CLI：python build_reconcile_html.py [YYYY-MM-DD]（缺省=今天）
产物：output/reconcile_<date>.html
"""
import os, sys, json, io, datetime, html as _html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')
RT_DIR = os.path.join(ROOT, 'data', 'roundtrip')
AUDIT = os.path.join(ROOT, 'data', 'push_audit.jsonl')
CST = datetime.timezone(datetime.timedelta(hours=8))

TYPE_CN = {'B': '买入', 'S': '卖出', 'X': '出场'}
TYPE_COLOR = {'B': '#1D9E75', 'S': '#E24B4A', 'X': '#378ADD'}


def esc(x):
    return _html.escape('' if x is None else str(x))


def load_json(p):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return None


def load_roundtrip(date):
    p = os.path.join(RT_DIR, f'{date}.jsonl')
    rows = []
    if os.path.exists(p):
        for line in io.open(p, encoding='utf-8'):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def load_audit_today(date):
    rows = []
    if os.path.exists(AUDIT):
        for line in io.open(AUDIT, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('ts', '').startswith(date):
                rows.append(r)
    return rows


def kpi(label, val, sub='', cls=''):
    return (f'<div class="metric {cls}"><div class="lbl">{esc(label)}</div>'
            f'<div class="val">{esc(val)}</div><div class="hint">{esc(sub)}</div></div>')


def render(date, rec, review, rts, audit):
    pool = rec.get('pool', {})
    syms = rec.get('symbols', {})

    wr_prod = pool.get('wr_prod_exec')
    wr_recalc = pool.get('wr_recalc')
    g1 = pool.get('g1_pp')
    n_live = pool.get('n_live_trips', 0)
    n_recalc = pool.get('n_recalc_trips', 0)
    note = pool.get('note', '')

    # ---- per-sym 对比行 ----
    cmp_rows = []
    alert_items = []
    for sym, r in syms.items():
        name = r.get('name', sym)
        lc = r.get('live_counts', {'B': 0, 'S': 0, 'total': 0})
        live_total = lc.get('total', 0)
        recalc_n = r.get('recalc_n_signals', 0)
        delta = r.get('delta_total', 0)
        detail_n = r.get('live_detail_n', 0)
        if r.get('error'):
            verdict = f'<span class="pill bad">{esc(r["error"])}</span>'
        elif delta == 0 and live_total == recalc_n:
            verdict = '<span class="pill ok">一致</span>'
        elif delta > 0:
            verdict = f'<span class="pill warn">实盘+{delta}(多推/重放)</span>'
        else:
            verdict = f'<span class="pill bad">实盘{delta}(漏推/抑制)</span>'
        if delta != 0:
            alert_items.append(f'{name}({sym})：实盘 {live_total} vs 复算 {recalc_n}（delta {delta:+d}）')
        if live_total > 0 and detail_n == 0:
            alert_items.append(f'{name}({sym})：state 计数 {live_total} 但无推送明细（audit/signal.txt 缺失）')
        cmp_rows.append(
            f'<tr><td>{esc(name)}<div class="sub2">{esc(sym)}</div></td>'
            f'<td class="num">{live_total}<div class="sub2">B{lc.get("B",0)}/S{lc.get("S",0)}</div></td>'
            f'<td class="num">{detail_n}</td>'
            f'<td class="num">{recalc_n}<div class="sub2">BS{r.get("recalc_n_bs",0)}</div></td>'
            f'<td class="num" style="color:{"#E24B4A" if delta<0 else ("#EF9F27" if delta>0 else "#1D9E75")}">{delta:+d}</td>'
            f'<td>{verdict}</td></tr>')

    # ---- 复算信号清单（review rows）----
    sig_sections = []
    rsyms = (review or {}).get('symbols', {})
    for sym, sdata in rsyms.items():
        rows = sdata.get('rows') or []
        nm = sdata.get('name', sym)
        if not rows:
            continue
        trs = []
        for r in rows:
            t = str(r.get('time', ''))[11:16]
            typ = r.get('type', '')
            color = TYPE_COLOR.get(typ, '#888')
            valid = r.get('valid')
            valid_html = ('<span class="pill ok">有效</span>' if valid else
                          ('<span class="pill bad">失效</span>' if valid is False else '<span class="pill gray">—</span>'))
            reason = r.get('reason') or r.get('tag') or ''
            trs.append(
                f'<tr><td class="num">{esc(t)}</td>'
                f'<td><span style="color:{color};font-weight:600">{esc(TYPE_CN.get(typ, typ))}</span></td>'
                f'<td class="num">{esc(r.get("price"))}</td>'
                f'<td class="num">{esc(r.get("day_chg"))}%</td>'
                f'<td class="num">{esc(r.get("max_fav_pct")) if r.get("max_fav_pct") is not None else "—"}</td>'
                f'<td>{valid_html}</td><td class="reason">{esc(reason)}</td></tr>')
        sig_sections.append(
            f'<h3>{esc(nm)} <span class="sub2">{esc(sym)} · {len(rows)} 条</span></h3>'
            '<table><thead><tr><th>时间</th><th>类型</th><th>价格</th><th>当日涨跌</th>'
            '<th>最大有利%</th><th>有效性</th><th>触发原因</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>')
    if not sig_sections:
        sig_sections.append('<div class="empty">当日复算无信号</div>')

    # ---- round-trip 明细 ----
    rt_trs = []
    for t in rts:
        src = t.get('source', '')
        src_html = (f'<span class="pill {"info" if src=="live" else "gray"}">{"实盘" if src=="live" else "复算"}</span>')
        ret = t.get('ret_pct', 0)
        ret_color = '#1D9E75' if ret > 0 else '#E24B4A'
        rt_trs.append(
            f'<tr><td>{src_html}</td><td>{esc(t.get("sym"))}</td>'
            f'<td class="num">{esc(t.get("entry_price"))}</td><td class="num">{esc(t.get("exit_price"))}</td>'
            f'<td class="num">{esc(t.get("exit_reason"))}</td><td class="num">{esc(t.get("hold_bars"))}</td>'
            f'<td class="num" style="color:{ret_color};font-weight:600">{ret:+.3f}%</td></tr>')
    rt_html = ('<table><thead><tr><th>来源</th><th>标的</th><th>入场价</th><th>出场价</th>'
               '<th>出场原因</th><th>持仓bar</th><th>净收益</th></tr></thead>'
               f'<tbody>{"".join(rt_trs)}</tbody></table>' if rt_trs else
               '<div class="empty">当日无 round-trip 配对（无B建仓或明细不足）</div>')

    # ---- 实盘推送明细（audit）----
    au_trs = []
    for a in audit:
        typ = a.get('type', '')
        color = TYPE_COLOR.get(typ, '#888')
        au_trs.append(
            f'<tr><td class="num">{esc(str(a.get("ts",""))[11:19])}</td><td>{esc(a.get("sym"))}</td>'
            f'<td><span style="color:{color};font-weight:600">{esc(TYPE_CN.get(typ, typ))}</span></td>'
            f'<td class="num">{esc(a.get("price"))}</td>'
            f'<td class="num">{"✓" if a.get("ok") else "✗"}</td></tr>')
    audit_html = ('<table><thead><tr><th>推送时间</th><th>标的</th><th>类型</th><th>价格</th><th>成功</th></tr></thead>'
                  f'<tbody>{"".join(au_trs)}</tbody></table>' if au_trs else
                  '<div class="empty">当日 push_audit 无记录（存储链路待修）</div>')

    alert_html = ('<ul>' + ''.join(f'<li>{esc(x)}</li>' for x in alert_items) + '</ul>'
                  if alert_items else '<div class="empty">无异常（实盘与复算信号数全部一致）</div>')

    g1_txt = '--' if g1 is None else f'{g1:+.1f}pp'
    wrp_txt = '--' if wr_prod is None else f'{wr_prod:.1f}%'
    wrr_txt = '--' if wr_recalc is None else f'{wr_recalc:.1f}%'
    low_sample = (n_live + n_recalc) < 10

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 生产vs回测 对账报告 {date}</title>
<style>
body{{background:#16181d;color:#E8E8EC;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6;padding:22px;max-width:1100px;margin:0 auto}}
h1{{font-size:20px;font-weight:600;margin:0 0 4px}}
h2{{font-size:16px;font-weight:600;margin:26px 0 12px;border-left:3px solid #1D9E75;padding-left:10px}}
h3{{font-size:14px;font-weight:600;margin:18px 0 8px;color:#A8AEB8}}
.sub{{color:#6E747E;font-size:12px;margin-bottom:18px}}
.sub2{{color:#6E747E;font-size:11px;font-weight:400}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:8px}}
.metric{{background:#1f232b;border-radius:8px;padding:12px 14px}}
.metric .lbl{{color:#6E747E;font-size:11px;margin-bottom:3px}}
.metric .val{{font-size:21px;font-weight:600}}
.metric .hint{{color:#6E747E;font-size:11px;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0 14px}}
th,td{{padding:7px 9px;text-align:left;border-bottom:1px solid #2f3540}}
th{{color:#A8AEB8;background:#1f232b;font-size:11.5px;font-weight:600}}
td.num{{text-align:center;font-variant-numeric:tabular-nums}}
td.reason{{color:#A8AEB8;font-size:11.5px;max-width:300px}}
tr:hover{{background:#1f232b}}
.pill{{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600}}
.pill.ok{{background:#0f3a2e;color:#1D9E75}}.pill.warn{{background:#3a2e0e;color:#EF9F27}}
.pill.bad{{background:#3a1a14;color:#E24B4A}}.pill.gray{{background:#2a2f38;color:#A8AEB8}}
.pill.info{{background:#163a5a;color:#378ADD}}
.empty{{color:#6E747E;font-size:12.5px;padding:10px 0}}
.warn-box{{background:#2a2113;border:1px solid #EF9F27;border-radius:8px;padding:10px 14px;font-size:12px;color:#EF9F27;margin:8px 0}}
ul{{margin:6px 0 6px 18px}}li{{margin:3px 0;font-size:12.5px}}
.foot{{margin-top:24px;color:#6E747E;font-size:11px;border-top:1px solid #2f3540;padding-top:10px}}
</style></head><body>
<h1>tpoint 生产 vs 回测 · 每日对账报告</h1>
<div class="sub">{esc(date)} ｜ 生成于 {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} ｜ 口径：simulate_day 同源 round-trip 配对（trail0.4/0.6+S出场，万一+印花+滑点2bps）</div>

<h2>一、池级汇总（滚动验收口径：单日&lt;10笔仅参考，验收用滚动20交易日）</h2>
<div class="grid">
{kpi('WR_prod_exec（实盘对账胜率）', wrp_txt, f'live round-trip {n_live} 笔', 'acc' if (wr_prod or 0) >= 55 else '')}
{kpi('WR_recalc（复算胜率）', wrr_txt, f'recalc round-trip {n_recalc} 笔')}
{kpi('G1（执行差距）', g1_txt, 'WR_recalc − WR_prod_exec')}
{kpi('信号笔数（live/recalc）', f'{n_live}/{n_recalc}', '低样本' if low_sample else '样本可用')}
{kpi('G2（数据源差距）', '--', '由每周探针补充')}
</div>
{f'<div class="warn-box">⚠️ {esc(note)}</div>' if note else ''}

<h2>二、实盘 vs 复算 信号数对比（state.json 权威计数 / detect_for 复算）</h2>
<table><thead><tr><th>标的</th><th>实盘推送</th><th>推送明细</th><th>复算信号</th><th>delta</th><th>判定</th></tr></thead>
<tbody>{''.join(cmp_rows)}</tbody></table>

<h2>三、差异与异常</h2>
{alert_html}

<h2>四、实盘推送明细（push_audit.jsonl）</h2>
{audit_html}

<h2>五、复算信号清单（生产同源 detect_for · forward 有效性验证）</h2>
{''.join(sig_sections)}

<h2>六、Round-trip 配对明细（净收益口径）</h2>
{rt_html}

<div class="foot">数据源：reconcile_{date}.json / review_{date}.json / roundtrip/{date}.jsonl / push_audit.jsonl ｜ 实盘 entry 取信号 bar close（防实时源 vs 历史复权错位）｜ 本报告由 build_reconcile_html.py 自动生成</div>
</body></html>"""


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.datetime.now(CST).strftime('%Y-%m-%d')
    rec = load_json(os.path.join(OUT, f'reconcile_{date}.json'))
    if rec is None:
        print(f'❌ 找不到 output/reconcile_{date}.json（先跑 prod_vs_bt_reconcile.py）')
        sys.exit(1)
    review = load_json(os.path.join(OUT, f'review_{date}.json'))
    rts = load_roundtrip(date)
    audit = load_audit_today(date)
    html = render(date, rec, review, rts, audit)
    out_path = os.path.join(OUT, f'reconcile_{date}.html')
    with io.open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'✅ {out_path}（{len(html)//1024}KB）')


if __name__ == '__main__':
    main()
