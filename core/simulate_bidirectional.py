# -*- coding: utf-8 -*-
"""core/simulate_bidirectional.py — 双向做T模拟（2026-08-20 新增）

对齐 R2P G-F3 / Track D：补齐「反T（S→B 先卖后买）」配对，与正T（B→S）合成双向做T。
- 正T：simulate_day（B 建仓 → S/止损/移动/时间出场）
- 反T：simulate_bidirectional（S 建仓 → B/止损/移动/时间出场），收益 = (entry−exit)/entry（卖高买低）

出场规则镜像正T（全部可配，默认=生产默认 make_config）：
  1. 硬止损：价格反弹触及 stop_price（entry + stop_atr_mult*atr，反T向上止损）
  2. B 信号出场（反T自然平仓）
  3. 移动止损：浮盈(下跌)达 trail_activate_pct 后，价格回升 trail_pct 触发
  4. 时间止损：time_stop_bars 根未平仓强平
  5. EOD：收盘强平

返回 round_trips 与 simulate_day 同构（ret_pct 为扣除双边成本的净收益，side='S' 标记反T）。
"""
import numpy as np

from exit_manager import make_config, _mk_trip  # noqa: F401  # 复用 make_config 默认与 trip 构造


def simulate_bidirectional(signals, prices, config=None, cost=None):
    """反T（S→B 先卖后买）配对模拟。signals/prices 同 simulate_day 契约。

    返回 trips：每条含 entry_idx/exit_idx/entry_price/exit_price/exit_reason/
    ret_pct(净)/gross_ret_pct(毛)/hold_bars/entry_reason/side='S'。
    """
    if config is None:
        config = make_config()
    if cost is None:
        cost = (0.0, 0.0)  # 调用方传 cost_for_symbol 以含印花税；此处不静默给默认值
    buy_cost, sell_cost = cost
    n = prices['n']
    c = prices['c']; h = prices.get('h'); lo = prices['lo']; atr = prices['atr']
    trend = prices.get('trend')
    day_date = prices.get('date')
    # 反T方向锁涨停不可平（无法买回）；镜像正T的 locked_down
    _pc = prices.get('pc'); _sym = prices.get('sym')
    from exit_manager import limit_thr
    locked_up = None
    if _pc and _pc > 0 and _sym and lo is not None:
        _lu = round(float(_pc) * (1 + limit_thr(_sym)), 2)
        locked_up = lo >= _lu - 0.02

    s_idx = {s['idx']: s for s in signals if s['type'] == 'S'}
    b_idx = {s['idx']: s for s in signals if s['type'] == 'B'}

    trips = []
    pos = None  # 反T空仓状态
    for i in range(2, n):
        if pos is None:
            # 空仓 → 找S建仓（先卖）
            if i in s_idx:
                s = s_idx[i]
                entry_price = s['price']
                stop_price = (entry_price + config['stop_atr_mult'] * atr[i]
                              if config['use_stop'] else 1e9)
                pos = {'entry_idx': i, 'entry_price': entry_price,
                       'entry_reason': s.get('reason', ''),
                       'stop_price': stop_price, 'min_fav': entry_price}
            continue

        can_buy = (locked_up is None) or (not bool(locked_up[i]))
        # 1) 硬止损（反T向上）
        if config['use_stop']:
            if config['stop_mode'] == 'trend':
                if trend is not None and trend[i] == 1 and can_buy:
                    trips.append(_mk_trip(pos, i, c[i], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue
            else:
                if h is not None and h[i] >= pos['stop_price'] and can_buy:
                    trips.append(_mk_trip(pos, i, pos['stop_price'], 'STOP', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue
        # 更新浮盈低点
        if c[i] < pos['min_fav']:
            pos['min_fav'] = c[i]
        # 2) B信号出场（反T自然平仓）
        if config['s_signal_exit'] and i in b_idx and can_buy:
            trips.append(_mk_trip(pos, i, b_idx[i]['price'], 'B', buy_cost, sell_cost, entry_date=day_date))
            pos = None
            continue
        # 3) 移动止损（反T：浮盈下跌后回升触发）
        if config['use_trailing']:
            fav_ret = (pos['entry_price'] - pos['min_fav']) / pos['entry_price'] * 100
            if fav_ret >= config['trail_activate_pct']:
                trail_stop = pos['min_fav'] * (1 + config['trail_pct'] / 100.0)
                if c[i] >= trail_stop and trail_stop < pos['stop_price'] and can_buy:
                    trips.append(_mk_trip(pos, i, c[i], 'TRAIL', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue
        # 4) 时间止损
        if config['use_time'] and (i - pos['entry_idx']) >= config['time_stop_bars'] and can_buy:
            trips.append(_mk_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost, entry_date=day_date))
            pos = None
            continue

    # EOD 强平
    if pos is not None:
        trips.append(_mk_trip(pos, n - 1, c[n - 1], 'EOD', buy_cost, sell_cost, entry_date=day_date))
    for t in trips:
        t['side'] = 'S'   # 反T标记
        # 反T收益方向翻转：先卖后买，赚 = (entry−exit)/entry。
        # _mk_trip 按正T算 gross=(exit−entry)/entry，取负即反T毛收益；净收益同样翻转。
        t['gross_ret_pct'] = round(-float(t['gross_ret_pct']), 3)
        t['ret_pct'] = round(-float(t['ret_pct']), 3)
    return trips


def simulate_dual(signals, prices, config=None, cost=None):
    """双向做T：正T（B→S）+ 反T（S→B）分别配对，返回 (long_trips, short_trips)。
    long_trips 由 simulate_day 产出（side='B'），short_trips 由 simulate_bidirectional 产出。
    """
    from exit_manager import simulate_day
    long_trips = simulate_day(signals, prices, config=config, cost=cost)
    for t in long_trips:
        t.setdefault('side', 'B')
    short_trips = simulate_bidirectional(signals, prices, config=config, cost=cost)
    return long_trips, short_trips
