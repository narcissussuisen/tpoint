#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_review_daily.py —— tpoint 实盘信号复盘（以 push_audit.jsonl 为权威源）

设计原则（与 tpoint 第一性原理一致）：
  * 复盘忠实：信号清单以生产真实推送账本 data/push_audit.jsonl 为准，
    不复算、不幻影。复算（floor 引擎）仅用于「还原触发条件 tag」与
    「用真实 1m 向前验证有效/失效」，不代表实盘实推。
  * 实盘数据：行情来自 MootdxDataSource（mootdx 通达信 TCP 7709，回退腾讯分时），
    与 monitor 同源；计数权威源为 data/state.json 的 _b_count/_s_count。

信号类型：
  B = 买入（开多/回补）  S = 卖出（开空/平多）  X = 出场（移动止损/止损）
有效判定（forward max-fav，与生产复算一致）：
  B：触发后有利波动(最高价相对触发价) > VALID_THR(%) 判有效
  S：触发后有利波动(最低价相对触发价) > VALID_THR(%) 判有效
  X：出场属风控动作，不判有效/失效，改报「已实现/浮盈亏」

用法：
  python signal_review_daily.py                  # 复盘今日（收盘后 15:30 定时任务调用）
  python signal_review_daily.py --cutoff 14:30   # 仅统计截至 14:30 的信号（盘中快照）
  python signal_review_daily.py --date 2026-07-22
  python signal_review_daily.py --push           # 生成报告后推送飞书复盘群
  python signal_review_daily.py --baseline-days 5
"""
import os, sys, json, time, argparse, io, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)

# 强制 floor 门控，必须同生产 run_monitor.bat (MACD_GATE_MODE=floor)
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np
import pandas as pd

# 复用生产级复算引擎（与 monitor 同源，保证「触发条件 tag / 验证」口径一致）
from daily_signal_review import (
    MootdxDataSource, compute_miji_indicators, M,
    fetch_1m, build_data, get_pc, replay_symbol,
    load_push_audit, load_live_counts, prev_trading_days,
    push_feishu_text, CST, HOLIDAYS_2026, VALID_THR, POS_PCT,
)

# 飞书复盘群 webhook（独立于实盘信号群 1d241455，避免复盘推送争抢频限）
FEISHU_REVIEW_HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622'

# 净有效上限(%)：最大不利回撤超过此值，即便曾出现有利波动也判“有效但脆弱/假突破”
ADVERSE_BOUND = 3.0


def _parse_ts(s):
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except Exception:
        try:
            return datetime.datetime.strptime(s[:16], '%Y-%m-%d %H:%M')
        except Exception:
            return None


def _net_valid(valid, adverse):
    """净有效：曾出现有利波动(valid) 且最大不利回撤未超界；否则 False/None。"""
    if valid is True:
        if adverse is None or adverse <= ADVERSE_BOUND:
            return True
        return False  # 有效但脆弱/假突破
    if valid is False:
        return False
    return None  # X 出场不判


def fwd_check(df, ts_str, op, price, cutoff_dt):
    """直接用真实 1m 在该笔推送「之后」的 bar 向前验证（与复算同口径，且只用信号 bar 之后）。
    返回 (valid, max_fav, adverse, reason)。
    注：实盘推送价 = 信号触发时使用的「上一根已收盘 bar」收盘价；验证只看其后的 bar。
        fetch_1m 的 trade_time 为日期级 Timestamp，需按 hour/minute 列定位信号 bar。"""
    if df is None or len(df) < 2:
        return None, None, None, ''
    c = df['close'].values.astype(float)
    hh = df['hour'].values if 'hour' in df.columns else None
    mm = df['minute'].values if 'minute' in df.columns else None
    tgt = _parse_ts(ts_str)
    if tgt is None or hh is None or mm is None:
        return None, None, None, ''
    tgt_h, tgt_m = tgt.hour, tgt.minute
    idx = -1
    for k in range(len(df)):
        if int(hh[k]) == tgt_h and int(mm[k]) == tgt_m:
            idx = k; break
    if idx < 0 or idx >= len(c) - 1:
        return None, None, None, ''
    # 仅用「信号 bar 之后」的 bar（idx+1 起）
    fwd = c[idx + 1:]
    if cutoff_dt is not None:
        # 仅保留 <= cutoff 的后续 bar
        fwd_tt = pd.to_datetime(df['trade_time'].values)[idx + 1:]
        mask = np.array([(t - pd.Timestamp(tgt)).total_seconds() <= (cutoff_dt - pd.Timestamp(tgt)).total_seconds()
                         for t in fwd_tt])
        if mask.any():
            fwd = fwd[mask]
    if len(fwd) == 0:
        return None, None, None, ''
    if op == 'B':
        max_fav = (fwd.max() - price) / price * 100.0      # 后续最高相对触发价的有利幅度
        adverse = (price - fwd.min()) / price * 100.0      # 后续最低相对触发价的不利回撤(正=下跌)
        valid = max_fav > VALID_THR
        reason = '' if valid else f'买入后最大回撤 {adverse:.2f}%（最低 {fwd.min():.2f}），未出现+{VALID_THR}%以上有利波动'
        return bool(valid), round(float(max_fav), 3), round(float(adverse), 3), reason
    elif op == 'S':
        max_fav = (price - fwd.min()) / price * 100.0      # 后续最低相对触发价的有利幅度(卖空看跌)
        adverse = (fwd.max() - price) / price * 100.0      # 后续最高反弹(正=上涨，对卖空不利)
        valid = max_fav > VALID_THR
        reason = '' if valid else f'卖出后最高反弹 {adverse:.2f}%（最高 {fwd.max():.2f}），未出现+{VALID_THR}%以上有利波动'
        return bool(valid), round(float(max_fav), 3), round(float(adverse), 3), reason
    return None, None, None, ''


def enrich_real_signals(audit_rows, replay_by_sym, df_by_sym, cutoff_dt):
    """把实盘推送事件与 floor 引擎复算（真实 1m）对齐，补全条件/有效-失效/盈亏。

    关键点（复盘忠实）：实盘推送带冷却/持仓门控，空状态复算会产生“幻影信号”，
    无法 1:1 还原实盘每笔 B/S 的触发条件。因此：
      * 有效/失效 = 用真实 1m 在该笔推送的「价格+时间」直接向前验证（与复算同口径，
        但不依赖复算是否复现该笔）——这是忠于实盘的最可靠判定。
      * 触发条件 tag = 优先取同分钟复算信号；取不到则标记为“实盘触发”（不臆造条件）。
      * 出场(X) = 风控动作，改报配对盈亏，不判有效/失效。
    """
    positions = {}  # sym -> {'side','entry','entry_ts'} 或 None（已平仓）
    out = []
    for a in sorted(audit_rows, key=lambda x: x['ts']):
        sym = a['sym']; op = a['type']; price = float(a['price']); ts = a['ts']
        cond = None; valid = None; max_fav = None; adverse = None; reason = ''
        if op in ('B', 'S'):
            # 优先用同分钟复算 tag 还原触发条件（仅描述性）；有效-失效一律以真实 1m 直验
            tgt_ts2 = pd.Timestamp(_parse_ts(ts)) if _parse_ts(ts) else None
            for r in replay_by_sym.get(sym, []):
                if r['type'] != op:
                    continue
                try:
                    if tgt_ts2 is not None and pd.to_datetime(r['time']).floor('min') == tgt_ts2.floor('min'):
                        cond = (r['tag'] or '').strip('[]') or r.get('band') or ''
                        break
                except Exception:
                    continue
            valid, max_fav, adverse, reason = fwd_check(df_by_sym.get(sym), ts, op, price, cutoff_dt)
            if cond is None:
                cond = '（实盘触发；条件文本未落库于 push_audit，仅存 ts/标的/类型/价格）'
        # 配对计算盈亏（仅对 B/S/X 做仓位配对）
        pnl = None; pnl_note = ''
        if op == 'B':
            positions[sym] = {'side': 'L', 'entry': price, 'entry_ts': ts}
        elif op == 'S':
            positions[sym] = {'side': 'S', 'entry': price, 'entry_ts': ts}
        elif op == 'X':
            pos = positions.get(sym)
            if pos:
                if pos['side'] == 'L':
                    pnl = (price - pos['entry']) / pos['entry'] * 100.0
                else:
                    pnl = (pos['entry'] - price) / pos['entry'] * 100.0
                pnl_note = f"平{pos['side']} @{pos['entry']:.3f}→{price:.3f}"
                positions[sym] = None
            cond = cond or '出场(移动止损/止损)'
        out.append({
            'ts': ts, 'sym': sym, 'type': op,
            'type_cn': {'B': '买入', 'S': '卖出', 'X': '出场'}.get(op, op),
            'price': round(price, 3), 'condition': cond or '—',
            'valid': valid, 'net_valid': _net_valid(valid, adverse),
            'max_fav_pct': max_fav, 'adverse_pct': adverse,
            'reason': reason, 'pnl_pct': round(pnl, 3) if pnl is not None else None,
            'pnl_note': pnl_note,
            'feishu_code': a.get('feishu_code'), 'ok': a.get('ok'),
        })
    # 截至 cutoff 仍持仓 → 浮盈亏（用真实 1m 最后价）
    mtm = {}
    for sym, pos in positions.items():
        if pos and df_by_sym.get(sym) is not None:
            last = float(df_by_sym[sym]['close'].iloc[-1])
            if pos['side'] == 'L':
                p = (last - pos['entry']) / pos['entry'] * 100.0
            else:
                p = (pos['entry'] - last) / pos['entry'] * 100.0
            mtm[sym] = {'side': pos['side'], 'entry': round(pos['entry'], 3),
                        'last': round(last, 3), 'pnl_pct': round(p, 3)}
    return out, mtm


def build_html(target, enriched, mtm, by_sym, baseline, comparison, live_today):
    def esc(x):
        return (str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    # 按标的归类
    sym_order = sorted(by_sym.keys())
    sections = ''
    for sym in sym_order:
        name = by_sym[sym]['name']
        rows = [r for r in enriched if r['sym'] == sym]
        if not rows:
            continue
        body = ''
        for r in rows:
            nv = r.get('net_valid')
            if nv is True:
                vtag = '<span style="color:#0a8f3c;font-weight:700">有效</span>'
            elif nv is False and r['valid'] is True:
                vtag = '<span style="color:#d4380d;font-weight:700">有效(脆弱)</span>'
            elif r['valid'] is False:
                vtag = '<span style="color:#d4380d;font-weight:700">失效</span>'
            else:
                vtag = '<span style="color:#888">—</span>'
            col = {'买入': '#0a8f3c', '卖出': '#d4380d', '出场': '#1677ff'}.get(r['type_cn'], '#333')
            pnl = '' if r['pnl_pct'] is None else f"{r['pnl_pct']:+.2f}%"
            reason = esc(r['reason']) or '—'
            mf = '' if r['max_fav_pct'] is None else f"{r['max_fav_pct']:+.2f}%"
            adv = '' if r['adverse_pct'] is None else f"{r['adverse_pct']:+.2f}%"
            body += (f"<tr><td>{esc(r['ts'][11:16])}</td>"
                     f"<td style='color:{col};font-weight:700'>{r['type_cn']}</td>"
                     f"<td>{r['price']:.3f}</td>"
                     f"<td>{esc(r['condition'])}</td>"
                     f"<td>{vtag}</td>"
                     f"<td>{mf}</td>"
                     f"<td>{adv}</td>"
                     f"<td>{pnl}</td><td style='color:#666'>{reason}</td></tr>")
        # 标的汇总（净有效口径）
        nb = sum(1 for r in rows if r['type'] == 'B')
        ns = sum(1 for r in rows if r['type'] == 'S')
        nx = sum(1 for r in rows if r['type'] == 'X')
        vb = sum(1 for r in rows if r['type'] == 'B' and r['net_valid'] is True)
        ib = sum(1 for r in rows if r['type'] == 'B' and r['net_valid'] is False)
        vs = sum(1 for r in rows if r['type'] == 'S' and r['net_valid'] is True)
        iss_ = sum(1 for r in rows if r['type'] == 'S' and r['net_valid'] is False)
        tot = vb + ib + vs + iss_
        wr = round((vb + vs) / tot * 100, 1) if tot else None
        mtm_s = mtm.get(sym)
        mtm_html = ''
        if mtm_s:
            mcol = '#0a8f3c' if mtm_s['pnl_pct'] >= 0 else '#d4380d'
            mtm_html = (f"<span style='color:{mcol};font-weight:700'>{mtm_s['pnl_pct']:+.2f}%</span> "
                        f"(浮盈亏，入场 {mtm_s['entry']:.3f} / 最新 {mtm_s['last']:.3f}，"
                        f"{'多' if mtm_s['side']=='L' else '空'})")
        sections += f"""
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 4px;color:#1f2a44">{esc(sym)} {esc(name)}</h2>
  <p style="color:#888;font-size:12px;margin:0 0 10px">
    实盘推送 {len(rows)} 次（买 {nb} / 卖 {ns} / 出场 {nx}）；
    净有效买 {vb} / 失效买 {ib}；净有效卖 {vs} / 失效卖 {iss_}；
    净胜率 <b>{wr if wr is not None else '—'}%</b>
    {'；仍持仓：' + mtm_html if mtm_html else ''}
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">时间</th><th>类型</th><th>价格</th><th>触发条件</th>
     <th>有效/失效</th><th>最大有利</th><th>最大不利</th><th>盈亏</th><th>说明</th></tr></thead>
   <tbody>{body}</tbody></table>
</div>"""
    # 总览 KPI
    nB = sum(1 for r in enriched if r['type'] == 'B')
    nS = sum(1 for r in enriched if r['type'] == 'S')
    nX = sum(1 for r in enriched if r['type'] == 'X')
    # 净有效口径（剔除“有效但脆弱/假突破”）：更贴近“信号是否真的起作用”
    vB = sum(1 for r in enriched if r['type'] == 'B' and r['net_valid'] is True)
    iB = sum(1 for r in enriched if r['type'] == 'B' and r['net_valid'] is False)
    vS = sum(1 for r in enriched if r['type'] == 'S' and r['net_valid'] is True)
    iS = sum(1 for r in enriched if r['type'] == 'S' and r['net_valid'] is False)
    frag = sum(1 for r in enriched if r['valid'] is True and r['net_valid'] is False)
    tot_e = vB + iB + vS + iS
    wr_e = round((vB + vS) / tot_e * 100, 1) if tot_e else None
    # 对比表
    cmp_rows = ''
    for m, cur, base, f in comparison:
        dcol = '#d4380d' if f else '#0a8f3c'
        flag = '⚠️ 异常' if f else '正常'
        cmp_rows += (f"<tr><td>{esc(m)}</td><td><b>{cur}</b></td><td>{base}</td>"
                     f"<td style='color:{dcol};font-weight:700'>{flag}</td></tr>")
    base_days = ', '.join(baseline['days']) or '无'
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 实盘信号复盘 {target}</title></head>
<body style="margin:0;background:#f5f6f8;font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2a44">
<div style="max-width:1000px;margin:0 auto;padding:24px">
  <div style="background:linear-gradient(135deg,#1f2a44,#33507a);color:#fff;border-radius:12px;padding:22px 26px">
    <h1 style="margin:0 0 6px;font-size:22px">tpoint 实盘信号复盘 · {target}</h1>
    <p style="margin:0;opacity:.85;font-size:13px">生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} ｜ 实盘权威源 push_audit.jsonl + state.json ｜ 验证行情 mootdx 真实 1m</p>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:16px">
    <div style="flex:1;min-width:140px;background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)"><div style="font-size:12px;color:#888">实盘信号总数</div><div style="font-size:24px;font-weight:700">{len(enriched)}</div><div style="font-size:12px;color:#666">买 {nB} / 卖 {nS} / 出场 {nX}</div></div>
    <div style="flex:1;min-width:140px;background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)"><div style="font-size:12px;color:#888">净胜率(B/S)</div><div style="font-size:24px;font-weight:700">{wr_e if wr_e is not None else '—'}%</div><div style="font-size:12px;color:#666">净有效 {vB+vS} / 失效 {iB+iS}（含脆弱 {frag}）</div></div>
    <div style="flex:1;min-width:140px;background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)"><div style="font-size:12px;color:#888">近{baseline['n']}日实盘均值</div><div style="font-size:24px;font-weight:700">{baseline['avg_total']:.1f}</div><div style="font-size:12px;color:#666">信号/日（{base_days}）</div></div>
    <div style="flex:1;min-width:140px;background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)"><div style="font-size:12px;color:#888">今日 vs 基线</div><div style="font-size:24px;font-weight:700">{len(enriched)-baseline['avg_total']:+.1f}</div><div style="font-size:12px;color:#666">信号数偏差</div></div>
  </div>
  <div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
    <h2 style="font-size:16px;margin:0 0 10px;color:#1f2a44">四、今日整体 vs 近{baseline['n']}交易日实盘基线</h2>
    <p style="color:#888;font-size:12px;margin:0 0 10px">基线=实盘权威计数(state.json)在可用prior交易日的平均值；今日实盘计数来自 state.json（与 push_audit 一致）。win-rate 为今日真实信号经真实 1m 验证结果。</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
     <thead><tr style="background:#f0f2f5;color:#555;text-align:left"><th style="padding:8px">指标</th><th>今日</th><th>基线均值</th><th>判定</th></tr></thead>
     <tbody>{cmp_rows}</tbody></table>
  </div>
  <h2 style="font-size:18px;margin:22px 0 0;color:#1f2a44">一~三、实盘信号明细（按标的归类）</h2>
  {sections}
  <p style="color:#999;font-size:12px;margin-top:24px;line-height:1.6">
    说明：信号清单以生产真实推送账本 <code>data/push_audit.jsonl</code> 为准；触发条件由 floor 引擎在同口径真实 1m 上复算还原（同生产逻辑，非实盘实推）；有效/失效以真实 1m 向前验证（阈值 {VALID_THR}%）。出场(X)为风控动作，不判有效/失效，改报盈亏。近{baseline['n']}日基线仅含 state.json 中保留的 prior 交易日（{base_days}）。
  </p>
  <p style="color:#999;font-size:12px;margin-top:8px">⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</div></body></html>"""
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='复盘日期 YYYY-MM-DD（默认今日）')
    ap.add_argument('--cutoff', default='15:00', help='信号统计与验证窗截至 HH:MM（默认 15:00=全时段；盘中快照用 14:30）')
    ap.add_argument('--push', action='store_true', help='生成报告后推送飞书复盘群')
    ap.add_argument('--baseline-days', type=int, default=5)
    args = ap.parse_args()

    target = args.date or datetime.datetime.now(CST).strftime('%Y-%m-%d')
    target_dt = datetime.datetime.strptime(target, '%Y-%m-%d').date()
    if target_dt.weekday() >= 5 or target in HOLIDAYS_2026:
        print(f"[skip] {target} 非交易日，退出")
        return None

    cutoff_dt = None
    if args.cutoff:
        try:
            cutoff_dt = datetime.datetime.strptime(f"{target} {args.cutoff}:00", '%Y-%m-%d %H:%M:%S')
        except Exception:
            cutoff_dt = None

    try:
        wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
        SYMS = list(wl.keys()); NAME = wl
    except Exception:
        SYMS = ['161129.SZ', '688347.SH', '513310.SH']
        NAME = {'161129.SZ': '原油LOF易方达', '688347.SH': '华虹公司', '513310.SH': '中韩半导体ETF华泰柏瑞'}

    ds = MootdxDataSource()
    # 1) 实盘推送（权威源），按 cutoff 截断「信号枚举」（cutoff 只限定要统计哪些信号）
    audit_all = load_push_audit(os.path.join(ROOT, 'data', 'push_audit.jsonl'), target)
    audit = [a for a in audit_all if cutoff_dt is None or (_parse_ts(a['ts']) and _parse_ts(a['ts']) <= cutoff_dt)]
    print(f"[audit] {target} 实盘推送共 {len(audit_all)} 笔，cutoff {args.cutoff} 枚举纳入 {len(audit)} 笔")

    # 2) 每个标的：真实 1m（全量，不截断）→ floor 复算（还原触发条件）+ 向前验证。
    #    验证窗用「触发后全部真实 1m」（截至最新 bar），不以 cutoff 截断，避免低估后续走势。
    replay_by_sym = {}; df_by_sym = {}
    for sym in SYMS:
        df = fetch_1m(ds, sym, target)
        if df is None or len(df) < 5:
            print(f"[{sym}] 无1m数据，跳过"); continue
        pc = get_pc(ds, sym, target)
        if pc is None or pc <= 0:
            print(f"[{sym}] PC 缺失，跳过"); continue
        data = build_data(df, pc)
        rows, stats = replay_symbol(sym, NAME.get(sym, sym), data, pc)
        replay_by_sym[sym] = rows
        df_by_sym[sym] = df
        print(f"[{sym}] 复算信号 {len(rows)} 次（条件还原用；验证以真实 1m 直验）")

    # 3) 实盘事件 ↔ 复算对齐，补全条件/有效-失效/盈亏
    enriched, mtm = enrich_real_signals(audit, replay_by_sym, df_by_sym, cutoff_dt)

    # 4) 近 N 交易日实盘基线（state.json 权威计数）
    prior_days = prev_trading_days(target_dt, args.baseline_days)
    state_path = os.path.join(ROOT, 'data', 'state.json')
    base_days = []; per_day_tot = []; per_day_B = []; per_day_S = []
    for d in prior_days:
        lc = load_live_counts(state_path, d)
        if not lc:
            continue
        tot = sum(e['total'] for e in lc.values())
        b = sum(e['B'] for e in lc.values())
        s = sum(e['S'] for e in lc.values())
        base_days.append(d); per_day_tot.append(tot); per_day_B.append(b); per_day_S.append(s)
    avg_total = round(float(np.mean(per_day_tot)), 1) if per_day_tot else 0.0
    avg_B = round(float(np.mean(per_day_B)), 1) if per_day_B else 0.0
    avg_S = round(float(np.mean(per_day_S)), 1) if per_day_S else 0.0

    # 今日实盘计数（state.json）
    live_today = load_live_counts(state_path, target)
    today_tot = sum(e['total'] for e in live_today.values())
    today_B = sum(e['B'] for e in live_today.values())
    today_S = sum(e['S'] for e in live_today.values())

    def flag(cur, base, rel=0.5):
        return abs(cur - base) > max(0.5, abs(base) * rel)

    comparison = [
        ('信号总数/日', today_tot, avg_total, flag(today_tot, avg_total, 0.6)),
        ('买入数/日', today_B, avg_B, flag(today_B, avg_B, 0.6)),
        ('卖出数/日', today_S, avg_S, flag(today_S, avg_S, 0.6)),
    ]
    baseline = {'n': args.baseline_days, 'days': base_days, 'avg_total': avg_total,
                'avg_B': avg_B, 'avg_S': avg_S}

    by_sym = {sym: {'name': NAME.get(sym, sym)} for sym in SYMS}
    out_json = {
        'date': target, 'cutoff': args.cutoff, 'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'push_audit.jsonl (实盘权威) + mootdx 真实1m 验证 + state.json 计数',
        'signals': enriched, 'open_positions_mtm': mtm,
        'today_real_counts': live_today,
        'baseline': baseline,
        'comparison': [{'metric': m, 'today': c, 'baseline': b, 'anomaly': f} for m, c, b, f in comparison],
        'audit_count': len(audit),
    }
    os.makedirs(os.path.join(ROOT, 'output'), exist_ok=True)
    jpath = os.path.join(ROOT, 'output', f'signal_review_{target}.json')
    hpath = os.path.join(ROOT, 'output', f'signal_review_{target}.html')
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump(out_json, f, ensure_ascii=False, indent=2)
    with open(hpath, 'w', encoding='utf-8') as f:
        f.write(build_html(target, enriched, mtm, by_sym, baseline, comparison, live_today))
    print(f"\n[ok] JSON -> {jpath}")
    print(f"[ok] HTML -> {hpath}")

    if args.push:
        nB = sum(1 for r in enriched if r['type'] == 'B')
        nS = sum(1 for r in enriched if r['type'] == 'S')
        nX = sum(1 for r in enriched if r['type'] == 'X')
        vB = sum(1 for r in enriched if r['type'] == 'B' and r['valid'] is True)
        iB = sum(1 for r in enriched if r['type'] == 'B' and r['valid'] is False)
        vS = sum(1 for r in enriched if r['type'] == 'S' and r['valid'] is True)
        iS = sum(1 for r in enriched if r['type'] == 'S' and r['valid'] is False)
        tot = vB + iB + vS + iS
        wr = round((vB + vS) / tot * 100, 1) if tot else None
        txt = (f"【tpoint 实盘信号复盘 {target}（截至 {args.cutoff}）】\n"
               f"实盘推送 {len(enriched)} 笔（买{nB}/卖{nS}/出场{nX}），"
               f"信号胜率 {wr}%（近{args.baseline_days}日实盘均值 {avg_total} 笔/日）。\n"
               f"报告：{hpath}")
        r = push_feishu_text(txt)
        print(f"[push] {r}")
    return out_json


if __name__ == '__main__':
    main()
