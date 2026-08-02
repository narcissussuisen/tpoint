# -*- coding: utf-8 -*-
"""
probe_gate_lookahead.py — 日级趋势闸门的前视偏差对照实验（2026-08-01 用户质疑）

用户问题："日级趋势闸门里，当时你怎么知道当天不是单边下跌日？"

核心：收盘<开盘 / 当日收跌 这类"日级标签"要到 15:00 才知道，盘中信号触发时无法判定
     ——若拿它过滤盘中信号 = 前视偏差(look-ahead bias)，回测胜率虚高。

实验设计（同一批 trips / 同一撮合，只换闸门判定源）：
  A. 无闸门                 : 现状基线（floor 抄底全放行）
  B. 事后日级闸门(前视!)    : 用"当日最终收盘<开盘"过滤——回测里能算，实盘无法实现 → 演示虚高
  C. 盘中可判定闸门(无前视) : 信号触发当 bar 即可判定的状态量
       C1: price < VWAP 且 当日跌幅 < -1.5%  (强弱势可判定)
       C2: trend_strong == -1 且 当日跌幅 < -1.5%
       C3: 仅 price < VWAP（弱化版）

结论口径：胜率/盈亏比/笔数对比；B vs C 的差距 = 前视偏差的"水分"。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault('MACD_GATE_MODE', 'floor')

import numpy as np

from core.miji_alpha import (compute_miji_indicators, detect_miji_signals,
                             compute_trend_strength)
from core.exit_manager import (simulate_day, aggregate_metrics, make_config,
                               cost_for_symbol)
from scripts.backtest_screener import (PROD_CONFIG, load_1m_csv, group_by_day,
                                       day_prev_close)

SYMBOLS = {
    '688146.SH': 'F:/keyfactor_data/1m/688146.SH_1m.csv',
    '600206.SH': 'F:/keyfactor_data/1m/600206.SH_1m.csv',
    '688347.SH': 'F:/keyfactor_data/1m/688347.SH_1m.csv',
    '600584.SH': 'F:/keyfactor_data/1m/600584.SH_1m.csv',
    '688766.SH': 'F:/keyfactor_data/1m/688766.SH_1m.csv',
    '161129.SZ': 'F:/keyfactor_data/1m/161129.SZ_1m.csv',
    '513310.SH': 'F:/keyfactor_data/1m/513310.SH_1m.csv',
    '688111.SH': 'F:/keyfactor_data/1m/688111.SH_1m.csv',
}


def run_day(sigs, prices, mcfg, cost, gate_mode):
    """按闸门模式过滤 B 信号后配对。gate_mode:
      none        = 不过滤
      post_close  = 事后日级闸门: 当日最终收盘<开盘 则当日全部B作废(前视)
      realtime_pv = 盘中可判定: 信号bar 触发时 price<VWAP 且 day_chg<-1.5% 才放行
      realtime_ts = 盘中可判定: 信号bar 触发时 trend_strong==-1 且 day_chg<-1.5% 才放行
      realtime_p  = 盘中可判定: 信号bar 触发时 仅 price<VWAP
    """
    if gate_mode == 'none':
        return simulate_day(sigs, prices, mcfg, cost=cost)
    o = prices['o']; c = prices['c']; vwap = prices['vwap']
    pc = prices['pc']; n = prices['n']
    trend_s = prices.get('trend_strong')
    # 事后日级闸门: 需要当日收盘 vs 开盘（模拟"已知当日结局"）
    if gate_mode == 'post_close':
        day_close_lt_open = (c[-1] < o[0])
        if day_close_lt_open:
            sigs = [s for s in sigs if s['type'] != 'B']
        return simulate_day(sigs, prices, mcfg, cost=cost)
    # 盘中可判定: 逐信号判定
    filtered = []
    for s in sigs:
        if s['type'] != 'B':
            filtered.append(s)
            continue
        i = s['idx']
        day_chg = (c[i] / pc - 1) * 100 if pc > 0 else 0.0
        if gate_mode == 'realtime_pv':
            ok = (c[i] < vwap[i]) and (day_chg < -1.5)
        elif gate_mode == 'realtime_ts':
            ok = (trend_s is not None and trend_s[i] == -1) and (day_chg < -1.5)
        elif gate_mode == 'realtime_p':
            ok = c[i] < vwap[i]
        else:
            ok = True
        if ok:
            filtered.append(s)
    return simulate_day(filtered, prices, mcfg, cost=cost)


def main():
    mcfg = make_config(**PROD_CONFIG)
    gates = ['none', 'post_close', 'realtime_pv', 'realtime_ts', 'realtime_p']
    agg = {g: {'trips': 0, 'win': 0.0, 'pl': 0.0, 'ann': 0.0} for g in gates}
    print(f'{"标的":<12}', end='')
    for g in gates:
        print(f'{"[A]无闸门":>12}{"[B]事后日级(前视)":>20}{"[C1]盘中VWAP+跌":>20}{"[C2]盘中强跌+跌":>20}{"[C3]盘中仅VWAP":>18}')
        break
    per_sym = {}
    for sym, path in SYMBOLS.items():
        if not os.path.exists(path):
            continue
        cost = cost_for_symbol(sym)
        df = load_1m_csv(path)
        days = group_by_day(df)
        trips_by_gate = {g: [] for g in gates}
        for date, sub in days:
            pc = day_prev_close(df, date)
            if pc is None or pc <= 0:
                continue
            o = sub['open'].values.astype(float)
            h = sub['high'].values.astype(float)
            lo = sub['low'].values.astype(float)
            c = sub['close'].values.astype(float)
            v = sub['volume'].values.astype(float)
            data = compute_miji_indicators(o, h, lo, c, v, pc)
            sigs = detect_miji_signals(data, pc)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'vwap': data['vwap'],
                      'pc': pc, 'n': data['n'], 'atr': data['atr'],
                      'trend_strong': data.get('trend_strong')}
            for g in gates:
                trips_by_gate[g].extend(run_day(sigs, prices, mcfg, cost, g))
        row = {}
        print(f'{sym:<12}', end='')
        for g in gates:
            m = aggregate_metrics(trips_by_gate[g])
            row[g] = m
            print(f'{"n"+str(m["total"]):>12}{str(m["win_rate"])+"%":>20}{str(m["pl_ratio"]):>20}', end='')
        print()
        per_sym[sym] = row
    # 汇总
    print('\n===== 汇总（加权平均胜率） =====')
    for g in gates:
        tot = sum(per_sym[s][g]['total'] for s in per_sym)
        wins = sum(per_sym[s][g]['win_rate'] / 100 * per_sym[s][g]['total'] for s in per_sym)
        wr = wins / tot * 100 if tot else 0
        print(f'  {g:<14} 总笔数 {tot:>4}  加权胜率 {wr:.1f}%')


if __name__ == '__main__':
    main()
