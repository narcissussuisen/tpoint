#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_target_state_report.py —— 目标态运行总览报告生成器（S6）

汇总:
  - T1~T4 核心验收（general 双向 / v4 灰度 / 对比报告 / 生产信号落盘）
  - T5~T8 数据质量哨兵（复用 target_state_sentinel）
  - T9 生产复盘交叉验证（review_<date>.json 存在且含逐标的复盘数据）
产出: output/target_state_run_<date>.html（自包含，验收矩阵 + 关键指标）

用法: python scripts/build_target_state_report.py [YYYY-MM-DD ...]
"""
import os, sys, json, datetime, math

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from target_state_sentinel import check_symbol, MIN_BARS  # noqa: E402

MAX_SIGNALS = 12


def run_checks(date):
    C = {}
    gen_path = os.path.join(OUT, f'general_signals_{date}.json')
    cmp_path = os.path.join(OUT, f'v4_gray_compare_{date}.json')
    rev_path = os.path.join(OUT, f'review_{date}.json')
    rev_html = os.path.join(OUT, f'review_{date}.html')

    gen = json.load(open(gen_path, encoding='utf-8')) if os.path.exists(gen_path) else None
    cmp = json.load(open(cmp_path, encoding='utf-8')) if os.path.exists(cmp_path) else None
    rev = json.load(open(rev_path, encoding='utf-8')) if os.path.exists(rev_path) else None

    syms = (gen or {}).get('symbols', {})
    rows = (cmp or {}).get('rows', [])

    # T1 通用双向
    t1 = all(s.get('n_b', 0) > 0 and s.get('n_s', 0) > 0 for s in syms.values())
    C['T1_general_bidirectional'] = {'pass': t1, 'detail': ' / '.join(
        f"{k}: B{v.get('n_b')}/S{v.get('n_s')}" for k, v in syms.items()) or '无标的'}
    # T2 v4 灰度 B>0
    t2 = all((r.get('v4_gray') or {}).get('n_b', 0) > 0 for r in rows if r.get('ok'))
    C['T2_v4_gray_runs'] = {'pass': t2, 'detail': ' / '.join(
        f"{r.get('sym')}: v4B{(r.get('v4_gray') or {}).get('n_b')}" for r in rows if r.get('ok')) or '无行'}
    # T3 对比报告
    t3 = cmp is not None and 'v4_promote_recommend' in (cmp or {})
    C['T3_compare_report'] = {'pass': t3, 'detail': str((cmp or {}).get('v4_promote_recommend'))}
    # T4 生产信号落盘
    t4 = gen is not None and 'symbols' in gen and len(gen['symbols']) > 0
    C['T4_general_signals_file'] = {'pass': t4, 'detail': gen_path}
    # T5~T8 哨兵
    sentinel_ok = True
    sentinel_rows = []
    for sym, s in syms.items():
        r = check_symbol(sym, s)
        sentinel_rows.append(r)
        sentinel_ok = sentinel_ok and r['all_pass']
    C['T5_T8_sentinel'] = {'pass': sentinel_ok, 'detail': '全标的哨兵通过' if sentinel_ok else '存在哨兵 FAIL'}
    # T9 生产复盘
    t9 = rev is not None and 'symbols' in rev and len(rev.get('symbols', {})) > 0 and os.path.exists(rev_html)
    C['T9_review_html'] = {'pass': t9, 'detail': f"{rev_html} ({os.path.getsize(rev_html) if os.path.exists(rev_html) else 0}B)" if t9 else rev_html}
    return {'gen': gen, 'cmp': cmp, 'rev': rev, 'C': C,
            'rows': rows, 'syms': syms, 'sentinel_rows': sentinel_rows}


def build_html(date, R):
    C, rows, syms, rev = R['C'], R['rows'], R['syms'], R['rev']
    gen = R['gen']
    all_pass = all(v['pass'] for v in C.values())

    # 逐标的对比表
    trs = []
    for r in rows:
        g, v = r.get('general', {}), r.get('v4_gray') or {}
        diff = (v.get('total_ret') or 0) - (g.get('total_ret') or 0)
        better = 'v4' if diff > 0 else ('通用' if diff < 0 else '持平')
        trs.append(f"""<tr>
<td>{r.get('sym')}</td><td>{r.get('name','')}</td>
<td>B{g.get('n_b')}/S{g.get('n_s')} · {g.get('trips')}对</td>
<td>{g.get('wr')}%</td>
<td style="color:{'#d32f2f' if (g.get('total_ret') or 0)>=0 else '#2e7d32'}">{g.get('total_ret')}%</td>
<td>B{v.get('n_b')}/S{v.get('n_s')} · {v.get('trips')}对</td>
<td>{v.get('wr')}%</td>
<td style="color:{'#d32f2f' if (v.get('total_ret') or 0)>=0 else '#2e7d32'}">{v.get('total_ret')}%</td>
<td>{better} ({diff:+.2f}pp)</td></tr>""")
    cmp_table = '<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px">' \
        '<tr style="background:#eef2f7"><th>标的</th><th>名称</th><th>通用 B/S·配对</th><th>WR</th><th>净%</th>' \
        '<th>v4灰 B/S·配对</th><th>WR</th><th>净%</th><th>v4 vs 通用</th></tr>' + ''.join(trs) + '</table>'

    # 验收矩阵
    acc_rows = ''.join(
        f'<tr><td>{k}</td><td>{"✅ PASS" if v["pass"] else "❌ FAIL"}</td><td style="font-size:12px;color:#555">{v["detail"]}</td></tr>'
        for k, v in C.items())

    # 哨兵明细
    sent_tbl = ''
    for sr in R['sentinel_rows']:
        for k, c in sr['checks'].items():
            sent_tbl += f'<tr><td>{sr["sym"]}</td><td>{k}</td><td>{"✅" if c["pass"] else "❌"}</td><td style="font-size:12px;color:#555">{c["detail"]}</td></tr>'

    # 复盘摘要（legacy 生产路径交叉验证）
    rev_lines = ''
    if rev:
        for sym, s in (rev.get('symbols') or {}).items():
            sm = s.get('summary') or {}
            rev_lines += f'<li>{sym} {s.get("name","")}: 信号{sm.get("n_signals")}次(买{sm.get("n_B")}/卖{sm.get("n_S")}/出{sm.get("n_X")}) 有效买{sm.get("valid_B")} 有效卖{sm.get("valid_S")} 日涨跌{s.get("stats",{}).get("day_chg_pct")}%</li>'

    verdict = ('✅ 目标态达成' if all_pass else '❌ 目标态未达成') + \
        ('（T1-T9 全部 PASS）' if all_pass else '（存在 FAIL，见验收矩阵）')

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 目标态运行总览 {date}</title></head>
<body style="margin:0;background:#f5f6f8;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:1100px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#1f2a44,#2d3b5e);color:#fff;padding:22px 26px;border-radius:12px">
<h1 style="margin:0 0 6px;font-size:22px">tpoint 目标态运行总览 · {date}</h1>
<p style="margin:0;opacity:.85">通用算法驱动 watchlist + v4 灰度测试 ｜ 引擎=general（use_general_engine=true）</p>
</div>
<div style="background:#fff;border-radius:12px;padding:20px;margin-top:16px;border:2px solid {'#2e7d32' if all_pass else '#d32f2f'}">
<div style="font-size:18px;font-weight:700;color:{'#2e7d32' if all_pass else '#d32f2f'}">{verdict}</div>
<div style="margin-top:8px;font-size:13px;color:#666">总判定 = T1∧T2∧T3∧T4∧T5∧T6∧T7∧T8∧T9 全 PASS（{sum(1 for v in C.values() if v['pass'])}/{len(C)}）</div>
</div>
<h2 style="font-size:16px;margin:22px 0 8px">1. 通用 vs v4 灰度 对比（watchlist 标的）</h2>
{cmp_table}
<h2 style="font-size:16px;margin:22px 0 8px">2. 验收矩阵（T1-T9）</h2>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px">
<tr style="background:#eef2f7"><th style="width:210px">用例</th><th style="width:90px">判定</th><th>证据</th></tr>{acc_rows}</table>
<h2 style="font-size:16px;margin:22px 0 8px">3. 数据质量哨兵明细（T5-T8）</h2>
<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px">
<tr style="background:#eef2f7"><th>标的</th><th>检查</th><th style="width:70px">结果</th><th>详情</th></tr>{sent_tbl}</table>
<h2 style="font-size:16px;margin:22px 0 8px">4. 生产复盘交叉验证（legacy 路径，系统存活证明）</h2>
<ul style="font-size:13px;line-height:1.9">{rev_lines or '<li>无复盘数据</li>'}</ul>
<p style="font-size:12px;color:#888;margin-top:22px">generated at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 数据源: mootdx 实时 1m（近 3-4 天窗口）｜ 性能目标(G-F1 WR≥55%)需离线长回测另验</p>
</div></body></html>"""
    return html


def main():
    dates = sys.argv[1:] or [datetime.date.today().strftime('%Y-%m-%d')]
    for d in dates:
        R = run_checks(d)
        html = build_html(d, R)
        out = os.path.join(OUT, f'target_state_run_{d}.html')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(html)
        all_pass = all(v['pass'] for v in R['C'].values())
        print(f"[{d}] 总览报告 -> {out} ({len(html.encode('utf-8'))}B) 总判定={'✅ PASS' if all_pass else '❌ FAIL'}")
        if not all_pass:
            for k, v in R['C'].items():
                if not v['pass']:
                    print(f"  FAIL: {k} {v['detail']}")


if __name__ == '__main__':
    main()
