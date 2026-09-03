# -*- coding: utf-8 -*-
"""
core/simulate_position_sm.py — 单一仓位状态机模拟器（T1.5 核心交付，2026-09-03）

为什么需要它（P0-20260903-reverseT-not-modeled 的根治修复）：
  1) exit_manager.simulate_day：纯多头——反T 日的 S 被忽略、B 回补被误判正T 建仓 → 系统性虚亏。
  2) simulate_bidirectional：空仓遇 S 直接建仓（裸卖空），且净收益翻转实现为
     -(gross-cost)= -gross+cost —— 成本变补贴，反T 每笔虚增 2×往返成本。
  3) 两者被 p10/p11/p7 等无条件相加 —— 同一组信号双重计费。
  4) simulate_base_position：有底仓门控，但持底仓日仍是两模拟器并行（L63-64），
     信号重复使用缺陷同构。

本模块 = monitor 实盘信号处理（core/monitor.py L1290-1565 单一 pos 状态机）的回测镜像：
  一笔信号只做一个动作（与实盘一致）：
    pos=None + B           → 开多（正T 建仓）
    pos=None + S + has_base → 开空（反T 建仓 = 卖底仓）
    pos=long  + S          → 平多（正T 出场，reason='S'）
    pos=short + B          → 平空（反T 回补，reason='B回补'）
    pos=long  + B / pos=short + S → 忽略（v1 不镜像实盘加仓，已知简化，
                                    与 live_roundtrip_review 全量配对口径一致）
  出场优先级按方向解耦（与 monitor P3.2 一致）：
    long : FIXSTOP > STOP > S信号 > TRAIL > TIME > EOD
    short: FIXSTOP > STOP > B信号 > TRAIL > TIME > EOD
  成本按方向正确扣：long 净 = gross - buy_cost - sell_cost；
                    short 净 = -gross_long_form - buy_cost - sell_cost（成本是支出，不是补贴）。
  跨日：日内 pos EOD 强平（对齐 monitor 15:00 EOD）；底仓(has_base)跨日由调用方管理（ledger）。

trip 结构与 exit_manager._mk_trip 同构（aggregate_metrics 可直接消费），另附 side='B'/'S'。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.exit_manager import make_config, _mk_trip, limit_thr  # noqa: E402


def _short_trip(pos, exit_idx, exit_price, reason, buy_cost, sell_cost, entry_date=None):
    """反T trip：entry=卖出价，exit=回补价。毛收益=(entry-exit)/entry；
    净收益=毛收益-双边成本（成本按支出扣除——修复 simulate_bidirectional 的
    翻转实现中成本变补贴的 bug：那里 net=-gross+cost，本处 net=-gross-cost）。"""
    t = _mk_trip(pos, exit_idx, exit_price, reason, 0.0, 0.0, entry_date=entry_date)
    t['gross_ret_pct'] = round(-float(t['gross_ret_pct']), 3)
    t['ret_pct'] = round(float(t['gross_ret_pct']) - buy_cost - sell_cost, 3)
    t['side'] = 'S'
    return t


def simulate_position_sm(sigs_by_day, prices_by_day, config_long=None, config_short=None,
                         cost=None, has_base=True):
    """单一仓位状态机跨日模拟。

    参数：
      sigs_by_day   : list of (date, sigs)，sigs=[{'type':'B'/'S','idx','price','reason'},...]
                      按日期升序；同日内按 idx 顺序处理。
      prices_by_day : list of (date, prices_dict)，与 sigs_by_day 对齐；
                      prices_dict 契约同 simulate_day（c/h/lo/atr/n/trend/pc/sym/date）。
      config_long   : 正T 出场配置（默认 make_config()；生产传 EXIT_CFG 类）
      config_short  : 反T 出场配置（默认同 config_long；生产传 EXIT_CFG_SHORT 类）
      cost          : (buy_cost, sell_cost) 成本率元组，调用方传 cost_for_symbol(sym)
      has_base      : 是否持有底仓（反T 的物理前提）。无底仓时 S 不建空仓（禁裸卖空）。
                      跨日底仓状态由调用方通过逐日传入控制（ledger 模式）。

    返回 dict：
      trips   : 全部 round-trip（side='B' 正T / 'S' 反T）
      n_long/n_short : 正T/反T trip 数
      summary : {'long_net','short_net','n_long','n_short'}
    """
    cfg_l = config_long or make_config()
    cfg_s = config_short or cfg_l
    buy_cost, sell_cost = cost if cost else (0.0, 0.0)
    trips = []
    n_long = n_short = 0

    for day_i, (date, sigs) in enumerate(sigs_by_day):
        prices = prices_by_day[day_i][1]
        n = prices['n']
        c = prices['c']
        # h/lo 统一转 np.array（向量化涨跌停锁定判定，与 simulate_day 同语义；
        # 调用方传 list 时 list<=float 不支持）
        h = np.asarray(prices.get('h')) if prices.get('h') is not None else None
        lo = np.asarray(prices.get('lo')) if prices.get('lo') is not None else None
        atr = prices['atr']; trend = prices.get('trend')
        day_date = prices.get('date')
        # 涨跌停锁定（镜像 simulate_day/simulate_bidirectional 的成交可行性）
        _pc = prices.get('pc'); _sym = prices.get('sym')
        locked_down = None
        locked_up = None
        if _pc and _pc > 0 and _sym:
            if h is not None:
                _ld = round(float(_pc) * (1 - limit_thr(_sym)), 2)
                locked_down = h <= _ld + 0.02
            if lo is not None:
                _lu = round(float(_pc) * (1 + limit_thr(_sym)), 2)
                locked_up = lo >= _lu - 0.02

        # 信号按 idx 索引
        b_idx = {s['idx']: s for s in sigs if s['type'] == 'B'}
        s_idx = {s['idx']: s for s in sigs if s['type'] == 'S'}

        pos = None  # {'side','entry_idx','entry_price','entry_reason','stop_price','max_fav','min_fav'}
        for i in range(2, n):
            if pos is not None:
                side = pos['side']
                is_long = side == 'long'
                ecfg = cfg_l if is_long else cfg_s
                can_exit = True
                if is_long and locked_down is not None and bool(locked_down[i]):
                    can_exit = False  # 多仓锁跌停不可卖
                if (not is_long) and locked_up is not None and bool(locked_up[i]):
                    can_exit = False  # 空仓锁涨停不可回补

                if is_long and c[i] > pos['max_fav']:
                    pos['max_fav'] = float(c[i])
                if (not is_long) and c[i] < pos['min_fav']:
                    pos['min_fav'] = float(c[i])

                if can_exit:
                    # 0) FIXSTOP（long 亏 lo 触及 / short 反弹 hi 触及）
                    if ecfg.get('use_fixed_stop', False):
                        fs_pct = ecfg.get('fixed_stop_pct', 1.5)
                        if is_long:
                            fs_price = pos['entry_price'] * (1 - fs_pct / 100.0)
                            if lo is not None and lo[i] <= fs_price:
                                mk = _mk_trip
                                trips.append(mk(pos, i, fs_price, 'FIXSTOP', buy_cost, sell_cost, entry_date=day_date))
                                pos = None; continue
                        else:
                            fs_price = pos['entry_price'] * (1 + fs_pct / 100.0)
                            if h is not None and h[i] >= fs_price:
                                trips.append(_short_trip(pos, i, fs_price, 'FIXSTOP', buy_cost, sell_cost, entry_date=day_date))
                                pos = None; continue
                    # 1) STOP
                    if ecfg['use_stop']:
                        if ecfg['stop_mode'] == 'trend':
                            hit = (trend is not None and ((is_long and trend[i] == -1) or ((not is_long) and trend[i] == 1)))
                            if hit:
                                if is_long:
                                    trips.append(_mk_trip(pos, i, c[i], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                                else:
                                    trips.append(_short_trip(pos, i, c[i], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                                pos = None; continue
                        else:
                            if is_long and lo is not None and lo[i] <= pos['stop_price']:
                                trips.append(_mk_trip(pos, i, pos['stop_price'], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                                pos = None; continue
                            if (not is_long) and h is not None and h[i] >= pos['stop_price']:
                                trips.append(_short_trip(pos, i, pos['stop_price'], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                                pos = None; continue
                    # 2) 反向信号自然平仓（正T: S / 反T: B回补）
                    if is_long and ecfg['s_signal_exit'] and i in s_idx:
                        trips.append(_mk_trip(pos, i, s_idx[i]['price'], 'S', buy_cost, sell_cost, entry_date=day_date))
                        pos = None; continue
                    if (not is_long) and ecfg['s_signal_exit'] and i in b_idx:
                        trips.append(_short_trip(pos, i, b_idx[i]['price'], 'B回补', buy_cost, sell_cost, entry_date=day_date))
                        pos = None; continue
                    # 3) TRAIL
                    if ecfg['use_trailing']:
                        if is_long:
                            fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                            if fav_ret >= ecfg['trail_activate_pct']:
                                trail_stop = pos['max_fav'] * (1 - ecfg['trail_pct'] / 100.0)
                                if c[i] <= trail_stop and trail_stop > pos['stop_price']:
                                    trips.append(_mk_trip(pos, i, c[i], 'TRAIL', buy_cost, sell_cost, entry_date=day_date))
                                    pos = None; continue
                        else:
                            fav_ret = (pos['entry_price'] - pos['min_fav']) / pos['entry_price'] * 100
                            if fav_ret >= ecfg['trail_activate_pct']:
                                trail_stop = pos['min_fav'] * (1 + ecfg['trail_pct'] / 100.0)
                                if c[i] >= trail_stop and trail_stop < pos['stop_price']:
                                    trips.append(_short_trip(pos, i, c[i], 'TRAIL', buy_cost, sell_cost, entry_date=day_date))
                                    pos = None; continue
                    # 4) TIME
                    if ecfg['use_time'] and (i - pos['entry_idx']) >= ecfg['time_stop_bars']:
                        if is_long:
                            trips.append(_mk_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost, entry_date=day_date))
                        else:
                            trips.append(_short_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost, entry_date=day_date))
                        pos = None; continue

            # ---- 空仓：入场判定（一笔信号一个动作） ----
            if pos is None:
                if i in b_idx:
                    b = b_idx[i]
                    pos = {'side': 'long', 'entry_idx': i, 'entry_price': b['price'],
                           'entry_reason': b.get('reason', ''),
                           'stop_price': (b['price'] - cfg_l['stop_atr_mult'] * atr[i]
                                          if cfg_l['use_stop'] else -1e9),
                           'max_fav': b['price'], 'min_fav': b['price']}
                elif i in s_idx and has_base:
                    # 反T 建仓 = 卖底仓（物理前提：has_base）。无底仓禁裸卖空。
                    s = s_idx[i]
                    pos = {'side': 'short', 'entry_idx': i, 'entry_price': s['price'],
                           'entry_reason': s.get('reason', ''),
                           'stop_price': (s['price'] + cfg_s['stop_atr_mult'] * atr[i]
                                          if cfg_s['use_stop'] else 1e9),
                           'max_fav': s['price'], 'min_fav': s['price']}
                continue

        # ---- EOD 强平 ----
        if pos is not None:
            is_long = pos['side'] == 'long'
            can_eod = True
            if is_long and locked_down is not None and bool(locked_down[n - 1]):
                can_eod = False
            if can_eod:
                if is_long:
                    trips.append(_mk_trip(pos, n - 1, c[n - 1], 'EOD', buy_cost, sell_cost, entry_date=day_date))
                else:
                    trips.append(_short_trip(pos, n - 1, c[n - 1], 'EOD', buy_cost, sell_cost, entry_date=day_date))

    for t in trips:
        t.setdefault('side', 'B')
    n_long = sum(1 for t in trips if t['side'] == 'B')
    n_short = sum(1 for t in trips if t['side'] == 'S')
    long_net = sum(t['ret_pct'] for t in trips if t['side'] == 'B')
    short_net = sum(t['ret_pct'] for t in trips if t['side'] == 'S')
    return {
        'trips': trips,
        'n_long': n_long, 'n_short': n_short,
        'summary': {'long_net': round(long_net, 3), 'short_net': round(short_net, 3),
                    'n_long': n_long, 'n_short': n_short},
    }

