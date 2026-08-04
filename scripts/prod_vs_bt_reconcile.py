#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""prod_vs_bt_reconcile.py — 生产 vs 回测 对账器（R0 基建 · 自迭代优化计划）

口径（计划 radiant-forging-einstein.md 三层分解）：
- WR_prod_exec：实盘推送信号（signal.txt 解析 + push_audit.jsonl 明细合并去重；
  state.json 计数兜底核对）→ exit_manager.simulate_day 同源 round-trip 配对
  （生产出场配置 PROD_CONFIG）→ 扣成本净胜率。度量「用户实际收到的信号」。
- WR_recalc：daily_signal_review.replay_symbol（M.detect_for 生产同源复算，
  含冷却/每日上限/per-symbol mpr/atr 透传）→ 同配对同成本。度量「引擎理论输出」。
- G1 = WR_recalc − WR_prod_exec（执行差距：首扫抑制/重放/人工）。
- G2 = WR_bt − WR_recalc（数据源/口径差距）：WR_bt 由每周 diag_r2p_probe 提供
  （F盘口径，R0 阶段 F盘滞后时记 None，不在本脚本内重算）。

判定纪律：单日信号 <10 笔噪声大 → 单日仅作记录与告警，验收一律用滚动20交易日
（由外层 iteration_state 任务聚合；本脚本输出单日值 + roundtrip 落库供滚动聚合）。

CLI：
  python prod_vs_bt_reconcile.py --date 2026-07-31 [--syms 161129.SZ,688111.SH]
      [--out output/reconcile_2026-07-31.json] [--no-roundtrip]

产物：
  1. output/reconcile_<date>.json（对账结果：per-sym 明细 + pool 汇总）
  2. data/roundtrip/<date>.jsonl（每笔 round-trip 一行，source=live|recalc）
"""
import os, sys, json, re, io, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

# 强制 floor 门控，与生产 run_monitor.bat 一致（import daily_signal_review 时也会设置）
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np

import daily_signal_review as R   # 复用数据加载/复算/计数/审计解析（口径与复盘一致）
from datasource import MootdxDataSource
from exit_manager import make_config, cost_for_symbol, simulate_day, aggregate_metrics
from backtest_screener import load_1m_csv, group_by_day, day_prev_close  # F盘兜底加载

F_DATA = r'F:\keyfactor_data\1m'   # tickflow 1m 历史库（历史日兜底；mootdx 仅3-4天上限）

# 生产出场配置（与 monitor / diag_r2p_probe 一致：仅移动止损 act0.4/trail0.6 + S 信号出场）
PROD_CONFIG = dict(use_stop=False, use_time=False,
                   use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
                   s_signal_exit=True)

SIG_TXT   = os.path.join(ROOT, 'data', 'signal.txt')
AUDIT     = os.path.join(ROOT, 'data', 'push_audit.jsonl')
STATE     = os.path.join(ROOT, 'data', 'state.json')
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')
RT_DIR    = os.path.join(ROOT, 'data', 'roundtrip')

RE_DATE = re.compile(r'^\[(\d{4}-\d{2}-\d{2})\]\s*$')
RE_TS   = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s*$')
RE_SIG  = re.compile(r'^(\U0001F7E2|\U0001F534|\U0001F535)\s+(.+?)\s+(BUY|SELL|EXIT)\b')
RE_PX   = re.compile(r'^现价\s+([0-9.]+)')


# --------------------------------------------------------------------------- #
# 实盘信号三源解析
# --------------------------------------------------------------------------- #
def parse_signal_txt(path, date):
    """解析 data/signal.txt → [{name, op, hhmmss, price}]（仅目标日期）。
    格式：[YYYY-MM-DD] 日期块 / [HH:MM:SS] 时间行 / 🟢🔴🔵 信号行 / 现价 X（…）。"""
    out = []
    if not os.path.exists(path):
        return out
    cur_date = None; cur_ts = None; pend = None
    with io.open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = RE_DATE.match(line)
            if m:
                cur_date = m.group(1); cur_ts = None; pend = None
                continue
            m = RE_TS.match(line)
            if m:
                cur_ts = m.group(1); pend = None
                continue
            m = RE_SIG.match(line)
            if m:
                _emoji, name, op = m.groups()
                pend = {'name': name.strip(),
                        'op': {'BUY': 'B', 'SELL': 'S', 'EXIT': 'X'}[op],
                        'date': cur_date, 'hhmmss': cur_ts}
                continue
            m = RE_PX.match(line)
            if m and pend is not None:
                pend['price'] = float(m.group(1))
                if pend.get('date') == date and pend.get('hhmmss'):
                    out.append(pend)
                pend = None
    return out


def name_to_sym(name, name2sym):
    """signal.txt 名称 → 标的代码：精确匹配 → 去空格匹配 → 双向包含匹配。"""
    if name in name2sym:
        return name2sym[name]
    key = name.replace(' ', '')
    for nm, sym in name2sym.items():
        if nm.replace(' ', '') == key:
            return sym
    for nm, sym in name2sym.items():
        if key and (key in nm.replace(' ', '') or nm.replace(' ', '') in key):
            return sym
    return None


def merge_live_signals(txt_rows, audit_rows, date, name2sym):
    """合并 signal.txt 与 push_audit.jsonl 的实盘信号。
    去重规则：sym+op+同分钟 视为同一信号（两源都记录推送），价格取 signal.txt 优先。
    返回 [{sym, op, hhmmss, price, src}]。"""
    merged = {}
    # push_audit 先打底（ts='YYYY-MM-DD HH:MM:SS'，sym 直接是代码）
    for r in audit_rows:
        ts = r.get('ts', '')
        if not ts.startswith(date):
            continue
        sym = r.get('sym'); op = r.get('type')
        if op not in ('B', 'S', 'X') or not sym:
            continue
        hhmmss = ts[11:19] if len(ts) >= 19 else None
        key = (sym, op, hhmmss[:5] if hhmmss else '')
        merged[key] = {'sym': sym, 'op': op, 'hhmmss': hhmmss,
                       'price': float(r.get('price') or 0.0), 'src': 'audit'}
    # signal.txt 覆盖（名称需映射）
    for r in txt_rows:
        sym = name_to_sym(r['name'], name2sym)
        if not sym:
            continue
        key = (sym, r['op'], r['hhmmss'][:5])
        merged[key] = {'sym': sym, 'op': r['op'], 'hhmmss': r['hhmmss'],
                       'price': float(r.get('price') or 0.0), 'src': 'signal_txt'}
    out = sorted(merged.values(), key=lambda x: (x['sym'], x['hhmmss'] or ''))
    return out


# --------------------------------------------------------------------------- #
# 数据获取：mootdx 主源（当日/近日）→ F盘 tickflow 兜底（历史日）
# --------------------------------------------------------------------------- #
def fetch_day_data(ds, sym, date):
    """返回 (data, df_day, pc, source)；失败返回 (None, None, None, reason)。
    source: 'mootdx' | 'fdisk'。F盘兜底使历史日（>mootdx 3-4天上限）可对账。"""
    # ---- 主源：mootdx（与 daily_signal_review 完全同路径）----
    try:
        pc = R.get_pc(ds, sym, date)
        df = R.fetch_1m(ds, sym, date)
        if df is not None and len(df) >= 5 and pc is not None and pc > 0:
            return R.build_data(df, pc), df, pc, 'mootdx'
    except Exception:
        pass
    # ---- 兜底：F盘 tickflow ----
    csv_path = os.path.join(F_DATA, f'{sym}_1m.csv')
    if not os.path.exists(csv_path):
        return None, None, None, 'no_data(mootdx+F盘缺文件)'
    try:
        full = load_1m_csv(csv_path)
        pc = day_prev_close(full, date)
        sub = None
        for d, g in group_by_day(full):
            if d == date:
                sub = g
                break
        if sub is None or len(sub) < 5 or pc is None or pc <= 0:
            return None, None, None, f'no_data(F盘无{date}或pc缺失)'
        sub = sub.reset_index(drop=True)
        return R.build_data(sub, pc), sub, pc, 'fdisk'
    except Exception as e:
        return None, None, None, f'fdisk_error({e})'


# --------------------------------------------------------------------------- #
# 信号 → simulate_day 输入
# --------------------------------------------------------------------------- #
def time_to_idx(trade_times, hhmmss):
    """信号时间 → bar idx：先 HH:MM 精确匹配，否则取 <= 信号时间的最后一根 bar。"""
    if hhmmss is None or trade_times is None:
        return -1
    hhmm = hhmmss[:5]
    cand = -1
    for k, t in enumerate(trade_times):
        ts = str(t)
        if len(ts) >= 16 and ts[11:16] == hhmm:
            return k
        if len(ts) >= 19 and ts[11:19] <= hhmmss:
            cand = k
    return cand


def recalc_rows_to_sigs(rows, trade_times, n):
    """replay_symbol rows → simulate_day 信号（仅 B/S；X 由 simulate_day 内部生成）。"""
    out = []
    for r in rows:
        if r['type'] not in ('B', 'S'):
            continue
        idx = time_to_idx(trade_times, (r.get('time') or '')[11:19] or None)
        if idx < 0 or idx >= n:
            continue
        out.append({'type': r['type'], 'idx': idx, 'price': float(r['price']),
                    'reason': r.get('tag', '')})
    return out


def live_rows_to_sigs(live_rows, sym, trade_times, n, closes):
    """实盘信号 → simulate_day 信号（仅 B/S）。
    ⚠️ 口径（2026-08-03 确立）：entry_price 一律取信号 bar 的 close（与复算口径一致），
    不用 signal.txt 推送价——实盘推送价来自当日实时源，与 F盘历史价存在复权/口径差
    （07-24 实证：推送2.236 vs F盘bar 2.18，差2.6%），混用会产生系统性错位。
    推送价与 bar close 的差记为 push_slip_pct（执行滑点参考，不进配对）。"""
    out = []
    for r in live_rows:
        if r['sym'] != sym or r['op'] not in ('B', 'S'):
            continue
        idx = time_to_idx(trade_times, r.get('hhmmss'))
        if idx < 0 or idx >= n:
            continue
        bar_close = float(closes[idx])
        slip = None
        if r.get('price'):
            slip = round((bar_close - float(r['price'])) / float(r['price']) * 100, 3)
        out.append({'type': r['op'], 'idx': idx, 'price': bar_close,
                    'reason': 'live_' + r.get('src', ''),
                    'push_price': r.get('price'), 'push_slip_pct': slip})
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description='生产 vs 回测 对账器（R0）')
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--syms', default='', help='逗号分隔；默认全 watchlist')
    ap.add_argument('--out', default='', help='默认 output/reconcile_<date>.json')
    ap.add_argument('--no-roundtrip', action='store_true', help='不落 roundtrip jsonl')
    args = ap.parse_args()
    date = args.date

    watch = json.load(open(WATCHLIST, encoding='utf-8'))
    syms = [s for s in (args.syms.split(',') if args.syms else list(watch.keys())) if s]
    name2sym = {v: k for k, v in watch.items()}

    # 三源实盘数据（全标的加载一次，per-sym 过滤在使用处）
    txt_rows   = parse_signal_txt(SIG_TXT, date)
    audit_rows = R.load_push_audit(AUDIT, date)
    live_all   = merge_live_signals(txt_rows, audit_rows, date, name2sym)
    state_counts = R.load_live_counts(STATE, date)

    # 2026-08-04 修复：live_counts 改为「audit/txt 合并明细优先，state.json 交叉校验」。
    # 根因：落盘断流时 state 计数同样失真，且 monitor 内存态会在事后覆盖磁盘 state.json
    # （当日实证：人工修正 state 后被 monitor 旧内存态回写覆盖），state 不可作唯一权威。
    # 明细逐笔可验证（含飞书 code=0 确认），作为权威；state 仅作交叉，差异置 flag 告警。
    detail_counts = {}
    for r in live_all:
        if r['op'] not in ('B', 'S'):
            continue
        c = detail_counts.setdefault(r['sym'], {'B': 0, 'S': 0, 'total': 0})
        c[r['op']] += 1
        c['total'] += 1
    live_counts = {}
    for sym in set(list(state_counts.keys()) + list(detail_counts.keys())):
        dc = detail_counts.get(sym, {'B': 0, 'S': 0, 'total': 0})
        sc = state_counts.get(sym, {'B': 0, 'S': 0, 'total': 0})
        if dc['total'] > 0:
            live_counts[sym] = dict(dc)
            live_counts[sym]['src'] = 'audit_detail'
        else:
            live_counts[sym] = dict(sc)
            live_counts[sym]['src'] = 'state' if sc['total'] > 0 else 'none'
        if dc['total'] != sc['total']:
            live_counts[sym]['state_mismatch'] = sc  # state 与明细不一致 → 数据质量哨兵

    ds = MootdxDataSource()
    mcfg = make_config(**PROD_CONFIG)

    result = {'date': date, 'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'config': 'PROD_CONFIG(trail0.4/0.6+s_exit, no stop/time) + cost_for_symbol',
              'symbols': {}, 'pool': {}}
    rt_records = []

    pool_live_trips, pool_recalc_trips = [], []
    for sym in syms:
        name = watch.get(sym, sym)
        rec = {'name': name}
        lc = live_counts.get(sym, {'B': 0, 'S': 0, 'total': 0})
        live_sym = [r for r in live_all if r['sym'] == sym]

        # ---- 复算（WR_recalc 口径：detect_for 生产同源）----
        data, df, pc, src = fetch_day_data(ds, sym, date)
        if data is None:
            rec['error'] = src
            rec['live_counts'] = lc
            result['symbols'][sym] = rec
            continue
        rows, stats = R.replay_symbol(sym, name, data, pc)
        tt = df['trade_time'].values if 'trade_time' in df.columns else None
        cost = cost_for_symbol(sym)
        prices = {'o': data['o'], 'h': data['h'], 'lo': data['lo'], 'c': data['c'],
                  'atr': data['atr'], 'trend': data.get('trend'), 'n': data['n']}

        recalc_sigs  = recalc_rows_to_sigs(rows, tt, data['n'])
        recalc_trips = simulate_day(recalc_sigs, prices, mcfg, cost=cost)

        # ---- 实盘（WR_prod_exec 口径）----
        live_sigs  = live_rows_to_sigs(live_sym, sym, tt, data['n'], data['c'])
        live_trips = simulate_day(live_sigs, prices, mcfg, cost=cost) if live_sigs else []
        live_detail_ok = len(live_sigs) > 0
        push_slips = [s['push_slip_pct'] for s in live_sigs if s.get('push_slip_pct') is not None]

        m_r = aggregate_metrics(recalc_trips)
        m_l = aggregate_metrics(live_trips)

        n_recalc_bs = len(recalc_sigs)
        rec.update({
            'pc': round(float(pc), 3), 'data_src': src,
            'live_counts': lc,                       # 明细优先(audit/txt合并)，state交叉校验(08-04起)
            'live_detail_n': len(live_sym),          # 有明细的实盘信号数（txt+audit 合并）
            'live_pairable_n': len(live_sigs),       # 可配对（映射到bar）的 B/S 数
            'live_detail_ok': live_detail_ok,
            'live_push_slip_pct': push_slips,        # 推送价 vs bar close 滑点（参考）
            'recalc_n_signals': len(rows),           # 复算信号总数（含X）
            'recalc_n_bs': n_recalc_bs,              # 复算 B/S 数（喂 simulate_day）
            'delta_total': lc.get('total', 0) - len(rows),   # 实盘-复算 信号数差
            'recalc_trips': m_r, 'live_trips': m_l,
            'wr_recalc': (m_r['win_rate'] if m_r['total'] > 0 else None),
            'wr_prod_exec': (m_l['win_rate'] if live_detail_ok and m_l['total'] > 0 else None),
            'g1_sym': (round(m_r['win_rate'] - m_l['win_rate'], 1)
                       if live_detail_ok and m_l['total'] > 0 and m_r['total'] > 0 else None),
        })
        result['symbols'][sym] = rec

        for t in recalc_trips:
            rt_records.append({'date': date, 'sym': sym, 'source': 'recalc', **t})
        for t in live_trips:
            rt_records.append({'date': date, 'sym': sym, 'source': 'live', **t})
        pool_live_trips.extend(live_trips)
        pool_recalc_trips.extend(recalc_trips)

    # ---- pool 汇总 ----
    mp_r = aggregate_metrics(pool_recalc_trips)
    mp_l = aggregate_metrics(pool_live_trips)
    wr_recalc = mp_r['win_rate'] if mp_r['total'] > 0 else None
    wr_prod   = mp_l['win_rate'] if mp_l['total'] > 0 else None
    g1 = round(wr_recalc - wr_prod, 1) if (wr_recalc is not None and wr_prod is not None) else None
    result['pool'] = {
        'wr_prod_exec': wr_prod, 'n_live_trips': mp_l['total'],
        'wr_recalc': wr_recalc, 'n_recalc_trips': mp_r['total'],
        'g1_pp': g1, 'g2_pp': None,   # G2 由每周 diag_r2p_probe（F盘口径）补充
        'live_pl_ratio': mp_l['pl_ratio'], 'recalc_pl_ratio': mp_r['pl_ratio'],
        'live_total_ret_pct': mp_l.get('total_ret_pct'), 'recalc_total_ret_pct': mp_r.get('total_ret_pct'),
        'note': ('单日<10笔噪声大，验收用滚动20交易日；' if (mp_l['total'] + mp_r['total']) < 10 else '') +
                ('实盘信号无明细(仅state计数)，WR_prod_exec 当日不可算' if mp_l['total'] == 0 else ''),
    }

    # ---- 落库 roundtrip ----
    if not args.no_roundtrip:
        os.makedirs(RT_DIR, exist_ok=True)
        rt_path = os.path.join(RT_DIR, f'{date}.jsonl')
        with io.open(rt_path, 'w', encoding='utf-8') as f:
            for r in rt_records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        result['roundtrip_path'] = os.path.relpath(rt_path, ROOT)

    out_path = args.out or os.path.join(ROOT, 'output', f'reconcile_{date}.json')
    with io.open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    print(f'[reconcile {date}] syms={len(result["symbols"])}')
    print(f'  pool WR_prod_exec={wr_prod} (n={mp_l["total"]}) | WR_recalc={wr_recalc} (n={mp_r["total"]}) | G1={g1}pp')
    for sym, rec in result['symbols'].items():
        if rec.get('error'):
            print(f'  {sym}: {rec["error"]} live_counts={rec["live_counts"]}')
        else:
            print(f'  {sym}: live={rec["live_counts"]["total"]}(明细{rec["live_detail_n"]}) '
                  f'recalc={rec["recalc_n_signals"]}(BS{rec["recalc_n_bs"]}) '
                  f'delta={rec["delta_total"]:+d} wr_live={rec["wr_prod_exec"]} wr_recalc={rec["wr_recalc"]}')
    print(f'  -> {os.path.relpath(out_path, ROOT)}')


if __name__ == '__main__':
    main()
