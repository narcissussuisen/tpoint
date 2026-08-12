# -*- coding: utf-8 -*-
"""
performance_stats.py — 卡方风格绩效统计（P0-3 迭代交付）

对齐 kf_日内回转plus_performance_20260731.xlsx 的指标口径：
  20日收益率 / 20日开仓率 / 年化收益率 / 周期开仓率 / 当日收益率 /
  当日开仓率 / 5日开仓率 / 5日收益率 / 20日胜率 / 5日胜率 / Level 星级

输入：round_trips 列表（tpoint simulate_day / aggregate_metrics 的 trip 格式，
      每笔含 ret_pct/hold_bars/entry_idx/exit_idx/exit_reason）或 CSV。

设计原则：
- 纯函数、无副作用，供 daily_signal_review / build_review_html / market_screener 复用。
- 开仓率口径：trip 笔数 / (可用交易 bar 数) —— 与卡方"开仓率"（实际开仓日/可交易日）
  近似，tpoint 为分钟级做T，故 bar 口径用"信号触发率"标注，避免与卡方口径混淆。
- Level 星级：按 20 日年化收益分档（1星 <0% / 2星 0-10% / 3星 10-40% / 4星 40-100% / 5星 >100%），
  阈值从 xlsx 实测分布标定（1星-7.3%/2星2.6%/3星24.4%/4星41.3%/5星118.7% 中位）。
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.exit_manager import aggregate_metrics  # noqa: E402

TRADING_DAYS = 244  # 一年交易日数（A股近似）


def _annualize(cum_nav, n_periods):
    """复利净值年化（n_periods 为样本周期数）。cum_nav<=0 时返回 0。"""
    if cum_nav <= 0 or n_periods < 1:
        return 0.0
    return (cum_nav ** (TRADING_DAYS / n_periods) - 1.0) * 100.0


def _level_star(ann_ret):
    """按年化收益率分档 Level 星级（阈值从 xlsx 实测分布标定）。"""
    if ann_ret >= 100:
        return 5
    if ann_ret >= 40:
        return 4
    if ann_ret >= 10:
        return 3
    if ann_ret >= 0:
        return 2
    return 1


def _window_stats(trips, window_days):
    """滚动窗口统计：取 trips 中最近 window_days 天内的笔。
    trips 需含 entry_date 字段（'YYYY-MM-DD'），无则退化为全部笔。
    返回 (笔数, 胜率%, 累计收益率%, 开仓率%)。

    开仓率口径（v2，2026-07-31 迭代修正）：
      做T为分钟级多笔/日，卡方"开仓率"= 实际开仓交易日/可交易日。
      tpoint 近似：开仓率 = 有笔交易日数 / 窗口日历天数 × 100%，
      即"该窗口内多少比例的日子系统给出了至少一次信号"——
      与卡方口径同构（有信号=有开仓），不受每日笔数放大。
    """
    if not trips:
        return (0, 0.0, 0.0, 0.0)
    if 'entry_date' not in trips[0]:
        subset = trips
        n_days = None
    else:
        dates = sorted({t.get('entry_date', '') for t in trips if t.get('entry_date')})
        if not dates:
            subset = trips
            n_days = None
        else:
            cutoff = dates[-1] if window_days is None else None
            if window_days is not None:
                from datetime import datetime, timedelta
                ref = datetime.strptime(dates[-1], '%Y-%m-%d')
                cutoff_dt = ref - timedelta(days=window_days * 1.5)  # 日历日近似放宽
                cutoff = cutoff_dt.strftime('%Y-%m-%d')
            subset = [t for t in trips if t.get('entry_date', '') >= cutoff]
            n_days = len(dates)
    if not subset:
        return (0, 0.0, 0.0, 0.0)
    rets = [t['ret_pct'] for t in subset]
    wins = sum(1 for r in rets if r > 0)
    cum = 1.0
    for r in rets:
        cum *= (1.0 + r / 100.0)
    total_ret = (cum - 1.0) * 100.0
    win_rate = wins / len(subset) * 100.0
    # [v2] 开仓率 = 有笔交易日数 / 窗口天数（上限 100%）
    if n_days:
        active_days = len({t.get('entry_date', '') for t in subset})
        open_rate = min(active_days / n_days * 100.0, 100.0)
    else:
        open_rate = -1.0
    return (len(subset), round(win_rate, 1), round(total_ret, 2), round(open_rate, 1))


def kf_style_stats(trips):
    """卡方风格统计主入口。
    输入：trip 列表（建议含 entry_date），输出 17 列近似指标 dict。

    [轮次2-3 迭代] 样本量阈值：
    - n_trips < 20 时标注 sample_warning="小样本"（年化在 <20 笔数学放大失真，如 4 笔→509%）；
    - 20 ≤ n_trips < 60 时标注 sample_warning="样本偏小"（年化可信度一般）；
    - n_trips ≥ 60 标注 sample_warning=None（正常）。
    年化数值仍照常给出，但由调用方（复盘 HTML/报告）展示警告，避免误导。"""
    if not trips:
        return {'n_trips': 0, 'sample_warning': None}
    m = aggregate_metrics(trips)
    n = m['total']
    if n < 20:
        sample_warning = '小样本'
    elif n < 60:
        sample_warning = '样本偏小'
    else:
        sample_warning = None
    d5 = _window_stats(trips, 5)
    d20 = _window_stats(trips, 20)
    # 当日 = 最后 entry_date 的笔
    today_trips = []
    if 'entry_date' in trips[0]:
        last_date = max((t.get('entry_date', '') for t in trips if t.get('entry_date')), default='')
        today_trips = [t for t in trips if t.get('entry_date', '') == last_date]
    d1 = _window_stats(today_trips, None)
    ann = m['ann_ret_pct']
    return {
        'n_trips': n,
        'sample_warning': sample_warning,
        'win_rate_20d': d20[1],
        'win_rate_5d': d5[1],
        'ret_20d_pct': d20[2],
        'ret_5d_pct': d5[2],
        'ret_today_pct': d1[2],
        'open_rate_20d_pct': d20[3] if d20[3] >= 0 else None,
        'open_rate_5d_pct': d5[3] if d5[3] >= 0 else None,
        'open_rate_today_pct': d1[3] if d1[3] >= 0 else None,
        'ann_ret_pct': ann,
        'max_drawdown_pct': m['max_drawdown_pct'],
        'sharpe': m['sharpe'],
        'level_star': _level_star(ann),
        'pl_ratio': m['pl_ratio'],
        'cum_nav': m['cum_nav'],
    }


# ========== CLI：读 CSV → 输出统计 ==========
def _read_trips_csv(path):
    """读回测 trips CSV（列: entry_idx,exit_idx,entry_price,exit_price,exit_reason,ret_pct,hold_bars,entry_reason[,entry_date]）"""
    import csv
    trips = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                t = {
                    'entry_idx': int(row.get('entry_idx', 0)),
                    'exit_idx': int(row.get('exit_idx', 0)),
                    'entry_price': float(row.get('entry_price', 0)),
                    'exit_price': float(row.get('exit_price', 0)),
                    'exit_reason': row.get('exit_reason', ''),
                    'ret_pct': float(row.get('ret_pct', 0)),
                    'hold_bars': int(row.get('hold_bars', 0)),
                    'entry_reason': row.get('entry_reason', ''),
                }
                if row.get('entry_date'):
                    t['entry_date'] = row['entry_date']
                trips.append(t)
            except (TypeError, ValueError):
                continue
    return trips


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python scripts/performance_stats.py <trips.csv>')
        print('      读取回测 trips CSV 并输出卡方风格绩效统计')
        sys.exit(1)
    trips = _read_trips_csv(sys.argv[1])
    stats = kf_style_stats(trips)
    print(f'样本笔数: {stats.get("n_trips", 0)}')
    if stats.get('sample_warning'):
        print(f'⚠️ 样本量警告: {stats["sample_warning"]}（年化/夏普数值可能失真，仅供参考）')
    for k, v in stats.items():
        if k in ('n_trips', 'sample_warning'):
            continue
        print(f'  {k}: {v}')
