# -*- coding: utf-8 -*-
"""
core/simulate_base_position.py —— 底仓模型（合规回测，修复裸卖空缺陷）

对账发现的致命缺陷：simulate_bidirectional 允许「无底仓先 S」（裸卖空），在下跌行情
免费做空，虚高反T 收益（+238%/+502%），虚低正T（-243%/-802%）。

本模块实现正确的「底仓模型」：
  1. 首日无仓：只能 B 买（建立底仓），持仓过夜（T+1 隔夜）。
  2. 次日及以后：持有底仓，才允许：
     - 正T（B→S 日内 round-trip，先买后卖）
     - 反T（S→B 日内 round-trip，先卖底仓后买回）——前提 base > 0
  3. 真实盈亏分两栏：
     - 做T盈亏 = 日内 round-trip 差价（正T + 反T），是「做T能力」的公正度量
     - 持仓盈亏 = 底仓隔夜涨跌（长期持有底仓的 beta，非做T能力）

返回：
  long_ret  : 正T round-trip 净收益列表（%）
  short_ret : 反T round-trip 净收益列表（%）
  hold_ret  : 底仓隔夜涨跌列表（%）
"""
import numpy as np

from exit_manager import simulate_day
from simulate_bidirectional import simulate_bidirectional


def simulate_base_position(sigs_by_day, prices_by_day, config=None, cost=None):
    """底仓模型跨日模拟。

    参数：
      sigs_by_day  : list of (date, sigs) 按日期升序
      prices_by_day: list of (date, prices_dict) 按日期升序，与 sigs_by_day 对齐
      config       : 正T出场配置（反T 由 simulate_bidirectional 内部复用）
      cost         : 双边成本率
    返回：
      dict(long_ret, short_ret, hold_ret, base_cost, n_days)
    """
    long_ret, short_ret, hold_ret = [], [], []
    base = 0            # 底仓份额（1 = 持有 1 份）
    base_cost = None    # 底仓成本价（首日 B 建仓价）
    prev_close = None

    for idx, (date, sigs, prices) in enumerate(sigs_by_day):
        n = prices['n']
        c = prices['c']
        o = prices['o']
        if len(c) < 20:
            continue
        # —— 底仓隔夜盈亏（上一日持仓 → 今日本日开盘）——
        if base > 0 and prev_close is not None and prev_close > 0:
            hold_ret.append((o[0] - prev_close) / prev_close)

        if base == 0:
            # 首日无仓：只能 B 建底仓（找第一个 B 信号），不做日内做T
            b_sigs = [s for s in sigs if s['type'] == 'B']
            if b_sigs:
                base = 1
                base_cost = float(b_sigs[0]['price'])
            # 首日建仓后不再做 T（当日底仓锁定）
        else:
            # 持有底仓：正T + 反T 日内 round-trip（收盘恢复底仓）
            long_ret.extend(t['ret_pct'] for t in simulate_day(sigs, prices, config, cost))
            short_ret.extend(t['ret_pct'] for t in simulate_bidirectional(sigs, prices, config, cost))

        prev_close = float(c[-1])

    return dict(
        long_ret=long_ret, short_ret=short_ret, hold_ret=hold_ret,
        base_cost=base_cost, n_days=len(sigs_by_day),
    )


def agg_rets(rets):
    """聚合收益列表 → (n, wr, net)。"""
    rets = [float(x) for x in rets if x is not None]
    if not rets:
        return dict(n=0, wr=None, net=None)
    wins = sum(1 for x in rets if x > 0)
    return dict(n=len(rets), wr=100.0 * wins / len(rets), net=sum(rets))
