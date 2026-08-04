#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_signal_review.py —— tpoint 每日收盘后信号复盘（生产级复算）

功能：
  1. 与生产 monitor.detect_for 完全一致（MACD_GATE_MODE=floor / 动态仓位 / 移动止损），
     用真实 1m 行情复现当日（或指定日期）全部 B/S/EXIT 触发序列。
  2. 逐信号向前验证（forward max-fav）：触发后是否出现有利波动，判定【有效/失效】，
     并对失效信号给出简要原因（反向波动幅度）。
  3. 汇总当日各标的信号表现，并与近 5 个交易日基线对比，标记异常 / 模式变化。
  4. 输出 JSON + 自包含 HTML 复盘报告；--push 时向 tpoint 信号群推送文本摘要。

数据来源（与生产同源）：
  - 当日：MootdxDataSource.intraday（mootdx 通达信 TCP 7709，回退腾讯分时）
  - 历史：MootdxDataSource.historical_1m

用法：
  python daily_signal_review.py                 # 复盘今日（收盘后 15:00 之后运行）
  python daily_signal_review.py --date 2026-07-22
  python daily_signal_review.py --push          # 生成报告后推送飞书信号群

注意：须在 tpoint venv 中运行（venv/Scripts/python.exe）。
"""
import os, sys, json, time, argparse, datetime, io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)

# 强制 floor 门控，必须与生产 run_monitor.bat(MACD_GATE_MODE=floor) 一致
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np
import pandas as pd

from datasource import MootdxDataSource
from miji_alpha import compute_miji_indicators
import monitor as M

# ---- 复刻生产常量（monitor 模块级，保持一致即可）----
COLDOWN_BARS = M.COLDOWN_BARS
MAX_B_DAILY  = M.MAX_B_DAILY
MAX_S_DAILY  = M.MAX_S_DAILY
MAX_SIZE_PCT = M.MAX_SIZE_PCT
POS_PCT      = M.POS_PCT
VALID_THR    = 0.15   # 有效阈值(%)：触发后有利波动需 > 0.15% 才判有效（与早盘复算一致）

# 飞书复盘群 webhook（独立于实盘信号群 1d241455，避免复盘推送与实盘信号争抢频限→11232 丢推）
FEISHU_SIGNAL_HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622'

CST = M.CST

HOLIDAYS_2026 = {
    '2026-01-01','2026-01-02','2026-01-26','2026-01-27','2026-01-28','2026-01-29','2026-01-30',
    '2026-02-02','2026-02-03','2026-04-06','2026-05-01','2026-05-04','2026-05-05',
    '2026-06-19','2026-06-22','2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07',
    '2026-12-25',
}


# ----------------------------------------------------------------------------- #
# 行情获取 / 指标构建
# ----------------------------------------------------------------------------- #
def get_pc(ds, sym, day):
    """取标的在 day 的昨收 PC（前一日收盘）。"""
    try:
        d = ds.klines.get(sym, period='1d', count=60)
        if d is None or len(d) == 0:
            return None
        d = d.sort_values('trade_date').reset_index(drop=True)
        last = str(d['trade_date'].iloc[-1])[:10]
        if last == day:
            return float(d['close'].iloc[-2])
        idx = d.index[d['trade_date'] == day]
        if len(idx):
            i = idx[0]
            return float(d['close'].iloc[i - 1]) if i > 0 else float(d['close'].iloc[0])
        return float(d['close'].iloc[-1])
    except Exception:
        return None


def get_daily_vol_med(ds, sym, day, win=10):
    try:
        d = ds.klines.get(sym, period='1d', count=60)
        if d is None or len(d) == 0:
            return None
        d = d.sort_values('trade_date').reset_index(drop=True)
        vols = d['volume'].clip(lower=0).values.astype(float)
        idx = d.index[d['trade_date'] == day]
        upto = idx[0] if len(idx) else len(d)
        prev = vols[max(0, upto - win):upto]
        if len(prev) == 0:
            return float(vols.mean())
        return float(np.median(prev))
    except Exception:
        return None


def fetch_1m(ds, sym, day):
    """[P1-3 迭代] 复盘 1m 数据获取：口径对齐 mootdx 真实 OHLC。

    当日（复算当日）→ 强制 mootdx 主源（historical_1m 直取当日真实 1m K线），
    不再走 intraday() 的腾讯分时兜底——腾讯分时 open=前收/high=low=极值，
    会与实盘 mootdx 真实数据产生口径偏差（07-31 588000 复算 B@1.783 vs 实盘 1.788）。
    仅当 mootdx 完全失败才降级 intraday（合成数据，报告将标注来源）。

    历史 → historical_1m（本就纯 mootdx，无兜底）。
    """
    if day == datetime.datetime.now(CST).strftime('%Y-%m-%d'):
        # 当日：优先 mootdx 真实 1m（historical_1m offset 拉取后按日过滤）
        try:
            df = ds.historical_1m(sym, day)
            if df is not None and len(df) >= 5:
                df.attrs['data_source'] = 'mootdx'
                return df
        except Exception:
            pass
        # mootdx 失败才降级 intraday（腾讯分时合成数据，标注来源）
        try:
            df = ds.intraday(sym)
            if df is not None and len(df) >= 5:
                df.attrs['data_source'] = 'tencent_synth'
                print(f"  ⚠️ 当日 {sym} mootdx 1m 失败，降级腾讯分时合成数据（口径偏差风险）")
                return df
        except Exception:
            df = None
        return None
    else:
        try:
            df = ds.historical_1m(sym, day)
            if df is not None and len(df) >= 5:
                df.attrs['data_source'] = 'mootdx'
                return df
        except Exception:
            df = None
        return None


def build_data(df, pc):
    c  = df['close'].values.astype(float)
    h  = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o  = df['open'].values.astype(float) if 'open' in df.columns else c.copy()
    has_vol = 'volume' in df.columns
    v  = df['volume'].values.astype(float) if has_vol else None
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data


# ----------------------------------------------------------------------------- #
# 信号复算 + 验证
# ----------------------------------------------------------------------------- #
def replay_symbol(sym, name, data, pc):
    """空 state 复算（与生产单实例单日从零开始等价），返回信号明细 + 日统计。"""
    c = data['c']; n = data['n']; df = data['df']
    trade_times = df['trade_time'].values if df is not None else None

    M.STATE[sym] = {'PC': pc}          # detect_for 读取 STATE[sym]['PC']
    st = {}                            # 干净当日状态（无冷却/持仓残留）
    sigs = M.detect_for(sym, name, data, st)

    rows = []
    for s in sigs:
        op = s[0]; price = float(s[1]); bar_tt = s[12] if len(s) > 12 else ''
        tag = s[9] if len(s) > 9 else ''
        exit_reason = s[10] if len(s) > 10 else ''
        pos_pct = s[13] if len(s) > 13 else POS_PCT
        day_chg = s[11] if len(s) > 11 else None
        chg = s[2] if len(s) > 2 else None
        idx = -1
        if trade_times is not None:
            for k, t in enumerate(trade_times):
                if str(t) == str(bar_tt):
                    idx = k; break
        # 向前验证
        max_fav = None; valid = None; adverse = None; reason = ''
        if idx >= 0 and idx < n - 1:
            fwd = c[idx + 1:]
            if op == 'B':
                best = (fwd.max() - price) / price * 100.0      # 有利(上涨)
                worst = (price - fwd.min()) / price * 100.0      # 不利(下跌)
                max_fav = round(float(best), 3)
                valid = best > VALID_THR
                adverse = round(float(worst), 3)
                if not valid:
                    reason = f'买入后最低回撤 {worst:.2f}%，未出现+{VALID_THR}%以上有利波动，均线引力被反向突破'
            elif op == 'S':
                best = (price - fwd.min()) / price * 100.0       # 有利(下跌)
                worst = (fwd.max() - price) / price * 100.0      # 不利(上涨)
                max_fav = round(float(best), 3)
                valid = best > VALID_THR
                adverse = round(float(worst), 3)
                if not valid:
                    reason = f'卖出后最高反弹 {worst:.2f}%，未出现+{VALID_THR}%以上有利波动，上轨压力被突破'
            else:  # X 出场
                max_fav = None; valid = None
                reason = f'持仓盈亏 {chg:.2f}%（{exit_reason} 出场）'
        rows.append({
            'time': str(bar_tt), 'type': op, 'type_cn': _type_cn(op, exit_reason),
            'price': round(price, 3), 'pos_pct': pos_pct,
            'day_chg': round(float(day_chg), 3) if day_chg is not None else None,
            'tag': tag, 'exit_reason': exit_reason,
            'band': ('触及下轨' if op == 'B' else ('触及上轨' if op == 'S' else '')),
            'max_fav_pct': max_fav, 'valid': (bool(valid) if valid is not None else None),
            'adverse_pct': adverse, 'reason': reason,
        })

    # 日统计
    stats = day_stats(df, data, pc)
    return rows, stats


def _type_cn(op, exit_reason):
    if op == 'B':
        return '买入'
    if op == 'S':
        return '卖出'
    # X 出场
    if exit_reason == 'B':
        return '回补(买)'
    if exit_reason == 'S':
        return '平多(卖)'
    return '止损/出场'


def day_stats(df, data, pc):
    c = data['c']; h = data['h']; lo = data['lo']; vwap = data['vwap']; atr = data['atr']; v = data['v']
    n = data['n']
    open_p = float(c[0]); high_p = float(h.max()); low_p = float(lo.min()); close_p = float(c[-1])
    day_chg = (close_p - pc) / pc * 100 if pc else 0.0
    total_vol = float(np.sum(v)) if v is not None else 0.0
    atr_last = float(atr[-1]); atr_pct = atr_last / close_p * 100 if close_p else 0.0
    range_pct = (high_p - low_p) / close_p * 100 if close_p else 0.0
    dev_abs = np.abs((c - vwap) / vwap * 100)
    return {
        'open': round(open_p, 3), 'high': round(high_p, 3), 'low': round(low_p, 3),
        'close': round(close_p, 3), 'pc': round(pc, 3) if pc else None,
        'day_chg_pct': round(day_chg, 3), 'total_volume': total_vol,
        'atr_last': round(atr_last, 4), 'atr_pct': round(atr_pct, 3),
        'intraday_range_pct': round(range_pct, 3),
        'avg_abs_dev_vwap_pct': round(float(dev_abs.mean()), 3),
        'max_abs_dev_vwap_pct': round(float(dev_abs.max()), 3),
        'n_bars': int(n),
    }


# ----------------------------------------------------------------------------- #
# 汇总 / 基线对比
# ----------------------------------------------------------------------------- #
def symbol_summary(rows, stats):
    b = [r for r in rows if r['type'] == 'B']
    s = [r for r in rows if r['type'] == 'S']
    x = [r for r in rows if r['type'] == 'X']
    valid_B = sum(1 for r in b if r['valid'])
    inv_B   = sum(1 for r in b if r['valid'] is False)
    valid_S = sum(1 for r in s if r['valid'])
    inv_S   = sum(1 for r in s if r['valid'] is False)
    valid_total = valid_B + valid_S
    inv_total = inv_B + inv_S
    win_rate = round(valid_total / (valid_total + inv_total) * 100, 1) if (valid_total + inv_total) else None
    return {
        'n_signals': len(rows), 'n_B': len(b), 'n_S': len(s), 'n_X': len(x),
        'valid_B': valid_B, 'invalid_B': inv_B, 'valid_S': valid_S, 'invalid_S': inv_S,
        'valid_total': valid_total, 'invalid_total': inv_total, 'win_rate': win_rate,
        'stats': stats,
    }


def prev_trading_days(target, n):
    d = target - datetime.timedelta(days=1)
    out = []
    while len(out) < n:
        ds_ = d.strftime('%Y-%m-%d')
        if d.weekday() < 5 and ds_ not in HOLIDAYS_2026:
            out.append(ds_)
        d -= datetime.timedelta(days=1)
    return out


# ----------------------------------------------------------------------------- #
# Fix3 (2026-07-22) 实盘权威源 = state.json 计数；复算仅作对照
# ----------------------------------------------------------------------------- #
def load_live_counts(state_path, date):
    """读 data/state.json 实盘权威计数。key 形如 _b_count_{sym}_{YYYYMMDD} / _s_count_{sym}_{YYYYMMDD}。
    返回 {sym: {'B': n, 'S': n, 'total': n}}。缺失/坏 → 空 dict（不误报）。"""
    d = date.replace('-', '')
    res = {}
    try:
        st = json.load(open(state_path, encoding='utf-8'))
    except Exception:
        return res
    for k, v in st.items():
        for typ, prefix in (('B', '_b_count_'), ('S', '_s_count_')):
            suf = '_' + d
            if k.startswith(prefix) and k.endswith(suf):
                sym = k[len(prefix):-len(suf)]
                e = res.setdefault(sym, {'B': 0, 'S': 0})
                try:
                    e[typ] += int(v)
                except Exception:
                    pass
    for e in res.values():
        e['total'] = e['B'] + e['S']
    return res


def load_push_audit(audit_path, date=None):
    """解析 data/push_audit.jsonl（Fix2 上线后才有）。返回行列表；date 给定则按 ts 前缀(YYYY-MM-DD)过滤。"""
    rows = []
    try:
        with open(audit_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if date and r.get('ts', '').startswith(date):
                    rows.append(r)
                elif not date:
                    rows.append(r)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return rows


# ----------------------------------------------------------------------------- #
# HTML 报告
# ----------------------------------------------------------------------------- #
def build_html(target, sym_results, baseline, comparison, live_counts=None, audit_rows=None, diff_rows=None):
    def esc(x):
        return (str(x).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    # ---- Fix3: 实盘权威源（state.json）vs 复算对照 + Δ ----
    live_counts = live_counts or {}
    all_syms = sorted(set(list(sym_results.keys()) + list(live_counts.keys())))
    cmp_rows_live = ''
    for sym in all_syms:
        live = live_counts.get(sym, {'B': 0, 'S': 0, 'total': 0})
        rc = sym_results.get(sym, {}).get('summary')
        rc_b = rc['n_B'] if rc else 0
        rc_s = rc['n_S'] if rc else 0
        rc_total = rc['n_signals'] if rc else 0
        delta = rc_total - live['total']
        pct = (delta / live['total'] * 100) if live['total'] else None
        pct_s = f"{pct:+.1f}%" if pct is not None else '—'
        dcolor = '#d4380d' if delta != 0 else '#0a8f3c'
        nm = sym_results.get(sym, {}).get('name') or live_counts.get(sym, {}).get('name', '')
        cmp_rows_live += (f"<tr><td>{esc(sym)} {esc(nm)}</td>"
                          f"<td>{live['B']}</td><td>{live['S']}</td><td><b>{live['total']}</b></td>"
                          f"<td>{rc_b}</td><td>{rc_s}</td><td><b>{rc_total}</b></td>"
                          f"<td style='color:{dcolor};font-weight:700'>{delta:+d}</td>"
                          f"<td style='color:{dcolor}'>{pct_s}</td></tr>")
    audit_html = ''
    if audit_rows:
        arows = ''
        for r in audit_rows:
            ok = r.get('ok')
            ok_tag = ('<span style="color:#0a8f3c;font-weight:700">成功</span>' if ok
                      else '<span style="color:#d4380d;font-weight:700">失败</span>')
            arows += (f"<tr><td>{esc(r.get('ts', ''))}</td><td>{esc(r.get('sym', ''))}</td>"
                      f"<td>{esc(r.get('type', ''))}</td><td>{esc(r.get('price', ''))}</td>"
                      f"<td>{esc(r.get('feishu_code', ''))}</td><td>{ok_tag}</td></tr>")
        audit_html = f"""
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 12px;color:#1f2a44">〇·B 推送审计（push_audit.jsonl 逐笔）</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">时间</th><th>标的</th><th>类型</th><th>价格</th><th>飞书code</th><th>结果</th></tr></thead>
   <tbody>{arows}</tbody></table>
</div>"""
    # [P2-2 迭代] 实盘/复算 diff 汇总（自动）
    diff_html = ''
    if diff_rows:
        drows = ''
        for d in diff_rows:
            vcolor = {'一致': '#0a8f3c', '实盘>复算(可能重放/多次同bar)': '#d4380d',
                      '实盘<复算(可能漏推/首扫抑制)': '#fa8c16'}.get(d['verdict'], '#333')
            drows += (f"<tr><td>{esc(d['sym'])} {esc(d['name'])}</td>"
                      f"<td>{d['live_B']}</td><td>{d['live_S']}</td>"
                      f"<td>{d['recalc_B']}</td><td>{d['recalc_S']}</td>"
                      f"<td>{d['delta_B']:+d}</td><td>{d['delta_S']:+d}</td>"
                      f"<td style='color:{vcolor};font-weight:700'>{esc(d['verdict'])}</td></tr>")
        diff_html = f"""
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 4px;color:#1f2a44">〇·C 实盘 vs 复算 diff 汇总（自动）</h2>
  <p style="color:#888;font-size:12px;margin:0 0 10px">Δ = 实盘 − 复算。负值→疑似漏推/首扫抑制（P0-2 已收窄窗口）；正值→疑似重放或同 bar 多次计数。仅汇总计数，逐笔见 push_audit。</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">标的</th><th>实盘买</th><th>实盘卖</th><th>复算买</th><th>复算卖</th><th>Δ买</th><th>Δ卖</th><th>结论</th></tr></thead>
   <tbody>{drows}</tbody></table>
</div>"""

    live_section = f"""
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 4px;color:#1f2a44">〇、实盘权威源（state.json）vs 复算对照</h2>
  <p style="color:#888;font-size:12px;margin:0 0 10px">实盘计数以 <code>data/state.json</code> 为准（生产真实推送账本）；右侧为 floor 引擎从零复算的<b>参考</b>计数，<b>非实盘实推</b>。Δ = 复算 − 实盘。</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">标的</th><th>实盘买</th><th>实盘卖</th><th>实盘合计</th>
     <th>复算买</th><th>复算卖</th><th>复算合计</th><th>Δ</th><th>偏差%</th></tr></thead>
   <tbody>{cmp_rows_live}</tbody></table>
</div>{diff_html}{audit_html}"""

    # 信号明细：按标的分类展示
    def _row_html(r):
        if r['valid'] is True:
            vtag = '<span style="color:#0a8f3c;font-weight:700">有效</span>'
        elif r['valid'] is False:
            vtag = '<span style="color:#d4380d;font-weight:700">失效</span>'
        else:
            vtag = '<span style="color:#888">—</span>'
        col = {'买入': '#0a8f3c', '卖出': '#d4380d', '回补(买)': '#0a8f3c',
               '平多(卖)': '#d4380d', '止损/出场': '#1677ff'}.get(r['type_cn'], '#333')
        cond = esc((r['tag'] or '').strip('[]') or r['band'] or '')
        extra = f"｜当日 {r['day_chg']:+.2f}%" if r['day_chg'] is not None else ''
        note = esc(r['reason']) if r['reason'] else ''
        return (f"<tr><td>{esc(r['time'][11:16])}</td>"
                f"<td style='color:{col};font-weight:700'>{esc(r['type_cn'])}</td>"
                f"<td>{r['pos_pct']}成</td><td>{r['price']:.3f}</td>"
                f"<td>{esc(cond)}{extra}</td><td>{vtag}</td>"
                f"<td>{r['max_fav_pct'] if r['max_fav_pct'] is not None else '—'}</td>"
                f"<td style='color:#666;font-size:12px'>{note}</td></tr>")
    sig_sections = ''
    for sym, res in sym_results.items():
        name = res.get('name', sym)
        s = res.get('summary')
        if not res.get('rows') or not s:
            err = res.get('error') or '无信号'
            sig_sections += (f'<div style="margin:16px 0">'
                             f'<div style="border-left:4px solid #2d3b5e;padding-left:10px">'
                             f'<b style="color:#1f2a44;font-size:15px">{esc(sym)} {esc(name)}</b></div>'
                             f'<p style="color:#999;font-size:12px;margin:6px 0 0 14px">{esc(err)}</p></div>')
            continue
        st = s['stats']
        badge = (f"日 {st['day_chg_pct']:+.2f}% ｜ 振幅 {st['intraday_range_pct']:.2f}% ｜ "
                 f"信号 {s['n_signals']}（买 {s['n_B']} / 卖 {s['n_S']} / 出场 {s['n_X']}）｜ "
                 f"胜率 {s['win_rate'] if s['win_rate'] is not None else '—'}%")
        rows_html = ''.join(_row_html(r) for r in res['rows'])
        sig_sections += f"""
<div style="margin:16px 0">
  <div style="display:flex;align-items:baseline;gap:10px;border-left:4px solid #2d3b5e;padding:4px 0 4px 10px;margin-bottom:8px">
    <b style="color:#1f2a44;font-size:15px">{esc(sym)} {esc(name)}</b>
    <span style="color:#888;font-size:12px">{esc(badge)}</span>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:6px 8px">时间</th><th>方向</th><th>仓位</th><th>价</th>
     <th>触发条件</th><th>验证</th><th>有利%</th><th>说明</th></tr></thead>
   <tbody>{rows_html}</tbody></table>
</div>"""
    # 标的汇总表
    sum_rows = ''
    for sym, res in sym_results.items():
        s = res['summary']; st = s['stats']
        sum_rows += (f"<tr><td>{esc(sym)} {esc(res['name'])}</td>"
                     f"<td>{s['n_B']}</td><td>{s['n_S']}</td><td>{s['n_X']}</td>"
                     f"<td>{s['valid_total']}/{s['invalid_total']}</td>"
                     f"<td><b>{s['win_rate'] if s['win_rate'] is not None else '—'}%</b></td>"
                     f"<td>{st['day_chg_pct']:+.2f}%</td>"
                     f"<td>{st['intraday_range_pct']:.2f}%</td>"
                     f"<td>{st['max_abs_dev_vwap_pct']:.2f}%</td></tr>")
    # 基线对比
    cmp_rows = ''
    for metric, cur, base, flag in comparison:
        color = '#d4380d' if flag else '#333'
        cmp_rows += (f"<tr><td>{esc(metric)}</td><td><b>{esc(cur)}</b></td>"
                     f"<td>{esc(base)}</td>"
                     f"<td style='color:{color};font-weight:700'>{esc('⚠ 异常' if flag else '正常')}</td></tr>")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 信号复盘 {target}</title></head>
<body style="margin:0;background:#f5f6f8;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:1080px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#1f2a44,#2d3b5e);color:#fff;padding:22px 26px;border-radius:12px">
  <h1 style="margin:0 0 6px;font-size:22px">tpoint 做T信号复盘 · {target}</h1>
  <p style="margin:0;opacity:.85;font-size:13px">实盘推送 <b style="color:#ffd479">{sum(l.get('total',0) for l in live_counts.values())}</b> 次（state.json 权威）｜复算参考 {sum(s['n_signals'] for s in [r['summary'] for r in sym_results.values() if r.get('summary')])} 次（非实盘实推，含 warmup 差异）｜
  胜率 {_overall_win(sym_results)}%｜生成于 {datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
{live_section}
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 4px;color:#1f2a44">一、信号明细（按标的分类，<span style="color:#d4380d">复算参考 · 非实盘推送</span>）</h2>
  <p style="color:#888;font-size:12px;margin:0 0 10px">以下为 floor 引擎从零复算的参考信号，含 warmup/状态差异，<b>不代表实盘真实推送</b>；实盘权威计数见 〇 节，实盘推送逐笔见 〇·B 节（需 push_audit.jsonl 落地）</p>
  {sig_sections}
</div>
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 12px;color:#1f2a44">二、各标的汇总</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">标的</th><th>买</th><th>卖</th><th>出场</th>
     <th>有效/失效</th><th>胜率</th><th>日涨跌</th><th>振幅</th><th>最大偏离VWAP</th></tr></thead>
   <tbody>{sum_rows}</tbody></table>
</div>
<div style="background:#fff;border-radius:10px;padding:18px;margin-top:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
  <h2 style="font-size:16px;margin:0 0 12px;color:#1f2a44">三、当日 vs 近5交易日基线</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <thead><tr style="background:#f0f2f5;color:#555;text-align:left">
     <th style="padding:8px">指标</th><th>当日</th><th>5日均值</th><th>判定</th></tr></thead>
   <tbody>{cmp_rows}</tbody></table>
  <p style="color:#888;font-size:12px;margin-top:10px">{esc(baseline['note'])}</p>
</div>
<p style="text-align:center;color:#aaa;font-size:12px;margin-top:18px">
 tpoint v9.2.0 复盘 · 信号为算法参考，非投资建议</p>
</div></body></html>"""
    return html


def _overall_win(sym_results):
    vb = vi = 0
    for r in sym_results.values():
        vb += r['summary']['valid_total']; vi += r['summary']['invalid_total']
    return round(vb / (vb + vi) * 100, 1) if (vb + vi) else 0.0


# ----------------------------------------------------------------------------- #
# 飞书推送（文本摘要，标准库实现，无额外依赖）
# ----------------------------------------------------------------------------- #
def push_feishu_text(text):
    import urllib.request
    try:
        body = json.dumps({'msg_type': 'text', 'content': {'text': text}}).encode('utf-8')
        req = urllib.request.Request(FEISHU_SIGNAL_HOOK, data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        return f'PUSH_FAIL:{e}'


# ----------------------------------------------------------------------------- #
# 主流程
# ----------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='复盘日期 YYYY-MM-DD（默认今日）')
    ap.add_argument('--push', action='store_true', help='生成报告后推送飞书信号群')
    ap.add_argument('--baseline-days', type=int, default=5)
    args = ap.parse_args()

    target = args.date or datetime.datetime.now(CST).strftime('%Y-%m-%d')
    target_dt = datetime.datetime.strptime(target, '%Y-%m-%d').date()

    # 交易日校验（非交易日直接干净退出，便于定时任务在节假日静默跳过）
    if target_dt.weekday() >= 5 or target in HOLIDAYS_2026:
        print(f"[skip] {target} 非交易日，退出（0 信号，不生成报告）")
        return None

    # 标的同时段动态读取 watchlist（与 monitor 一致）
    try:
        wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
        SYMS = list(wl.keys()); NAME = wl
    except Exception:
        SYMS = ['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH']
        NAME = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300058.SZ': '蓝色光标', '600570.SH': '恒生电子', '688111.SH': '金山办公'}

    ds = MootdxDataSource()
    sym_results = {}
    for sym in SYMS:
        name = NAME.get(sym, sym)
        df = fetch_1m(ds, sym, target)
        if df is None:
            sym_results[sym] = {'name': name, 'error': '无1m数据', 'rows': [], 'summary': None}
            print(f"[{sym}] 无1m数据，跳过")
            continue
        pc = get_pc(ds, sym, target)
        if pc is None or pc <= 0:
            sym_results[sym] = {'name': name, 'error': 'PC缺失', 'rows': [], 'summary': None}
            print(f"[{sym}] PC缺失，跳过")
            continue
        data = build_data(df, pc)
        rows, stats = replay_symbol(sym, name, data, pc)
        sym_results[sym] = {'name': name, 'rows': rows, 'summary': symbol_summary(rows, stats), 'stats': stats}
        print(f"[{sym}] 信号 {len(rows)} 次(买{sum(1 for r in rows if r['type']=='B')}/"
              f"卖{sum(1 for r in rows if r['type']=='S')}/出场{sum(1 for r in rows if r['type']=='X')}) "
              f"胜率 {sym_results[sym]['summary']['win_rate']}%")

    # ---- 基线：近 N 个交易日（仅用当日已监控的“老标的”做可比基线；新标的剔除）----
    prior_days = prev_trading_days(target_dt, args.baseline_days)
    # 判定新标的：若某 sym 在全部 prior_days 均无数据 → 视为新标的，不参与基线均值
    baseline_syms = []
    new_syms = []
    for sym in SYMS:
        has_any = False
        for d in prior_days:
            df = fetch_1m(ds, sym, d)
            if df is not None and get_pc(ds, sym, d):
                has_any = True; break
        (baseline_syms if has_any else new_syms).append(sym)

    base_metric = {s: {'n_signals': [], 'n_B': [], 'n_S': [], 'win_rate': [],
                       'range': [], 'maxdev': []} for s in baseline_syms}
    for d in prior_days:
        for sym in baseline_syms:
            df = fetch_1m(ds, sym, d)
            if df is None:
                continue
            pc = get_pc(ds, sym, d)
            if pc is None or pc <= 0:
                continue
            data = build_data(df, pc)
            rows, stats = replay_symbol(sym, NAME.get(sym, sym), data, pc)
            s = symbol_summary(rows, stats)
            base_metric[sym]['n_signals'].append(s['n_signals'])
            base_metric[sym]['n_B'].append(s['n_B'])
            base_metric[sym]['n_S'].append(s['n_S'])
            base_metric[sym]['win_rate'].append(s['win_rate'] if s['win_rate'] is not None else 0)
            base_metric[sym]['range'].append(stats['intraday_range_pct'])
            base_metric[sym]['maxdev'].append(stats['max_abs_dev_vwap_pct'])

    # 基线均值（跨标的跨日合并）
    def mean_list(vals):
        flat = [v for vs in vals for v in vs]
        return round(float(np.mean(flat)), 2) if flat else 0.0
    base_mean = {
        'n_signals': mean_list([m['n_signals'] for m in base_metric.values()]),
        'n_B': mean_list([m['n_B'] for m in base_metric.values()]),
        'n_S': mean_list([m['n_S'] for m in base_metric.values()]),
        'win_rate': mean_list([m['win_rate'] for m in base_metric.values()]),
        'range': mean_list([m['range'] for m in base_metric.values()]),
        'maxdev': mean_list([m['maxdev'] for m in base_metric.values()]),
    }
    # 当日（老标的）合计
    cur = {'n_signals': 0, 'n_B': 0, 'n_S': 0, 'valid': 0, 'invalid': 0,
           'range': [], 'maxdev': []}
    for sym in baseline_syms:
        r = sym_results.get(sym, {})
        if not r.get('summary'):
            continue
        s = r['summary']
        cur['n_signals'] += s['n_signals']; cur['n_B'] += s['n_B']; cur['n_S'] += s['n_S']
        cur['valid'] += s['valid_total']; cur['invalid'] += s['invalid_total']
        cur['range'].append(s['stats']['intraday_range_pct'])
        cur['maxdev'].append(s['stats']['max_abs_dev_vwap_pct'])
    cur_win = round(cur['valid'] / (cur['valid'] + cur['invalid']) * 100, 1) if (cur['valid'] + cur['invalid']) else 0.0
    cur_range = round(float(np.mean(cur['range'])), 2) if cur['range'] else 0.0
    cur_maxdev = round(float(np.mean(cur['maxdev'])), 2) if cur['maxdev'] else 0.0

    def flag_abn(cur_v, base_v, rel=0.5):
        return abs(cur_v - base_v) > max(0.5, abs(base_v) * rel)

    comparison = [
        ('日均信号数', cur['n_signals'], base_mean['n_signals'],
         flag_abn(cur['n_signals'], base_mean['n_signals'], 0.6)),
        ('日均买入数', cur['n_B'], base_mean['n_B'], flag_abn(cur['n_B'], base_mean['n_B'], 0.6)),
        ('日均卖出数', cur['n_S'], base_mean['n_S'], flag_abn(cur['n_S'], base_mean['n_S'], 0.6)),
        ('信号胜率(%)', cur_win, base_mean['win_rate'],
         flag_abn(cur_win, base_mean['win_rate'], 0.3)),
        ('平均日内振幅(%)', cur_range, base_mean['range'], flag_abn(cur_range, base_mean['range'], 0.4)),
        ('平均最大偏离VWAP(%)', cur_maxdev, base_mean['maxdev'], flag_abn(cur_maxdev, base_mean['maxdev'], 0.4)),
    ]
    baseline_note = (f"基线=近{args.baseline_days}交易日({', '.join(prior_days)})，"
                     f"仅含可比老标的：{', '.join(baseline_syms) or '无'}；"
                     f"新标的(不参与基线)：{', '.join(new_syms) or '无'}。"
                     f"所有日期均用当前 floor 引擎复算，保证对比口径一致。")

    # ---- Fix3: 实盘权威源 + 推送审计（与复算同屏对照）----
    state_path = os.path.join(ROOT, 'data', 'state.json')
    live_counts = load_live_counts(state_path, target)
    audit_path = os.path.join(ROOT, 'data', 'push_audit.jsonl')
    audit_rows = load_push_audit(audit_path, target)

    # ---- [P2-2 迭代] 实盘/复算 diff 汇总（自动）----
    # 每个标的：实盘计数 vs 复算计数 → 差异绝对值 + 方向（漏推/多算/一致）
    diff_rows = []
    for sym in sorted(set(list(sym_results.keys()) + list(live_counts.keys()))):
        live = live_counts.get(sym, {'B': 0, 'S': 0, 'total': 0})
        recalc = sym_results.get(sym, {}).get('summary')
        rB = (recalc or {}).get('n_B', 0)
        rS = (recalc or {}).get('n_S', 0)
        dB = live['B'] - rB
        dS = live['S'] - rS
        if dB == 0 and dS == 0:
            verdict = '一致'
        elif dB > 0 or dS > 0:
            verdict = '实盘>复算(可能重放/多次同bar)'
        else:
            verdict = '实盘<复算(可能漏推/首扫抑制)'
        diff_rows.append({
            'sym': sym, 'name': (sym_results.get(sym, {}).get('name')
                                 or live_counts.get(sym, {}).get('name', '')),
            'live_B': live['B'], 'live_S': live['S'], 'live_total': live['total'],
            'recalc_B': rB, 'recalc_S': rS, 'recalc_total': (recalc or {}).get('n_signals', 0),
            'delta_B': dB, 'delta_S': dS, 'verdict': verdict,
        })

    # ---- 输出 ----
    out_json = {
        'date': target, 'mode': 'floor (production)', 'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'symbols': {sym: {'name': r['name'], 'summary': r.get('summary'),
                          'stats': r.get('stats'), 'rows': r.get('rows', []),
                          'error': r.get('error')}
                    for sym, r in sym_results.items()},
        'baseline_days': prior_days,
        'baseline_syms': baseline_syms, 'new_syms': new_syms,
        'baseline_mean': base_mean,
        'today_legacy': {k: cur[k] for k in ('n_signals', 'n_B', 'n_S', 'valid', 'invalid')},
        'today_win_rate': cur_win, 'today_range': cur_range, 'today_maxdev': cur_maxdev,
        'comparison': [{'metric': m, 'today': c, 'baseline': b, 'anomaly': f} for m, c, b, f in comparison],
        'baseline_note': baseline_note,
        'live_counts': live_counts,
        'audit_count': len(audit_rows),
        'diff': diff_rows,  # [P2-2] 实盘/复算自动 diff
    }
    os.makedirs(os.path.join(ROOT, 'output'), exist_ok=True)
    jpath = os.path.join(ROOT, 'output', f'review_{target}.json')
    hpath = os.path.join(ROOT, 'output', f'review_{target}.html')
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump(out_json, f, ensure_ascii=False, indent=2)
    with open(hpath, 'w', encoding='utf-8') as f:
        f.write(build_html(target, {k: v for k, v in sym_results.items() if v.get('summary')},
                           {'note': baseline_note}, comparison,
                           live_counts=live_counts, audit_rows=audit_rows, diff_rows=diff_rows))
    print(f"\n[ok] JSON -> {jpath}")
    print(f"[ok] HTML -> {hpath}")

    if args.push:
        txt = (f"【tpoint 信号复盘 {target}】\n"
               f"触发 {cur['n_signals']} 次(买{cur['n_B']}/卖{cur['n_S']})，"
               f"胜率 {cur_win}%（5日基线 {base_mean['win_rate']}%）；"
               f"日内振幅 {cur_range}% vs 基线 {base_mean['range']}%。\n"
               f"报告：{hpath}")
        r = push_feishu_text(txt)
        print(f"[push] {r}")

    return out_json


if __name__ == '__main__':
    main()
