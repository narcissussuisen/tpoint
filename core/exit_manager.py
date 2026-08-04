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


# ========== 成本模型 ==========

def make_cost_model(commission_rate=0.0001, stamp_duty=0.0005641, slippage_bps=2.0):
    """构造做T双边成本模型（2026-08-01 加入，胜率口径修复）。

    胜率必须扣除成本，否则"裸价差>0"定义的胜率虚高：
      688146 裸胜率 54.9% 中，23.4% 正收益 < 0.2%，扣成本后翻负。

    参数（用户实际费率 2026-08-01 提供）：
      commission_rate : 佣金单边费率。**沪深股票/ETF/可转债/债券现券/港股通：
                        万一(0.0001)，不免五**（此前默认万1.3 高估了成本）。
                        **北交所：千分之0.575 (0.000575)**，调用时显式传北交所费率。
      stamp_duty      : 印花税单边（卖出万5.641 = 0.05641%）。仅沪深股票适用；
                        ETF/LOF/可转债/债券现券/港股通/北交所无印花税 → 调用时传 0
      slippage_bps    : 滑点（bps，每边），默认 2bps=0.02%，双边 0.04%。
                        信号触发价≠成交价：买=信号价上滑、卖=信号价下滑。
    返回 (buy_cost_pct, sell_cost_pct) 各边成本率(%)
    """
    slip_pct = slippage_bps / 100.0  # bps→%
    buy = commission_rate * 100 + slip_pct
    sell = commission_rate * 100 + stamp_duty * 100 + slip_pct
    return buy, sell


# 个股默认：万一佣金 + 卖出印花税 + 滑点 ≈ 双边 0.11%
DEFAULT_COST = make_cost_model()
# ETF/LOF/可转债/债券现券：万一佣金 + 无印花税 + 滑点 ≈ 双边 0.06%
DEFAULT_COST_NO_STAMP = make_cost_model(stamp_duty=0.0)
# 北交所：千分之0.575 佣金 + 无印花税 + 滑点（2026-08-01 用户确认费率）
DEFAULT_COST_BSE = make_cost_model(commission_rate=0.000575, stamp_duty=0.0)


def cost_for_symbol(sym):
    """按标的代码自动选择成本模型（2026-08-01，2026-08-01 补 920 段）。
    规则：
      - 北交所（4xx/8xx/920xxx 或 .BJ 后缀）→ 千分之0.575 佣金 + 无印花税（DEFAULT_COST_BSE）
      - 1xx/5xx（LOF/ETF/基金）→ 无印花税（DEFAULT_COST_NO_STAMP）
      - 其余（沪深个股）→ 万一佣金 + 印花税（DEFAULT_COST）
    """
    s = str(sym)
    code = s.split('.')[0]
    exch = s.split('.')[-1].upper() if '.' in s else ''
    if code.startswith(('4', '8')) or code.startswith('920') or exch == 'BJ':
        return DEFAULT_COST_BSE
    if code.startswith(('1', '5')):
        return DEFAULT_COST_NO_STAMP
    return DEFAULT_COST

# ========== 单日正向T配对模拟 ==========

def simulate_day(signals, prices, config, cost=None):
    """对单日信号做正向T(先买后卖)配对模拟, 应用出场管理规则。

    参数:
      signals: detect_signals 输出的当日信号列表(含 type/idx/price)
      prices:  dict, 必须含 'o','h','lo','c' 数组 与 'atr' 数组, 以及 'n'
      config:  make_config() 输出
      cost:    (buy_cost_pct, sell_cost_pct) 双边成本率(%)。默认 DEFAULT_COST。
               ret_pct 为扣除双边成本后的净收益（胜率口径修复 2026-08-01）。
    返回: round_trips 列表, 每条含
      entry_idx, exit_idx, entry_price, exit_price, exit_reason,
      ret_pct(净), gross_ret_pct(毛), hold_bars, entry_reason(该B的触发原因)
    """
    if cost is None:
        cost = DEFAULT_COST
    buy_cost, sell_cost = cost
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
                    trips.append(_mk_trip(pos, i, c[i], 'STOP', buy_cost, sell_cost))
                    pos = None
                    continue
            else:
                # ATR噪音止损: 盘中最低价触及 stop_price 即出
                if lo[i] <= pos['stop_price']:
                    trips.append(_mk_trip(pos, i, pos['stop_price'], 'STOP', buy_cost, sell_cost))
                    pos = None
                    continue
        # 更新浮动盈利高点
        if c[i] > pos['max_fav']:
            pos['max_fav'] = c[i]
        # 2) S信号出场(原v9自然出场)
        if config['s_signal_exit'] and i in s_idx:
            trips.append(_mk_trip(pos, i, s_idx[i]['price'], 'S', buy_cost, sell_cost))
            pos = None
            continue
        # 3) 移动止损(浮盈保护)
        if config['use_trailing']:
            fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
            if fav_ret >= config['trail_activate_pct']:
                trail_stop = pos['max_fav'] * (1 - config['trail_pct'] / 100.0)
                if c[i] <= trail_stop and trail_stop > pos['stop_price']:
                    trips.append(_mk_trip(pos, i, c[i], 'TRAIL', buy_cost, sell_cost))
                    pos = None
                    continue
        # 4) 时间止损(超时强平)
        if config['use_time'] and (i - pos['entry_idx']) >= config['time_stop_bars']:
            trips.append(_mk_trip(pos, i, c[i], 'TIME', buy_cost, sell_cost))
            pos = None
            continue

    # 收盘仍未平仓 → 强平(EOD)
    if pos is not None:
        trips.append(_mk_trip(pos, n - 1, c[n - 1], 'EOD', buy_cost, sell_cost))
    return trips


def _mk_trip(pos, exit_idx, exit_price, reason, buy_cost=0.0, sell_cost=0.0):
    entry_price = pos['entry_price']
    gross = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    # 净收益 = 毛收益 - 双边成本（买边成本在买入价基础上扣除，卖边在卖出价基础上扣除）
    net = gross - buy_cost - sell_cost
    return {
        'entry_idx': pos['entry_idx'],
        'exit_idx': int(exit_idx),
        'entry_price': round(float(entry_price), 2),
        'exit_price': round(float(exit_price), 2),
        'exit_reason': reason,
        'ret_pct': round(float(net), 3),
        'gross_ret_pct': round(float(gross), 3),
        'hold_bars': int(exit_idx - pos['entry_idx']),
        'entry_reason': pos.get('entry_reason', ''),
    }


# ========== 聚合指标 ==========

def aggregate_metrics(trips):
    """汇总 round_trips 指标: 笔数/胜率/均盈/均亏/盈亏比/总收益/平均持仓/各出场占比。

    [2026-08-01 胜率口径修复] ret_pct 已是扣除双边成本后的净收益：
      - win_rate  = 净收益 > 0 的比例（真实可复现胜率）
      - 新增 gross_win_rate = 毛收益 > 0 的比例（对比用，展示成本对胜率的侵蚀）
      - pl_ratio  基于净收益计算

    2026-07-31 迭代扩展（P0-3 绩效统计）：
    - max_drawdown_pct : 复利净值序列最大回撤(%)（等权每笔）
    - sharpe           : 每笔收益率年化夏普（默认一年 244 个交易日，每笔≈1日持仓近似）
    - ann_ret_pct      : 年化收益率(复利净值几何年化, 等权每笔日度近似)
    注意：tpoint 笔级口径无费用模型，与卡方 xlsx 口径（双边费用万3.5+万5.641）不可直接比较。
    """
    base = {'total': 0, 'win_rate': 0.0, 'gross_win_rate': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'pl_ratio': 0.0, 'total_ret': 0.0, 'cum_nav': 1.0,
            'max_drawdown_pct': 0.0, 'sharpe': 0.0, 'ann_ret_pct': 0.0,
            'avg_hold': 0, 'by_reason': {}}
    if not trips:
        return base
    rets = np.array([t['ret_pct'] for t in trips], dtype=float)
    gross_rets = np.array([t.get('gross_ret_pct', t['ret_pct']) for t in trips], dtype=float)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float('inf')
    # 复利净值(每笔等权投入)
    cum_nav = float(np.prod(1.0 + rets / 100.0))
    # [P0-3] 最大回撤：净值序列从峰值回落的最大幅度
    nav = np.cumprod(1.0 + rets / 100.0)
    peak = np.maximum.accumulate(nav)
    drawdown = (nav - peak) / peak * 100.0
    max_dd = float(-drawdown.min()) if len(drawdown) else 0.0
    # [P0-3] 夏普（每笔收益率年化近似，基准0）：mean/std * sqrt(244)
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(244))
    else:
        sharpe = 0.0
    # [P0-3] 年化收益率：cum_nav 折算到一年（近似每笔=1交易日）
    n_days = max(len(trips), 1)
    ann_ret = (cum_nav ** (244.0 / n_days) - 1.0) * 100.0
    by_reason = {}
    for t in trips:
        by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1
    return {
        'total': len(trips),
        'win_rate': round(len(wins) / len(trips) * 100, 1),
        'gross_win_rate': round(float((gross_rets > 0).mean() * 100), 1),
        'avg_win': round(avg_win, 3),
        'avg_loss': round(avg_loss, 3),
        'pl_ratio': round(pl_ratio, 2) if pl_ratio != float('inf') else 99.0,
        'total_ret': round(float(rets.sum()), 2),
        'cum_nav': round(cum_nav, 3),
        'max_drawdown_pct': round(max_dd, 2),
        'sharpe': round(sharpe, 2),
        'ann_ret_pct': round(ann_ret, 2),
        'avg_hold': round(float(np.mean([t['hold_bars'] for t in trips])), 1),
        'by_reason': by_reason,
    }
