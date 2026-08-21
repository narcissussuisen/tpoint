# -*- coding: utf-8 -*-
"""core/exit_v3.py — 三条件止损出场（R4 / T0T 借鉴，2026-08-20 新增）

对齐 toasty-cascade-tesla.md §4.1「重构 exit_manager：exit_v3 三条件止损」：
  1. **硬止损**：反向波动达 max(1.2×ATR%, 0.8%) 取严 → 立即止损（大亏变小亏）
  2. **趋势止损**：VWAP 反穿 + MACD 同向确认 → 趋势证伪即出（不被正常回踩洗掉）
  3. **时间止损**：60 根无新高（反T：无新低）→ 无进展强平（释放资金）
  + 信号出场（B/S 自然平仓）+ EOD 强平

支持方向：
  - direction='long'  （正T：B 建仓先买后卖，赚 = 上涨）
  - direction='short' （反T：S 建仓先卖后买，赚 = 下跌）

输入 prices dict 须含 o/h/lo/c/vwap/atr/hist(MACD)/n；与 build_data 输出兼容。
返回 trips 与 simulate_day 同构（ret_pct 净收益，side 标记，short 方向收益翻转）。
"""
import numpy as np

from exit_manager import make_config, _mk_trip  # noqa: F401


def _vwap_cross(data, i, direction):
    """VWAP 反穿检测。long: close 下穿 vwap（c[i]<=vwap 且前 bar >vwap）；short 反之。"""
    vwap = data['vwap']
    c = data['c']
    if i < 1 or vwap is None or vwap[i] is None:
        return False
    try:
        if vwap[i] <= 0:
            return False
    except Exception:
        return False
    if direction == 'long':
        return c[i] <= vwap[i] and c[i - 1] > vwap[i - 1]
    return c[i] >= vwap[i] and c[i - 1] < vwap[i - 1]


def _macd_same_dir(data, i, direction):
    """MACD hist 同向确认。long: hist<0（动能转空）；short: hist>0（动能转多）。"""
    hist = data.get('hist')
    if hist is None:
        return False
    if direction == 'long':
        return float(hist[i]) < 0
    return float(hist[i]) > 0


def exit_v3(signals, prices, direction='long', cost=None,
            stop_atr_mult=1.2, stop_fixed_pct=0.8, time_stop_bars=60,
            s_signal_exit=True, trend_exit=True, use_hard_stop=True):
    """三条件止损出场模拟（正T long / 反T short 通用）。

    参数：
      signals: detect 输出信号（type/idx/price）
      prices: dict（含 o/h/lo/c/vwap/atr/hist/n/date/pc/sym）
      direction: 'long'=正T(B→S) / 'short'=反T(S→B)
      stop_atr_mult / stop_fixed_pct: 硬止损 = max(1.2×ATR%, 0.8%) 取严
      time_stop_bars: 时间止损（无新高/新低根数）
      trend_exit: 启用 VWAP 反穿 + MACD 同向 趋势止损
      use_hard_stop: 启用硬止损
    """
    if cost is None:
        cost = (0.0, 0.0)
    buy_cost, sell_cost = cost
    n = prices['n']
    c = prices['c']; h = prices.get('h'); lo = prices['lo']; atr = prices['atr']
    day_date = prices.get('date')
    _pc = prices.get('pc'); _sym = prices.get('sym')
    # 锁停牌不可平：long 跌停 / short 涨停（镜像 simulate_day 的 locked_down）
    locked = None
    if _pc and _pc > 0 and _sym:
        from exit_manager import limit_thr
        thr = limit_thr(_sym)
        if direction == 'long':
            ld = round(float(_pc) * (1 - thr), 2)
            locked = h <= ld + 0.02 if h is not None else None
        else:
            lu = round(float(_pc) * (1 + thr), 2)
            locked = lo >= lu - 0.02

    entry_idx_map = {s['idx']: s for s in signals if s['type'] == ('B' if direction == 'long' else 'S')}
    exit_idx_map = {s['idx']: s for s in signals if s['type'] == ('S' if direction == 'long' else 'B')}

    trips = []
    pos = None
    for i in range(2, n):
        if pos is None:
            if i in entry_idx_map:
                e = entry_idx_map[i]
                ep = e['price']
                atr_pct = float(atr[i]) / ep * 100.0 if ep > 0 and atr[i] > 0 else 0.0
                stop_dist = max(stop_atr_mult * atr_pct, stop_fixed_pct)  # %
                pos = {'entry_idx': i, 'entry_price': ep,
                       'entry_reason': e.get('reason', ''),
                       'stop_pct': stop_dist,
                       'extreme': ep}  # long: max_fav / short: min_fav
            continue

        can_exit = True
        if locked is not None:
            can_exit = not bool(locked[i]) if direction == 'long' else not bool(locked[i])

        # 1) 硬止损
        if use_hard_stop and can_exit:
            if direction == 'long':
                stop_px = pos['entry_price'] * (1 - pos['stop_pct'] / 100.0)
                if lo[i] <= stop_px:
                    trips.append(_mk_trip(pos, i, stop_px, 'HARD', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue
            else:
                stop_px = pos['entry_price'] * (1 + pos['stop_pct'] / 100.0)
                if h is not None and h[i] >= stop_px:
                    trips.append(_mk_trip(pos, i, stop_px, 'HARD', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue

        # 更新极值（long 高点 / short 低点）
        if direction == 'long':
            if c[i] > pos['extreme']:
                pos['extreme'] = c[i]
        else:
            if c[i] < pos['extreme']:
                pos['extreme'] = c[i]

        # 2) 趋势止损（VWAP 反穿 + MACD 同向）
        if trend_exit and can_exit:
            if _vwap_cross(prices, i, direction) and _macd_same_dir(prices, i, direction):
                trips.append(_mk_trip(pos, i, c[i], 'TREND', buy_cost, sell_cost, entry_date=day_date))
                pos = None
                continue

        # 3) 信号出场（long: S / short: B 自然平仓）
        if s_signal_exit and i in exit_idx_map and can_exit:
            trips.append(_mk_trip(pos, i, exit_idx_map[i]['price'], 'SIG',
                                  buy_cost, sell_cost, entry_date=day_date))
            pos = None
            continue

        # 4) 时间止损（无新高/新低）
        if can_exit and (i - pos['entry_idx']) >= time_stop_bars:
            # 最近 time_stop_bars 内极值未刷新 → 无进展
            win_start = pos['entry_idx']
            if direction == 'long':
                no_new_high = pos['extreme'] <= pos['entry_price'] * 1.0001
                # 更严格：60 根窗口内 high 未创新高于 entry 后高点
                seg_high = float(np.max(c[win_start:i + 1])) if i > win_start else pos['entry_price']
                no_new_high = seg_high <= pos['entry_price'] * 1.0001
                if no_new_high:
                    trips.append(_mk_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue
            else:
                seg_low = float(np.min(c[win_start:i + 1])) if i > win_start else pos['entry_price']
                no_new_low = seg_low >= pos['entry_price'] * 0.9999
                if no_new_low:
                    trips.append(_mk_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost, entry_date=day_date))
                    pos = None
                    continue

    # EOD 强平
    if pos is not None:
        trips.append(_mk_trip(pos, n - 1, c[n - 1], 'EOD', buy_cost, sell_cost, entry_date=day_date))

    for t in trips:
        t['side'] = 'S' if direction == 'short' else 'B'
        if direction == 'short':
            # 反T收益翻转：赚 = (entry−exit)/entry
            t['gross_ret_pct'] = round(-float(t['gross_ret_pct']), 3)
            t['ret_pct'] = round(-float(t['ret_pct']), 3)
    return trips


def simulate_v3_dual(signals, prices, cost=None, **kw):
    """双向 + v3 三条件止损：返回 (long_trips, short_trips)。"""
    long_trips = exit_v3(signals, prices, direction='long', cost=cost, **kw)
    short_trips = exit_v3(signals, prices, direction='short', cost=cost, **kw)
    return long_trips, short_trips
