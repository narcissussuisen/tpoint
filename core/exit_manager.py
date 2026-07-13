"""v9 出场管理模块 (Execution / Exit Management Layer)

叠加在 indicators 的 B/S 信号之上, 管理"从B建仓到最终平仓"的全过程:
  - 硬止损 (ATR-based): B信号错(价格继续破位下行)时兜住, 把大亏变小亏
  - 时间止损: 持仓超过阈值(分钟)仍无S信号, 强制平仓, 释放资金
  - 移动止损: 浮盈后回撤锁定利润, 让利润奔跑但保护
  - S信号出场: 原有的自然出场(仍保留, 作为出场源之一)

★ 概念澄清: 出场管理 ≠ S信号提示
  S信号只是一个"建议出场"的触发器(提醒你该考虑卖了);
  出场管理是触发之后的执行纪律层, 覆盖止损/时间/移动等多种出场路径,
  还管"S发错时怎么办"。目标是把B信号盈亏比从 1.05:1 提至 1.6:1(最大杠杆点)。

本模块与数据源/STATE无关, 纯算法, 可被 monitor(实盘) / 回测共用。
"""
import numpy as np


# ========== 出场配置 ==========

def make_config(use_stop=True, stop_atr_mult=1.5, stop_mode='atr',
                use_time=True, time_stop_bars=90,
                use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
                s_signal_exit=True):
    """构造出场配置。所有开关独立, 便于做消融实验(看哪个规则贡献最大)。

    参数说明:
      use_stop        是否启用硬止损
      stop_atr_mult   止损距离 = 入场价 - stop_atr_mult * ATR(入场bar), ATR自适应波动
      stop_mode       'atr'  : 盘中最低价触及 stop_price 即止损(紧, 对均值回归易噪音止损)
                      'trend': 仅当趋势确认翻空(trend==-1)才止损(宽, 只在"升势判错/破位"时出场)
                                → 均值回归抄下影线策略的正确止损方式, 不被正常下探洗掉
      use_time        是否启用时间止损
      time_stop_bars  持仓超过多少根(分钟)无出场则强平
      use_trailing    是否启用移动止损
      trail_activate_pct  浮盈≥该百分比才激活移动止损(避免噪音触发)
      trail_pct       从浮动高点回撤该百分比触发移动止损
      s_signal_exit   是否把S信号作为出场源(默认开, 即原v9自然出场保留)
    """
    return {
        'use_stop': use_stop,
        'stop_atr_mult': stop_atr_mult,
        'stop_mode': stop_mode,
        'use_time': use_time,
        'time_stop_bars': time_stop_bars,
        'use_trailing': use_trailing,
        'trail_activate_pct': trail_activate_pct,
        'trail_pct': trail_pct,
        's_signal_exit': s_signal_exit,
    }


# ========== 单日正向T配对模拟 ==========

def simulate_day(signals, prices, config):
    """对单日信号做正向T(先买后卖)配对模拟, 应用出场管理规则。

    参数:
      signals: detect_signals 输出的当日信号列表(含 type/idx/price)
      prices:  dict, 必须含 'o','h','lo','c' 数组 与 'atr' 数组, 以及 'n'
      config:  make_config() 输出
    返回: round_trips 列表, 每条含
      entry_idx, exit_idx, entry_price, exit_price, exit_reason,
      ret_pct, hold_bars, entry_reason(该B的触发原因)
    """
    n = prices['n']
    c = prices['c']; lo = prices['lo']; atr = prices['atr']
    trend = prices.get('trend')
    # 信号按idx建索引
    b_idx = {s['idx']: s for s in signals if s['type'] == 'B'}
    s_idx = {s['idx']: s for s in signals if s['type'] == 'S'}

    trips = []
    pos = None  # 持仓状态: entry_idx/entry_price/entry_reason/stop_price/max_fav
    for i in range(2, n):
        if pos is None:
            # 空仓 → 找B建仓(单仓位模型, B持仓中忽略新B)
            if i in b_idx:
                b = b_idx[i]
                entry_price = b['price']
                stop_price = (entry_price - config['stop_atr_mult'] * atr[i]
                              if config['use_stop'] else -1e9)
                pos = {'entry_idx': i, 'entry_price': entry_price,
                       'entry_reason': b.get('reason', ''),
                       'stop_price': stop_price, 'max_fav': entry_price}
            continue

        # ---- 持仓中, 检查出场(优先级: 硬止损 > S信号 > 移动止损 > 时间止损) ----
        # 1) 硬止损(风险兜底, 最高优先)
        if config['use_stop']:
            if config['stop_mode'] == 'trend':
                # 趋势破位止损: 仅当趋势确认翻空(trend==-1)才出场, 不被正常下探洗掉
                if trend is not None and trend[i] == -1:
                    trips.append(_mk_trip(pos, i, c[i], 'STOP'))
                    pos = None
                    continue
            else:
                # ATR噪音止损: 盘中最低价触及 stop_price 即出
                if lo[i] <= pos['stop_price']:
                    trips.append(_mk_trip(pos, i, pos['stop_price'], 'STOP'))
                    pos = None
                    continue
        # 更新浮动盈利高点
        if c[i] > pos['max_fav']:
            pos['max_fav'] = c[i]
        # 2) S信号出场(原v9自然出场)
        if config['s_signal_exit'] and i in s_idx:
            trips.append(_mk_trip(pos, i, s_idx[i]['price'], 'S'))
            pos = None
            continue
        # 3) 移动止损(浮盈保护)
        if config['use_trailing']:
            fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
            if fav_ret >= config['trail_activate_pct']:
                trail_stop = pos['max_fav'] * (1 - config['trail_pct'] / 100.0)
                if c[i] <= trail_stop and trail_stop > pos['stop_price']:
                    trips.append(_mk_trip(pos, i, c[i], 'TRAIL'))
                    pos = None
                    continue
        # 4) 时间止损(超时强平)
        if config['use_time'] and (i - pos['entry_idx']) >= config['time_stop_bars']:
            trips.append(_mk_trip(pos, i, c[i], 'TIME'))
            pos = None
            continue

    # 收盘仍未平仓 → 强平(EOD)
    if pos is not None:
        trips.append(_mk_trip(pos, n - 1, c[n - 1], 'EOD'))
    return trips


def _mk_trip(pos, exit_idx, exit_price, reason):
    entry_price = pos['entry_price']
    ret = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    return {
        'entry_idx': pos['entry_idx'],
        'exit_idx': int(exit_idx),
        'entry_price': round(float(entry_price), 2),
        'exit_price': round(float(exit_price), 2),
        'exit_reason': reason,
        'ret_pct': round(float(ret), 3),
        'hold_bars': int(exit_idx - pos['entry_idx']),
        'entry_reason': pos.get('entry_reason', ''),
    }


# ========== 聚合指标 ==========

def aggregate_metrics(trips):
    """汇总 round_trips 指标: 笔数/胜率/均盈/均亏/盈亏比/总收益/平均持仓/各出场占比"""
    if not trips:
        return {'total': 0, 'win_rate': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
                'pl_ratio': 0.0, 'total_ret': 0.0, 'cum_nav': 1.0,
                'avg_hold': 0, 'by_reason': {}}
    rets = np.array([t['ret_pct'] for t in trips], dtype=float)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float('inf')
    # 复利净值(每笔等权投入)
    cum_nav = float(np.prod(1.0 + rets / 100.0))
    by_reason = {}
    for t in trips:
        by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1
    return {
        'total': len(trips),
        'win_rate': round(len(wins) / len(trips) * 100, 1),
        'avg_win': round(avg_win, 3),
        'avg_loss': round(avg_loss, 3),
        'pl_ratio': round(pl_ratio, 2) if pl_ratio != float('inf') else 99.0,
        'total_ret': round(float(rets.sum()), 2),
        'cum_nav': round(cum_nav, 3),
        'avg_hold': round(float(np.mean([t['hold_bars'] for t in trips])), 1),
        'by_reason': by_reason,
    }
