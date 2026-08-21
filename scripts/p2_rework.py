# -*- coding: utf-8 -*-
"""
p2_rework.py -- P2.1 返工评估（回应量化专家 FAIL 的 5 个 blocker）

修复点：
 [B1 笔数混淆] paired 反事实：对基线(off)的同一批成交，仅做"封顶不新开"的反事实
            （用日内 lo + can_sell 判断该笔是否会在 -pct 被砍，是则改以封顶价出场，
             否则保留原出场）—— 保持完全相同的成交总体，剥离单仓位重入场的笔数混淆。
 [B2 指标 gaming] 主指标改为 avg_trip(每笔期望=total_ret/total) / cum_nav / sharpe，
            P/L 盈亏比降为辅助；不再以 P/L 单调涨为选优依据。
 [B3 边界最优+未测<1.2] 加扫 0.8 / 1.0 / 1.1（低于旧边界）与 1.2/1.5/2.0。
 [B4 无OOS] 全局按交易日时间切分：前 60% in-sample 调参，后 40% out-of-sample 验证；
            报告 IS vs OOS 的 avg_trip/cum_nav/sharpe 一致性。
 [B6 EOD/can_sell 异常] 诊断 1.2% 档 ret<-1.3 且非 FIXSTOP 的越界笔，打印 exit_reason
            与 exit_idx 处 can_sell，定位是否为 EOD 强平未校验 can_sell。

输出：p2_rework.json（全口径 + 三口径 baseline/paired/full + IS/OOS）。
"""
import os, sys, json, glob, argparse, datetime
import numpy as np
import pandas as pd
from dataclasses import replace

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import (simulate_day, make_config, cost_for_symbol, limit_thr, aggregate_metrics)
from daily_signal_review import build_data
from p2_diagnose import load_days

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = r'F:/WorkBuddyItem/automation-2026-08-03-09-39-31'

PCTS_FULL = [0.8, 1.0, 1.1, 1.2, 1.5, 2.0]
PCTS_PAIRED = [0.8, 1.0, 1.1, 1.2, 1.5, 2.0]
GAP = 8
IS_RATIO = 0.6


def make_can_sell(prices):
    pc = prices.get('pc'); sym = prices.get('sym'); h = prices.get('h')
    if pc and pc > 0 and sym and h is not None:
        _ld = round(float(pc) * (1 - limit_thr(sym)), 2)
        locked = h <= _ld + 0.02
        return [not bool(x) for x in locked]
    return None


def cap_trip(trip, lo, can_sell, pct, buy_cost, sell_cost):
    """paired 反事实：若基线成交在 [entry_idx+1, exit_idx] 内盘中低点击穿 -pct 且可卖，
    则改为以封顶价出场（仅封顶、不新开）；否则保留原出场。"""
    ei = trip['entry_idx']; xi = trip['exit_idx']; ep = trip['entry_price']
    stop_price = ep * (1 - pct / 100.0)
    seg_lo = lo[ei + 1:xi + 1]
    seg_cs = can_sell[ei + 1:xi + 1] if can_sell is not None else [True] * len(seg_lo)
    for k in range(len(seg_lo)):
        if seg_cs[k] and seg_lo[k] <= stop_price:
            gross = (stop_price - ep) / ep * 100
            net = gross - buy_cost - sell_cost
            return {'ret_pct': round(float(net), 3), 'entry_date': trip.get('entry_date'),
                    'exit_reason': 'FIXSTOP', 'hold_bars': k + 1}
    return trip  # 未触发封顶：保留原成交（含 exit_reason / entry_date / hold_bars）


def analyze_symbol(sym, gap, pcts_full, pcts_paired):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return None
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cost = cost_for_symbol(sym)
    buf = {'dates': [], 'baseline': [], 'full': {p: [] for p in pcts_full},
           'daymeta': {}}  # daymeta[d] = (lo, can_sell)
    prev_close = None
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c,
                           'volume': v, 'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data['trend'], 'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        can_sell = make_can_sell(prices)
        sigs = detect_signals_general(data, pc, replace(GENERAL_DEFAULT, signal_gap=gap))
        cfg_off = make_config(use_stop=False)
        base = simulate_day(sigs, prices, cfg_off, cost)
        for t in base:
            t['date'] = d
        buf['baseline'].extend(base)
        for p in pcts_full:
            cfg = make_config(use_stop=False, use_fixed_stop=True, fixed_stop_pct=p)
            fl = simulate_day(sigs, prices, cfg, cost)
            for t in fl:
                t['date'] = d
            buf['full'][p].extend(fl)
        buf['daymeta'][d] = (lo, can_sell, base, cost)
        buf['dates'].append(d)
        prev_close = c[-1]
    return buf


def paired_for_symbol(buf, pcts_paired):
    """对基线成交做封顶反事实，返回 {pct: [capped trips]}。"""
    out = {p: [] for p in pcts_paired}
    for d, (lo, can_sell, base, cost) in buf['daymeta'].items():
        buy_cost, sell_cost = cost
        for trip in base:
            for p in pcts_paired:
                out[p].append(cap_trip(trip, lo, can_sell, p, buy_cost, sell_cost))
    return out


def split_trips(trips, date2split):
    is_l, os_l = [], []
    for t in trips:
        s = date2split.get(t.get('date'))
        (is_l if s == 'IS' else os_l).append(t)
    return is_l, os_l


def metrics(trips):
    if not trips:
        return None
    m = aggregate_metrics(trips)
    avg_trip = round(m['total_ret'] / m['total'], 4) if m['total'] else None
    return dict(total=m['total'], win_rate=m['win_rate'], avg_win=m['avg_win'],
                avg_loss=m['avg_loss'], pl_ratio=m['pl_ratio'],
                avg_trip=avg_trip, total_ret=m['total_ret'],
                cum_nav=m['cum_nav'], sharpe=m['sharpe'],
                max_dd=m['max_drawdown_pct'], ann_ret=m['ann_ret_pct'],
                worst=m['by_reason'] and min(t['ret_pct'] for t in trips) if trips else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gap', type=int, default=GAP)
    ap.add_argument('--max-syms', type=int, default=0)
    ap.add_argument('--out-suffix', default='rework')
    a = ap.parse_args()

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    if a.max_syms:
        files = files[:a.max_syms]

    buffers = []
    all_dates = set()
    for path in files:
        sym = os.path.basename(path).replace('_1m.csv', '')
        buf = analyze_symbol(sym, a.gap, PCTS_FULL, PCTS_PAIRED)
        if buf is None or not buf['baseline']:
            continue
        buffers.append((sym, buf))
        all_dates.update(buf['dates'])

    sdates = sorted(all_dates)
    thr = sdates[int(IS_RATIO * len(sdates))]
    date2split = {d: ('IS' if d < thr else 'OOS') for d in sdates}
    print(f"[split] n_dates={len(sdates)} IS={len([d for d in sdates if d<thr])} "
          f"OOS={len([d for d in sdates if d>=thr])} threshold={thr}")

    # 聚合三口径 × 时间切分
    result = {'meta': dict(gap=a.gap, is_ratio=IS_RATIO, threshold=thr,
                           pcts_full=PCTS_FULL, pcts_paired=PCTS_PAIRED),
              'baseline': {}, 'paired': {}, 'full': {}, 'anomaly': []}

    def agg_and_store(kind, pct, trips_all):
        is_l, os_l = split_trips(trips_all, date2split)
        result[kind].setdefault(pct, {})['all'] = metrics(trips_all)
        result[kind][pct]['IS'] = metrics(is_l)
        result[kind][pct]['OOS'] = metrics(os_l)

    # baseline
    base_all = []
    for sym, buf in buffers:
        base_all.extend(buf['baseline'])
    agg_and_store('baseline', 'off', base_all)

    # paired (cap-only)
    for p in PCTS_PAIRED:
        paired_all = []
        for sym, buf in buffers:
            paired_all.extend(paired_for_symbol(buf, [p])[p])
        agg_and_store('paired', p, paired_all)

    # full (re-entry)
    for p in PCTS_FULL:
        full_all = []
        for sym, buf in buffers:
            full_all.extend(buf['full'][p])
        agg_and_store('full', p, full_all)

    # 异常诊断：full 1.2% 档 ret<-1.3 且非 FIXSTOP
    full12 = []
    for sym, buf in buffers:
        full12.extend(buf['full'][1.2])
    for t in full12:
        if t['ret_pct'] < -1.3 and t['exit_reason'] != 'FIXSTOP':
            result['anomaly'].append(dict(
                sym=t.get('sym'), date=t.get('date'), ret=t['ret_pct'],
                exit_reason=t['exit_reason'], entry_idx=t['entry_idx'],
                exit_idx=t['exit_idx'], entry_price=t['entry_price'],
                exit_price=t['exit_price']))

    fn = os.path.join(OUT, f'p2_rework_{a.out_suffix}.json')
    json.dump(result, open(fn, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    # 打印主表
    print("\n=== P2 返工主表：avg_trip(每笔期望%) / cum_nav / sharpe / P/L / WR ===")
    print("%-10s %-5s %8s %10s %8s %7s %7s" % ('kind', 'pct', 'avgTrip', 'cum_nav', 'sharpe', 'P/L', 'WR%'))
    rows = []
    rows.append(('baseline', 'off', result['baseline']['off']))
    for p in PCTS_PAIRED:
        rows.append(('paired', p, result['paired'][p]['all']))
    for p in PCTS_FULL:
        rows.append(('full', p, result['full'][p]['all']))
    for kind, p, m in rows:
        m = m['all'] if isinstance(m, dict) and 'all' in m else m
        if m:
            print("%-10s %-5s %8.4f %10.4f %8.3f %7s %7.1f" % (
                kind, str(p), m['avg_trip'], m['cum_nav'], m['sharpe'],
                str(m['pl_ratio']), m['win_rate']))

    print("\n--- baseline vs paired(封顶,同成交) vs full(重入场) @ 关键档, OOS 段 ---")
    for p in [1.0, 1.2, 1.5, 2.0]:
        b = result['baseline']['off'].get('OOS')
        pa = result['paired'].get(p, {}).get('OOS')
        fu = result['full'].get(p, {}).get('OOS')
        def fmt(m):
            return ("avg=%.4f cum=%.3f sh=%.3f PL=%s WR=%.1f" % (
                m['avg_trip'], m['cum_nav'], m['sharpe'], str(m['pl_ratio']), m['win_rate'])) if m else "n/a"
        print("  pct=%s  baseOOS:%s | pairedOOS:%s | fullOOS:%s" % (p, fmt(b), fmt(pa), fmt(fu)))

    print("\nanomaly(1.2档 ret<-1.3 非FIXSTOP) 笔数=%d (示例前8):" % len(result['anomaly']))
    for x in result['anomaly'][:8]:
        print("   ", x)
    print("\nJSON -> %s" % fn)


if __name__ == '__main__':
    main()
