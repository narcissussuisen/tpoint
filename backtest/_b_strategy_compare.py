#!/usr/bin/env python3
"""
B信号逻辑对比 — 三种做T买点逻辑离线回测
基于 tickflow 落地的真实分钟数据(7标的×21日), 秒级可复现。
目标: 找到比当前「VWAP通道偏离回归」更优的B买点逻辑。
S信号逻辑保持当前不变(对照组), 只换B。

三种B逻辑:
  A. 回归型(当前v9): 上升趋势中价格跌破VWAP-K1*ATR下轨后回归
  B. VWAP动态支撑回踩: 上升趋势中回踩触及VWAP本身不破(收阳站回上方)+放量
  C. ORB开盘区间突破: 早盘(10:30前)放量突破开盘区间高点(动量跟随)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import numpy as np
import pandas as pd
from v9_indicators import compute_indicators, detect_signals, VOL_THRESHOLD, K1, K2

TARGETS = {
    '300975.SZ': '商络电子', '601869.SH': '长飞光纤', '603938.SH': '三孚股份',
    '300395.SZ': '菲利华', '301526.SZ': '国际复材',
    '300757.SZ': '罗博特科', '688820.SH': '盛合晶微',
}


def gen_b_regression(data, i, pc, day_df=None):
    """A. 回归型(完整复制生产版check_b_trigger的B逻辑, 无冷却)"""
    if data['atr'][i] <= 0:
        return False
    c = data['c']; o = data['o']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']
    vol_ratio = data['vol_ratio']; has_vol = data['has_vol']
    lower_std = vwap[i] - K1 * atr[i]
    lower_ext = vwap[i] - K2 * atr[i]
    is_yang = c[i] > o[i]
    lower_shadow = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
    if trend[i] != 1:
        return False
    if not (is_yang or lower_shadow >= 0.5 * atr[i]):
        return False
    triggered = False
    if (c[i - 1] <= lower_std or lo[i - 1] <= lower_std) and c[i] > lower_std:
        triggered = True
    elif lo[i] <= lower_ext and lower_shadow >= atr[i]:
        triggered = True
    if triggered and has_vol and vol_ratio[i] < VOL_THRESHOLD:
        return False
    return triggered


def gen_b_vwap_support(data, i, pc, day_df=None):
    """B. VWAP动态支撑回踩: 上升趋势中触及VWAP不破, 收阳站回上方, 放量"""
    if data['atr'][i] <= 0:
        return False
    c = data['c']; o = data['o']; lo = data['lo']
    vwap = data['vwap']; trend = data['trend']
    vol_ratio = data['vol_ratio']
    if trend[i] != 1:
        return False
    if vol_ratio[i] < VOL_THRESHOLD:
        return False
    # 触及VWAP(下影或实体触到), 但收在VWAP上方(支撑有效), 收阳
    touch = lo[i] <= vwap[i] * 1.003
    closed_above = c[i] > vwap[i]
    yang = c[i] > o[i]
    return touch and closed_above and yang


def gen_b_orb(data, i, pc, day_df):
    """C. ORB开盘区间突破: 早盘(10:30前)放量突破开盘后区间高点"""
    if data['atr'][i] <= 0:
        return False
    c = data['c']; o = data['o']
    h = data['h']; vol_ratio = data['vol_ratio']
    t = str(day_df['trade_time'].iloc[i])[11:16]
    if t >= '10:30':
        return False
    if vol_ratio[i] < VOL_THRESHOLD:
        return False
    # 开盘至当前的最高价(不含当前根)
    if i < 5:
        return False
    orb_high = np.max(h[max(0, i - 30):i])
    breakout = c[i] > orb_high * 1.001
    yang = c[i] > o[i]
    return breakout and yang


def eval_b(sym, name, bfunc):
    fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data', f'{sym}_1m.csv')
    if not os.path.exists(fpath):
        return []
    df = pd.read_csv(fpath)
    if len(df) < 240:
        return []
    df = df.sort_values('trade_time').reset_index(drop=True)
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['trade_time'].dt.strftime('%Y-%m-%d')
    sigs = []
    for date, day_df in df.groupby('date'):
        day_df = day_df.reset_index(drop=True)
        if len(day_df) < 60:
            continue
        o = day_df['open'].values.astype(float)
        h = day_df['high'].values.astype(float)
        lo = day_df['low'].values.astype(float)
        c = day_df['close'].values.astype(float)
        v = day_df['volume'].values.astype(float)
        pc = float(day_df['open'].iloc[0])
        data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
        n = len(day_df)
        for i in range(1, n):
            if bfunc(data, i, pc, day_df):
                f30 = min(i + 30, n - 1)
                c30 = float(c[f30])
                sigs.append({
                    'price': float(c[i]), 'c30': c30,
                    'ret_30': round((c30 - c[i]) / c[i] * 100, 2),
                    'hit_30': c30 > c[i],
                })
    return sigs


def summarize(label, all_sigs):
    if not all_sigs:
        return f"{label:<14} 信号=0"
    n = len(all_sigs)
    win = sum(s['hit_30'] for s in all_sigs)
    wr = win / n * 100
    avg_ret = np.mean([s['ret_30'] for s in all_sigs])
    wins = [s['ret_30'] for s in all_sigs if s['hit_30']]
    losses = [s['ret_30'] for s in all_sigs if not s['hit_30']]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 0
    pl = (avg_win / avg_loss) if avg_loss > 0 else float('inf')
    return (f"{label:<14} 信号={n:<4} T+30胜率={wr:5.1f}%  平均收益={avg_ret:+5.2f}%  "
            f"盈亏比={pl:.2f}:1")


def main():
    print("=" * 78)
    print("B信号逻辑对比 — 三种做T买点 (S保持当前逻辑不变)")
    print("=" * 78)
    for label, bfunc in [('A.回归型(当前)', gen_b_regression),
                         ('B.VWAP支撑', gen_b_vwap_support),
                         ('C.ORB突破', gen_b_orb)]:
        all_sigs = []
        for sym, name in TARGETS.items():
            all_sigs += eval_b(sym, name, bfunc)
        print(summarize(label, all_sigs))
    print()
    print("注: 当前v9锁定版B T+30=56.5%(量比2.0+仅上升). 以上为单B逻辑原始表现,")
    print("   未叠加S. 盈亏比=赢均收益/输均亏损绝对值.")


if __name__ == '__main__':
    main()
