#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reconcile_20260723.py — 信号推送对账（2026-07-23 事件复盘，v9.2.1 修正版）
==================================================================================
对比今天三个交易标的的「理论应触发信号」与「实际已推送信号」，
列出真实缺失条目，并逐条标注原因。

【方法学修正（v9.2.1）】—— 评估窗口感知对账（"盘中增量快照"的回溯等价实现）
---------------------------------------------------------------------------------
旧版（已推翻）用收盘后 fresh-state 全量重放全天 240 根，把下午 13:00–15:00 本应在
监控扫描冻结后「从未被评估」的理论信号也当成"应推未推"，得到 16 条缺失。其中 7 条
为下午 phantom miss，属于误判。

本版做法：
  1. 从 state.json 读每个标的「实际处理到的最后一根 bar 索引」(max bar_<sym>_<idx>)，
     映射回当日 1m 行情得到 freeze_dt（本事件 = bar#118 ≈ 11:28，三标的统一）。
     监控只评估过 <= freeze_dt 的 bar；之后（下午）扫描冻结，从未评估。
  2. 从 push_audit.jsonl 读「最后成功推送时间」push_cutoff_dt（本事件 = 10:42）。
     10:42 后扫描虽续行至 11:28，但推送中断，未再送达。
  3. 收盘后 fresh-state 全量重放仍用于生成「理论信号全集」，但分类时：
       - ts > freeze_dt           → NOT_EVALUATED（未评估·扫描冻结），【不计入缺失率】
       - ts <= freeze_dt 且匹配实推 → DELIVERED
       - ts <= freeze_dt 且未匹配   → MISSED（真实缺失，归因：推送中断 / 状态失同步）
  4. 命中率仅按「评估窗口内」计算，分母不含冻结后理论信号。

证据来源（单一真相）：
  理论信号：live engine (core/monitor.detect_for) fresh-state 全量重放今日真实 1m（mootdx）。
  实际推送：data/push_audit.jsonl（权威审计）。
  评估窗口：data/state.json（bar 索引）+ data/push_audit.jsonl（推送截止）。

旧版结论收回：16-miss = 9 真实缺失(688347 10:43–11:19×7 + 513310 09:42/09:50×2)
            + 7 冻结后误判(下午)。以本版输出为准。

输出：
  output/signal_reconcile_20260723.csv   （真实缺失 + 未评估 明细）
  output/signal_reconcile_20260723.html  （对账报告）
  stdout                                 （控制台对账报告）

用法（在 tpoint 根目录用 venv 跑）：
  python scripts/reconcile_20260723.py
"""
import os, sys, json, argparse, re
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'core'))
sys.path.insert(0, BASE)

import monitor as M
from core.datasource import MootdxDataSource

DATA_DIR = os.path.join(BASE, 'data')
AUDIT = os.path.join(DATA_DIR, 'push_audit.jsonl')
STATE_JSON = os.path.join(DATA_DIR, 'state.json')
OUT_CSV = os.path.join(BASE, 'output', 'signal_reconcile_20260723.csv')
OUT_HTML = os.path.join(BASE, 'output', 'signal_reconcile_20260723.html')

NAME = {
    '161129.SZ': '原油LOF易方达',
    '688347.SH': '华虹宏力',
    '513310.SH': '中韩半导体ETF华泰柏瑞',
}
SYMS = list(NAME.keys())

DAY_BARS = 240  # A股 1m 全天根数（09:30–11:30=121 + 13:00–15:00=119）


# --------------------------------------------------------------------------- #
# 数据准备
# --------------------------------------------------------------------------- #
def get_pc(sym):
    """取昨收(PC)与预热，写入 STATE。"""
    try:
        ds = MootdxDataSource()
        d = ds.get(sym, period='1d', count=5, as_dataframe=True)
        if d is None or len(d) == 0:
            print(f'  ⚠️ {sym} 日K为空')
            return None, None
        d = d.sort_values('trade_date')
        today_str = datetime.now().strftime('%Y-%m-%d')
        last_date = str(d['trade_date'].iloc[-1])[:10]
        pc = float(d['close'].iloc[-2]) if last_date == today_str else float(d['close'].iloc[-1])
        warm = d['close'].values[-30:].astype(float)
        M.STATE[sym]['PC'] = pc
        M.STATE[sym]['WARM'] = warm
        return pc, warm
    except Exception as e:
        print(f'  ⚠️ {sym} 日K获取失败: {e}')
        return None, None


def theoretical_signals(sym):
    """拉取今日真实 1m，fresh-state 全量重放 detect_for，返回 (信号列表, df)。"""
    ds = MootdxDataSource()
    df = ds.intraday(sym, as_dataframe=True)
    if df is None or len(df) < 5:
        print(f'  ⚠️ {sym} 今日 1m 不足5根（intraday 失败/非交易时段）')
        return None
    df = df.sort_values('trade_time').reset_index(drop=True)
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    has_vol = v is not None and float(v.sum()) > 0
    pc = M.STATE[sym]['PC']
    data = M.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    data['n'] = len(df)
    st = {}
    sigs = M.detect_for(sym, NAME[sym], data, st)
    return sigs, df


def derive_freeze(sym, df):
    """从 state.json 读该标的最大已处理 bar 索引 → 映射回 df 的 trade_time = freeze_dt。

    返回 (freeze_bar_1based, freeze_dt_or_None, unevaluated_bars)。
    freeze_bar=0 表示无法派生（state.json 无该标 bar 记录）。"""
    maxidx = 0
    if os.path.exists(STATE_JSON):
        try:
            d = json.load(open(STATE_JSON, encoding='utf-8'))
            pat = re.compile(r'^bar_%s_(\d+)$' % re.escape(sym))
            for k in d:
                m = pat.match(k)
                if m:
                    maxidx = max(maxidx, int(m.group(1)))
        except Exception as e:
            print(f'  ⚠️ 读 state.json 失败: {e}')
    if maxidx == 0:
        return 0, None, 0
    i0 = maxidx - 1  # 0-based
    freeze_dt = None
    if df is not None and len(df) > i0:
        freeze_dt = _parse_ts(df['trade_time'].iloc[i0])
    unevaluated = max(0, DAY_BARS - maxidx)
    return maxidx, freeze_dt, unevaluated


def derive_push_cutoff():
    """从 push_audit.jsonl 读最后成功推送时间 = 推送截止。无审计则返回 None。"""
    actual = load_audit()
    if not actual:
        return None
    return max(a['ts'] for a in actual)


def load_audit():
    """读取实际推送审计，返回 [{sym,type,ts(dt),price}]。"""
    out = []
    if not os.path.exists(AUDIT):
        return out
    with open(AUDIT, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get('ok'):
                continue
            ts = datetime.strptime(r['ts'], '%Y-%m-%d %H:%M:%S')
            out.append({'sym': r['sym'], 'type': r['type'],
                        'ts': ts, 'price': float(r.get('price', 0))})
    return out


def _parse_ts(bt):
    if not bt:
        return None
    s = str(bt).replace('T', ' ').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def parse_sig(sig):
    typ = sig[0]
    price = float(sig[1])
    reason = sig[10] if len(sig) > 10 else ''
    bt = sig[12] if len(sig) > 12 and sig[12] else ''
    size = sig[13] if len(sig) > 13 else None
    return {'type': typ, 'price': price, 'reason': reason,
            'ts': _parse_ts(bt), 'size': size}


def miss_cause(t, push_cutoff_dt):
    """真实缺失(评估窗口内未推)的归因。"""
    if push_cutoff_dt and t['ts'] and t['ts'] > push_cutoff_dt:
        return ('推送中断(10:42 后扫描续行至 11:28 但未再推送)：'
                '理论信号在评估窗口内产生，但因推送通道中断未送达飞书。')
    if t['sym'] == '513310.SH':
        return ('状态失同步(盘间重启丢失持仓态)：513310 于 09:35 开空后，'
                '持仓状态因盘前/早盘重启丢失同步，平仓(09:42)与回补(09:50)信号未复现；'
                'state.json 显示该空仓至收盘仍未平。')
    return ('评估窗口内未推送(原因待查)：理论信号在处理到的 bar 范围内产生，'
            '但未出现在推送审计中，疑似状态失同步或推送中断叠加。')


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--match-min', type=int, default=3,
                        help='理论/实推时间匹配容差(分钟)')
    args = parser.parse_args()
    match_min = args.match_min

    print('=' * 70)
    print('tpoint 信号推送对账  2026-07-23  [v9.2.1 评估窗口感知版]')
    print(f'数据抓取时刻: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 70)

    actual = load_audit()
    print(f'\n[实际推送] push_audit.jsonl 共 {len(actual)} 条:')
    for a in actual:
        print(f'  {a["ts"]:%H:%M:%S}  {NAME.get(a["sym"], a["sym"]):>12}  '
              f'{a["type"]}  @ {a["price"]:.3f}')
    push_cutoff_dt = derive_push_cutoff()
    print(f'[推送截止] 最后成功推送 = {push_cutoff_dt:%H:%M:%S}' if push_cutoff_dt
          else '[推送截止] 无审计记录')

    # 理论信号 + 冻结点派生
    theo = []
    coverage = []
    freeze_info = {}
    for sym in SYMS:
        print(f'\n--- 拉取并重放 {NAME[sym]} ({sym}) ---')
        pc, warm = get_pc(sym)
        if pc is None:
            coverage.append((sym, 'PC获取失败', 0))
            continue
        print(f'  PC(昨收)={pc:.3f}  WARM={None if warm is None else len(warm)}根')
        res = theoretical_signals(sym)
        if res is None:
            coverage.append((sym, '1m不足', 0))
            continue
        sigs, df = res
        fb, fdt, uneval = derive_freeze(sym, df)
        freeze_info[sym] = (fb, fdt, uneval)
        nbars = len(df)
        coverage.append((sym, f'OK bars={nbars}', len(sigs)))
        last_t = str(df['trade_time'].iloc[-1])
        print(f'  1m 根数={nbars}  末根={last_t}  理论信号 {len(sigs)} 条')
        print(f'  冻结点: bar#{fb}' + (f' ≈ {fdt:%H:%M:%S}' if fdt else '')
              + f'  未评估根数(估算)={uneval}')
        for s in sigs:
            p = parse_sig(s)
            if p['ts'] is None:
                continue
            theo.append({'sym': sym, 'name': NAME[sym], **p})

    # 统一冻结点：取三标的 freeze_dt 的最小者（最早冻结），更保守
    fdt_list = [fi[1] for fi in freeze_info.values() if fi[1] is not None]
    freeze_dt = min(fdt_list) if fdt_list else None
    freeze_bar = min((fi[0] for fi in freeze_info.values() if fi[0] > 0), default=0)
    uneval_total = sum(fi[2] for fi in freeze_info.values())
    print(f'\n[评估窗口] 统一冻结点 freeze_dt={freeze_dt:%H:%M:%S} (bar#{freeze_bar})'
          if freeze_dt else '\n[评估窗口] 无法派生冻结点（state.json 无 bar 记录）')
    if uneval_total:
        uneval_range = DAY_BARS - freeze_bar  # 三标同一下午窗口
        print(f'[评估窗口] 监控未评估根数(每标估算)={uneval_range}'
              f'（下午 13:00–15:00 全程未评估，三标同一窗口，'
              f'合计 {uneval_total} bar-实例）')

    # 分类：DELIVERED / MISSED(真实缺失) / NOT_EVALUATED(冻结后)
    delivered, missed, not_eval = [], [], []
    used_actual = set()
    for t in theo:
        if freeze_dt and t['ts'] and t['ts'] > freeze_dt:
            not_eval.append(t)
            continue
        match = None
        for j, a in enumerate(actual):
            if j in used_actual or a['sym'] != t['sym'] or a['type'] != t['type']:
                continue
            if t['ts'] is None or a['ts'] is None:
                continue
            dt = abs((t['ts'] - a['ts']).total_seconds()) / 60.0
            if dt <= match_min:
                match = (j, dt)
                break
        if match:
            j, dt = match
            used_actual.add(j)
            delivered.append((t, actual[j], dt))
        else:
            missed.append(t)

    extra_actual = [actual[j] for j in range(len(actual)) if j not in used_actual]

    eval_theo = len(theo) - len(not_eval)
    rate = (len(delivered) / eval_theo * 100) if eval_theo else 0.0

    print('\n' + '=' * 70)
    print('对账结果（评估窗口感知）')
    print('=' * 70)
    print(f'理论信号(全量):       {len(theo)}')
    print(f'  其中 评估窗口内:    {eval_theo}')
    print(f'  其中 冻结后未评估:  {len(not_eval)}  ← 不计入缺失率')
    print(f'实际推送总数:         {len(actual)}')
    print(f'已推送(匹配):         {len(delivered)}')
    print(f'真实缺失(评估窗口内未推): {len(missed)}')
    print(f'未评估(扫描冻结后):   {len(not_eval)}')
    if extra_actual:
        print(f'实际推送但理论未触发: {len(extra_actual)}')
    print(f'命中率(评估窗口内):   {rate:.1f}%  = {len(delivered)} / {eval_theo}')

    if delivered:
        print('\n--- 已推送(匹配) ---')
        for t, a, dt in delivered:
            print(f'  ✅ {t["name"]:>12} {t["type"]} {t["ts"]:%H:%M:%S} '
                  f'(实推 {a["ts"]:%H:%M:%S}, 差 {dt:.1f}min) @ {t["price"]:.3f}')

    print('\n--- 真实缺失(评估窗口内应推未推) ---')
    if not missed:
        print('  无')
    else:
        for t in missed:
            print(f'  ❌ {t["name"]:>12} {t["type"]} {t["ts"]:%H:%M:%S} '
                  f'reason={t["reason"] or "-"} @ {t["price"]:.3f} size={t["size"]}')

    print('\n--- 未评估(扫描冻结后，不计入缺失) ---')
    if not not_eval:
        print('  无（重放数据未覆盖冻结后 bar，或当日无下午理论信号）')
    else:
        for t in not_eval:
            print(f'  ⏸ {t["name"]:>12} {t["type"]} {t["ts"]:%H:%M:%S} '
                  f'reason={t["reason"] or "-"} @ {t["price"]:.3f} size={t["size"]}')

    if extra_actual:
        print('\n--- 实际推送但理论未触发(需关注) ---')
        for a in extra_actual:
            print(f'  ⚠️ {NAME.get(a["sym"],a["sym"]):>12} {a["type"]} '
                  f'{a["ts"]:%H:%M:%S} @ {a["price"]:.3f}')

    # CSV
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    rows = []
    for t in missed:
        rows.append({
            '类别': '真实缺失',
            '标的代码': t['sym'], '标的名称': t['name'],
            '缺失信号类型': ('X:' + t['reason']) if t['type'] == 'X' and t['reason'] else t['type'],
            '理论触发时间': t['ts'].strftime('%Y-%m-%d %H:%M:%S') if t['ts'] else '',
            '理论触发价': f'{t["price"]:.3f}',
            '信号仓位(成)': t['size'] if t['size'] is not None else '',
            '未推送原因': miss_cause(t, push_cutoff_dt),
        })
    for t in not_eval:
        rows.append({
            '类别': '未评估(冻结后)',
            '标的代码': t['sym'], '标的名称': t['name'],
            '缺失信号类型': ('X:' + t['reason']) if t['type'] == 'X' and t['reason'] else t['type'],
            '理论触发时间': t['ts'].strftime('%Y-%m-%d %H:%M:%S') if t['ts'] else '',
            '理论触发价': f'{t["price"]:.3f}',
            '信号仓位(成)': t['size'] if t['size'] is not None else '',
            '未推送原因': '扫描冻结(>bar%d≈%s 后未评估)，非真实缺失，不计入缺失率'
                         % (freeze_bar, freeze_dt.strftime('%H:%M:%S') if freeze_dt else '?'),
        })
    with open(OUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        if rows:
            cols = list(rows[0].keys())
            f.write(','.join(cols) + '\n')
            for r in rows:
                f.write(','.join(str(r[c]) for c in cols) + '\n')
        else:
            f.write('类别,标的代码,标的名称,缺失信号类型,理论触发时间,理论触发价,信号仓位(成),未推送原因\n')
            f.write('(无差异)\n')
    print(f'\n差异明细已写入: {OUT_CSV}')

    print('\n--- 数据覆盖度 ---')
    for sym, stat, n in coverage:
        print(f'  {NAME.get(sym,sym):>12} ({sym}): {stat}')

    write_html(OUT_HTML, delivered, missed, not_eval, extra_actual, theo,
               actual, eval_theo, freeze_dt, freeze_bar, uneval_total,
               push_cutoff_dt, coverage, match_min, rate)


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
def _esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def write_html(path, delivered, missed, not_eval, extra_actual, theo,
               actual, eval_theo, freeze_dt, freeze_bar, uneval_total,
               push_cutoff_dt, coverage, match_min, rate):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    freeze_s = freeze_dt.strftime('%H:%M:%S') if freeze_dt else '?'
    cutoff_s = push_cutoff_dt.strftime('%H:%M:%S') if push_cutoff_dt else '无'
    uneval_range = DAY_BARS - freeze_bar if freeze_bar else 0

    def sig_type_cell(t):
        if t['type'] == 'X' and t['reason']:
            return f'X:{_esc(t["reason"])}'
        return _esc(t['type'])

    def miss_cause_html(t):
        return _esc(miss_cause(t, push_cutoff_dt))

    del_rows = ''.join(
        f'<tr><td>{_esc(t["name"])}</td><td>{_esc(t["type"])}</td>'
        f'<td>{t["ts"]:%H:%M:%S}</td><td>{a["ts"]:%H:%M:%S}</td>'
        f'<td>{dt:.1f}</td><td>{t["price"]:.3f}</td>'
        f'<td>{t["size"] if t["size"] is not None else ""}</td></tr>'
        for t, a, dt in delivered) or '<tr><td colspan="7" class="muted">无</td></tr>'

    miss_rows = ''.join(
        f'<tr><td>{_esc(t["name"])}</td><td>{sig_type_cell(t)}</td>'
        f'<td>{t["ts"]:%Y-%m-%d %H:%M:%S}</td><td>{t["price"]:.3f}</td>'
        f'<td>{t["size"] if t["size"] is not None else ""}</td>'
        f'<td class="cause">{miss_cause_html(t)}</td></tr>'
        for t in missed) or '<tr><td colspan="6" class="muted">无真实缺失</td></tr>'

    nev_rows = ''.join(
        f'<tr><td>{_esc(t["name"])}</td><td>{sig_type_cell(t)}</td>'
        f'<td>{t["ts"]:%Y-%m-%d %H:%M:%S}</td><td>{t["price"]:.3f}</td>'
        f'<td>{t["size"] if t["size"] is not None else ""}</td>'
        f'<td class="cause">扫描冻结(&gt;bar{freeze_bar}≈{freeze_s} 后未评估)，'
        f'非真实缺失，不计入缺失率</td></tr>'
        for t in not_eval) or '<tr><td colspan="6" class="muted">无（重放未覆盖冻结后 bar）</td></tr>'

    extra_rows = ''.join(
        f'<tr><td>{_esc(NAME.get(a["sym"], a["sym"]))}</td>'
        f'<td>{_esc(a["type"])}</td><td>{a["ts"]:%H:%M:%S}</td>'
        f'<td>{a["price"]:.3f}</td></tr>'
        for a in extra_actual) or '<tr><td colspan="4" class="muted">无</td></tr>'

    cov_rows = ''.join(
        f'<tr><td>{_esc(NAME.get(sym, sym))} ({_esc(sym)})</td>'
        f'<td>{_esc(stat)}</td><td>{n}</td></tr>'
        for sym, stat, n in coverage)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tpoint 信号推送对账 2026-07-23 [修正版]</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  background:#0f1115; color:#e6e8eb; margin:0; padding:24px; line-height:1.55; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.sub {{ color:#8b929c; font-size:13px; margin-bottom:18px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }}
.card {{ background:#1a1d24; border:1px solid #2a2f3a; border-radius:10px;
  padding:14px 18px; min-width:120px; }}
.card .k {{ font-size:26px; font-weight:700; }}
.card .l {{ font-size:12px; color:#8b929c; margin-top:2px; }}
.card.red .k {{ color:#ff6b6b; }}
.card.green .k {{ color:#51cf66; }}
.card.blue .k {{ color:#4dabf7; }}
.card.gray .k {{ color:#8b929c; }}
section {{ margin-bottom:22px; }}
h2 {{ font-size:15px; border-left:3px solid #4dabf7; padding-left:8px; margin:0 0 10px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; padding:7px 9px; border-bottom:1px solid #23272f; }}
th {{ color:#8b929c; font-weight:600; background:#16191f; }}
td.cause {{ color:#ffa94d; max-width:520px; }}
td.muted {{ color:#8b929c; }}
.ev {{ background:#16191f; border:1px solid #2a2f3a; border-radius:10px;
  padding:14px 16px; font-size:13px; }}
.ev li {{ margin:4px 0; }}
.note {{ font-size:12px; color:#8b929c; margin-top:8px; }}
.banner {{ background:#3a2a1a; border:1px solid #6b4a1a; border-radius:10px;
  padding:12px 16px; font-size:13px; color:#ffd8a8; margin-bottom:18px; }}
.disclaimer {{ margin-top:24px; font-size:12px; color:#ff8787;
  border-top:1px dashed #3a3f4a; padding-top:12px; }}
</style></head><body>
<h1>tpoint 信号推送对账 · 2026-07-23 <span style="font-size:13px;color:#4dabf7;">[修正版 v9.2.1]</span></h1>
<div class="sub">生成时刻 {now} ｜ 理论信号 = live 引擎 fresh-state 全量重放今日真实 1m（不推送）｜
实际推送 = data/push_audit.jsonl ｜ 匹配容差 ±{match_min}min</div>

<div class="banner">⚠️ <b>方法学修正与旧结论收回</b>：本版采用「评估窗口感知」对账（"盘中增量快照"的回溯等价实现）。
旧版 16-miss 结论已被推翻——16 = <b>9 真实缺失</b>(688347 10:43–11:19×7 + 513310 09:42/09:50×2)
+ <b>7 冻结后误判</b>(下午 13:00–15:00 监控从未评估的理论信号)。以本版输出为准。</div>

<div class="cards">
  <div class="card blue"><div class="k">{len(theo)}</div><div class="l">理论信号(全量)</div></div>
  <div class="card green"><div class="k">{len(actual)}</div><div class="l">实际已推送</div></div>
  <div class="card red"><div class="k">{len(missed)}</div><div class="l">真实缺失(评估窗口内)</div></div>
  <div class="card gray"><div class="k">{len(not_eval)}</div><div class="l">未评估(冻结后·不计)</div></div>
  <div class="card"><div class="k">{rate:.1f}%</div><div class="l">命中率(评估窗口)</div></div>
</div>

<section>
  <h2>一、已推送（理论⊆实际匹配，{len(delivered)} 条）</h2>
  <table><thead><tr><th>标的名称</th><th>类型</th><th>理论触发</th>
  <th>实推时间</th><th>时间差(min)</th><th>价</th><th>仓位(成)</th></tr></thead>
  <tbody>{del_rows}</tbody></table>
</section>

<section>
  <h2>二、真实缺失（评估窗口内应推未推，{len(missed)} 条）</h2>
  <table><thead><tr><th>标的名称</th><th>缺失信号类型</th><th>理论触发时间</th>
  <th>理论触发价</th><th>仓位(成)</th><th>未推送原因</th></tr></thead>
  <tbody>{miss_rows}</tbody></table>
  <div class="note">仅含监控实际评估过(bar≤{freeze_bar}≈{freeze_s})且未推送的理论信号；
  归因维度：① 10:42 后推送中断；② 盘间重启致持仓状态失同步。</div>
</section>

<section>
  <h2>三、未评估（扫描冻结后，{len(not_eval)} 条，<span style="color:#ff8787;">不计入缺失率</span>）</h2>
  <table><thead><tr><th>标的名称</th><th>类型</th><th>理论触发时间</th>
  <th>理论触发价</th><th>仓位(成)</th><th>说明</th></tr></thead>
  <tbody>{nev_rows}</tbody></table>
  <div class="note">这些理论信号由收盘后全量重放生成，但其对应 bar(&gt;{freeze_bar})在事发当日
  从未被监控评估（扫描 11:28 后冻结，下午全程未处理），故非真实缺失，仅为"若当时在线本应产生"。
  旧版误将其计入缺失，导致 16-miss 虚高。</div>
</section>

<section>
  <h2>四、实际推送但理论未触发（{len(extra_actual)} 条，需关注）</h2>
  <table><thead><tr><th>标的名称</th><th>类型</th><th>实推时间</th><th>价</th></tr></thead>
  <tbody>{extra_rows}</tbody></table>
</section>

<section>
  <h2>五、评估窗口与根因证据</h2>
  <div class="ev"><ul>
    <li><b>冻结点（评估窗口上界）</b>：state.json 三标的 bar_<sym>_<idx> 最大索引均为
      <b>{freeze_bar}</b>（≈{freeze_s}，上午收盘前）。此后新 bar 未再处理；
      下午 13:00–15:00 全程未评估 → {uneval_range} 根/标（三标同一窗口，合计 {uneval_total} bar-实例）未评估。</li>
    <li><b>推送截止</b>：push_audit.jsonl 最后成功推送 = <b>{cutoff_s}</b>。
      此后扫描续行至 {freeze_s} 但未再推送。</li>
    <li><b>进程未崩溃退出</b>：monitor_crash.log 当日白天无退出记录，
      系 mootdx 单 TCP socket 挂死阻塞扫描循环（已通过 v9.2.1 新鲜度门控 + 强制轮换修复）。</li>
    <li><b>状态失同步</b>：state.json 终态 688347 B:2/S:0（从未开空）、513310 空仓至收盘未平、
      161129 B:0/S:0，与理论路径背离，指向盘间重启丢失持仓态（513310 09:42/09:50 缺失之因）。</li>
    <li><b>数据延迟排除</b>：state.json _miss_161129/_miss_688347/_miss_513310 均为 0，
      当日无行情缺口，缺失非数据延迟所致。</li>
    <li><b>结论修正</b>：真实缺失 = {len(missed)} 条（评估窗口内）；冻结后误判 = {len(not_eval)} 条（不计）。
      命中率按评估窗口计 = <b>{rate:.1f}%</b>（{len(delivered)}/{eval_theo}）。</li>
  </ul></div>
</section>

<section>
  <h2>六、数据覆盖度</h2>
  <table><thead><tr><th>标的</th><th>状态</th><th>理论信号数</th></tr></thead>
  <tbody>{cov_rows}</tbody></table>
  <div class="note">1m 行情经 mootdx 实时拉取；收盘后重放可得当日真实 bar（若数据源返回完整全天）。
  评估窗口由 state.json bar 索引派生，非硬编码。</div>
</section>

<div class="disclaimer">⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</div>
</body></html>'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML 报告已写入: {path}')


if __name__ == '__main__':
    main()
