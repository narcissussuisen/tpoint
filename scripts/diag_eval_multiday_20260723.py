#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_eval_multiday_20260723.py — tpoint v9 多日 OOS 门控对比
（让数据说话：floor / strict / pure / regime-adaptive 谁在跨行情下更稳）

与方法论：
- 同一生产引擎（miji）逐日回放，五种+一个参考门控各跑一遍：
    pure          : MACD_GATE_MODE='off'       纯引力(gravity-only)
    strict        : MACD_GATE_MODE='strict'    B需MACD底背离/S需顶背离
    floor         : MACD_GATE_MODE='floor'     当前生产(背离 或 价格地板/天花板)，无跌日B抑制(基线)
    floor_bsupp   : floor + 跌日B通道抑制(floor_suppress_buy_day_chg=-1.0，本次新增对称护栏)
    regime        : floor + 因果 intraday 体制检测(floor 之外下行体制禁开多)
    regime_naive  : floor + 朴素 trend==-1 禁开多(= 之前 measure①, 参考列)
- 行情体制识别 regime_classify() 仅用 i 之前棒(因果, 无前视)：
    VWAP 漂移率 + 收盘在VWAP上占比 + OLS斜率/R² + 方差比(VR) 融合，
    加 debounce(最小停留) 防假切换。阈值为首发启发式, 未经调参(防过拟合声明)。
- 数据源：mootdx 1m 端点硬上限 ~800 棒 → 仅近 3 完整交易日可用
    (07-21/07-22/07-23; 07-20 仅80棒半天已排除)。覆盖涨/跌/平混合。
- 出场：复制生产 EXIT_CFG 移动止损 + 反向信号平仓 + EOD 强制平仓(诊断假设, 真实无EOD强平)。
- 注意：华虹 T+1, 日内同开同平不现实; 本诊断逐日独立+ EOD 强平, 仅相对对比有效。

输出：
  output/diag_eval_multiday_20260723.csv   每(日,标,模式) P&L/胜率/笔数
  output/diag_eval_multiday_regime_20260723.csv  每(模式,体制@B入场) 笔数/盈亏
  output/diag_eval_multiday_20260723.txt   人类可读汇总
  output/diag_eval_multiday_20260723.png   累计P&L曲线 + 体制分布
"""
import os
import sys
import json

# 复刻生产门控默认（run_monitor.bat 设 floor）；逐调用覆盖 macd_gate_mode
os.environ['MACD_GATE_MODE'] = 'floor'

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd
from core import miji_alpha as miji
from core.datasource import MootdxDataSource

SYMS = [
    ('161129.SZ', '原油LOF'),
    ('688347.SH', '华虹半导体'),
    ('513310.SH', '中韩半导体ETF'),
]
# 仅完整日（排除 07-20：仅 80 棒半天，800棒窗口截断）
DAYS = ['2026-07-21', '2026-07-22', '2026-07-23']
MODES = ['pure', 'strict', 'floor', 'floor_bsupp', 'regime', 'regime_naive']
MODE_LABEL = {
    'pure': 'pure(纯引力)',
    'strict': 'strict',
    'floor': 'floor(当前·无跌日抑制)',
    'floor_bsupp': 'floor_bsupp(跌日B抑制)',
    'regime': 'regime(体制自适应)',
    'regime_naive': 'regime_naive(朴素trend禁B)',
}
GATE = {'pure': 'off', 'strict': 'strict', 'floor': 'floor', 'floor_bsupp': 'floor',
        'regime': 'floor', 'regime_naive': 'floor'}
# 跌日B通道抑制开关：仅 floor_bsupp 开启(-1.0%)；floor 显式关(0.0)作基线，
# regime/regime_naive 用各自 trend 机制(不叠加 day_chg 抑制)以隔离变量。
SUPPRESS = {m: 0.0 for m in MODES}
SUPPRESS['floor_bsupp'] = -1.0

# 复刻 monitor 常量
COLDOWN_BARS = 3          # 注意：生产用 SIGNAL_GAP=8, 此处 diag 简化; 各模式一致故相对公平
MAX_B_DAILY = 12
MAX_S_DAILY = 12
MAX_SIZE_PCT = 8
EXIT_CFG = dict(use_stop=False, use_time=False, use_trailing=True,
                trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
REGIME_W = 30             # 体制识别窗口(棒)
REGIME_MIN_STAY = 12      # 体制最小停留(防假切换)

ds = MootdxDataSource()


def strength_size(g_dev_pct, m_present):
    strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
    return 4 if strong else 2


def get_pc(sym, day):
    d = ds.get(sym, period='1d', count=60, as_dataframe=True)
    if d is None or len(d) == 0:
        return None
    d = d.sort_values('trade_date').reset_index(drop=True)
    idx = d.index[d['trade_date'] == day]
    if len(idx) == 0:
        return float(d['close'].iloc[-1])
    i = idx[0]
    if i == 0:
        return float(d['close'].iloc[0])
    return float(d['close'].iloc[i - 1])


def fetch_day(sym, day):
    today = pd.Timestamp.now().strftime('%Y-%m-%d')
    if day == today:
        df = ds.klines.intraday(sym, as_dataframe=True)
    else:
        df = ds.klines.historical_1m(sym, day, offset=2000)
    if df is None or len(df) == 0:
        return None, None
    df = df.sort_values('trade_time').reset_index(drop=True)
    pc = get_pc(sym, day)
    return df, pc


def build_data(df, pc):
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df else None
    has_vol = v is not None and np.sum(v) > 0
    data = miji.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data


def regime_classify(data, i, W=REGIME_W):
    """因果 intraday 体制识别（仅用 i 及之前棒）。返回 up/down/range。

    首发启发式（未调参）：VWAP漂移 + 收盘>VWAP占比 + OLS斜率/R² + 方差比(VR)。
    VR>1 = 持续(趋势), VR<0.85 = 均值回复(震荡); 方向由漂移/占比/斜率共同定。
    """
    c = data['c']; vwap = data['vwap']; atr = data['atr']
    lo0 = max(0, i - W)
    seg_c = c[lo0:i + 1]
    seg_v = vwap[lo0:i + 1]
    if len(seg_c) < max(8, int(W * 0.5)):
        return 'range'
    v0 = seg_v[0]
    drift = (seg_v[-1] - v0) / v0 * 100.0 if v0 > 0 else 0.0
    above = float(np.mean(seg_c > seg_v))
    x = np.arange(len(seg_c), dtype=float)
    y = seg_c
    if np.std(y) > 1e-12:
        slope, intercept = np.polyfit(x, y, 1)
        yhat = slope * x + intercept
        ss_res = np.sum((y - yhat) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        slope_pct = slope / np.mean(y) * 100.0 * len(seg_c)  # 窗口总漂移%
    else:
        slope_pct = 0.0
        r2 = 0.0
    ret = np.diff(seg_c)
    if len(ret) > 5 and np.var(ret) > 0:
        q = 5
        cum = seg_c[q:] - seg_c[:-q]
        vr = np.var(cum) / (q * np.var(ret))
    else:
        vr = 1.0

    # 方向判定
    direction = 'range'
    if drift > 0.25 and above > 0.50:
        direction = 'up'
    elif drift < -0.25 and above < 0.50:
        direction = 'down'
    # 斜率方向一致性
    if slope_pct < 0 and direction == 'up':
        direction = 'range'
    if slope_pct > 0 and direction == 'down':
        direction = 'range'
    # 强均值回复 → 震荡
    if vr < 0.85:
        direction = 'range'
    # 漂移过弱 → 震荡
    if abs(drift) < 0.15:
        direction = 'range'
    return direction


def regime_array(data, n):
    """预计算逐棒体制(带 debounce)。"""
    arr = ['range'] * n
    state = 'range'
    dur = 0
    c = data['c']; atr = data['atr']
    for i in range(2, n):
        if atr[i] <= 0:
            arr[i] = state
            continue
        raw = regime_classify(data, i, W=REGIME_W)
        if raw == state:
            dur += 1
        elif raw != state and dur >= REGIME_MIN_STAY:
            state = raw
            dur = 0
        # raw != state 且 dur<MIN_STAY: 维持旧 state, dur 不变
        arr[i] = state
    return arr


def simulate(sym, name, data, mode, regime_arr=None, suppress=0.0):
    """回放单日单模式。返回 (events, closed, overnight, n, regime_arr)。"""
    c = data['c']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']
    trend = data['trend']; n = data['n']
    df = data['df']
    trade_times = df['trade_time'].values if df is not None else None
    gmode = GATE[mode]
    regime_overlay = (mode == 'regime')
    naive_overlay = (mode == 'regime_naive')

    st = {}
    pos = None
    events = []
    closed = []
    run_hi_max = -1e9
    for i in range(2, n):
        bar_key = f'bar_{sym}_{i}'
        if st.get(bar_key):
            continue
        if atr[i] <= 0:
            st[bar_key] = 1
            continue
        run_hi_max = max(run_hi_max, data['h'][i])
        near_limit_up = ((run_hi_max - data['pc']) / data['pc'] >= 0.20) if data['pc'] > 0 else False
        bt = str(trade_times[i])[11:16] if trade_times is not None and i < len(trade_times) else ''

        if pos is not None:
            side = pos['side']
            if side == 'long':
                if c[i] > pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            else:
                if c[i] < pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            exited = False
            if not exited and EXIT_CFG['s_signal_exit']:
                if side == 'long':
                    ts, rs = miji.check_s_trigger(data, i, macd_gate_mode=gmode)
                    if ts:
                        events.append(('X', 'S', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], rs))
                        closed.append(dict(side='long', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                           max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                           exit_reason='S', size=pos['size_pct']))
                        pos = None; exited = True
                else:
                    tb, rb = miji.check_b_trigger(data, i, macd_gate_mode=gmode)
                    if tb:
                        events.append(('X', 'B', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], rb))
                        closed.append(dict(side='short', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                           max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                           exit_reason='B', size=pos['size_pct']))
                        pos = None; exited = True
            if not exited and EXIT_CFG['use_trailing']:
                if side == 'long':
                    fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 - EXIT_CFG['trail_pct'] / 100.0)
                        if c[i] <= trail_stop:
                            events.append(('X', 'TRAIL', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], ''))
                            closed.append(dict(side='long', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                               max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                               exit_reason='TRAIL', size=pos['size_pct']))
                            pos = None; exited = True
                else:
                    fav_ret = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 + EXIT_CFG['trail_pct'] / 100.0)
                        if c[i] >= trail_stop:
                            events.append(('X', 'TRAIL', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], ''))
                            closed.append(dict(side='short', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                               max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                               exit_reason='TRAIL', size=pos['size_pct']))
                            pos = None; exited = True
            st[bar_key] = 1
            continue

        # 空仓：双向触发（跌日B通道抑制经 floor_suppress_buy_day_chg 透传）
        tb, rb = miji.check_b_trigger(data, i, macd_gate_mode=gmode,
                                      floor_suppress_buy_day_chg=suppress)
        ts, rs = miji.check_s_trigger(data, i, macd_gate_mode=gmode)
        if not (tb or ts):
            st[bar_key] = 1
            continue
        # 下行体制禁开多（regime / regime_naive 叠加）
        if tb and pos is None:
            ban = False
            if regime_overlay and regime_arr is not None and regime_arr[i] == 'down':
                ban = True
            if naive_overlay and trend[i] == -1:
                ban = True
            if ban:
                st[bar_key] = 1
                continue
        if tb:
            g_dev = (c[i] - vwap[i]) / vwap[i] * 100.0
            s_pct = strength_size(g_dev, 'MACD' in (rb or ''))
            if s_pct > 0 and (i - st.get(f'_cooldown_{sym}_B', -9999)) >= COLDOWN_BARS and st.get(f'_b_count_{sym}', 0) < MAX_B_DAILY:
                st[f'_cooldown_{sym}_B'] = i
                st[f'_b_count_{sym}'] = st.get(f'_b_count_{sym}', 0) + 1
                events.append(('B', '', i, bt, float(c[i]), float(c[i]), s_pct, rb))
                pos = {'side': 'long', 'entry_price': float(c[i]), 'entry_idx': i,
                       'max_fav': float(c[i]), 'size_pct': s_pct}
        if ts:
            g_dev = (c[i] - vwap[i]) / vwap[i] * 100.0
            s_pct = strength_size(g_dev, 'MACD' in (rs or ''))
            if s_pct > 0 and (i - st.get(f'_cooldown_{sym}_S', -9999)) >= COLDOWN_BARS and st.get(f'_s_count_{sym}', 0) < MAX_S_DAILY and not near_limit_up:
                st[f'_cooldown_{sym}_S'] = i
                st[f'_s_count_{sym}'] = st.get(f'_s_count_{sym}', 0) + 1
                events.append(('S', '', i, bt, float(c[i]), float(c[i]), s_pct, rs))
                pos = {'side': 'short', 'entry_price': float(c[i]), 'entry_idx': i,
                       'max_fav': float(c[i]), 'size_pct': s_pct}
        st[bar_key] = 1

    overnight = pos is not None
    if pos is not None:
        ei = n - 1
        ebt = str(trade_times[ei])[11:16] if trade_times is not None and ei < len(trade_times) else ''
        events.append(('X', 'EOD', ei, ebt, float(c[ei]), pos['entry_price'], pos['size_pct'], ''))
        closed.append(dict(side=pos['side'], entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                           max_fav=pos['max_fav'], exit_price=float(c[ei]), exit_idx=ei,
                           exit_reason='EOD', size=pos['size_pct']))
        pos = None
    return events, closed, overnight, n, regime_arr


def round_pnl(lp):
    if lp['side'] == 'long':
        return (lp['exit_price'] - lp['entry']) / lp['entry'] * 100.0
    else:
        return (lp['entry'] - lp['exit_price']) / lp['entry'] * 100.0


def main():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # CJK 字体
    cjk = r'C:/Windows/Fonts/simhei.ttf'
    if os.path.exists(cjk):
        font_manager.fontManager.addfont(cjk)
        plt.rcParams['font.family'] = 'SimHei'
    plt.rcParams['axes.unicode_minus'] = False

    print('=' * 72)
    print('tpoint v9 多日 OOS 门控对比  |  日: 07-21/07-22/07-23  |  3标 × 6模式')
    print('=' * 72)

    rows = []
    regime_rows = []   # (mode, regime_at_B_entry, n, pnl, win)
    equity = {m: [1.0] for m in MODES}   # 跨 symbol-day 累计净值(顺序: day-major)
    seq_labels = []
    # 体制分布统计（全样本逐棒）
    regime_bar_counts = {'up': 0, 'down': 0, 'range': 0}

    for day in DAYS:
        for sym, name in SYMS:
            seq_labels.append(f'{day[5:]}\n{name}')
            try:
                df, pc = fetch_day(sym, day)
            except Exception as e:
                print(f'  ⚠️ {day} {sym} 获取失败: {e}')
                for m in MODES:
                    equity[m].append(equity[m][-1])
                rows.append(dict(day=day, sym=sym, name=name, mode='ERR', n_rounds=0,
                                 n_win=0, win_rate=0.0, pnl_pct=0.0, avg_round=0.0))
                continue
            if df is None or len(df) < 60:
                print(f'  ⚠️ {day} {sym} 1m 不足60棒(仅{0 if df is None else len(df)})，跳过')
                for m in MODES:
                    equity[m].append(equity[m][-1])
                rows.append(dict(day=day, sym=sym, name=name, mode='ERR', n_rounds=0,
                                 n_win=0, win_rate=0.0, pnl_pct=0.0, avg_round=0.0))
                continue
            data = build_data(df, pc)
            n = data['n']
            rarr = regime_array(data, n)
            for rg in rarr:
                if rg in regime_bar_counts:
                    regime_bar_counts[rg] += 1
            print(f'\n########## {day} {sym} {name}  bar={n}  pc={pc:.3f} ##########')
            for m in MODES:
                events, closed, overnight, _, _ = simulate(sym, name, data, m, regime_arr=rarr,
                                                            suppress=SUPPRESS.get(m, 0.0))
                wins = sum(1 for lp in closed if round_pnl(lp) > 0)
                tot = sum(round_pnl(lp) for lp in closed)
                nr = len(closed)
                wr = wins / nr * 100 if nr else 0.0
                avg = tot / nr if nr else 0.0
                rows.append(dict(day=day, sym=sym, name=name, mode=m, n_rounds=nr,
                                 n_win=wins, win_rate=wr, pnl_pct=tot, avg_round=avg))
                equity[m].append(equity[m][-1] * (1 + tot / 100.0))
                # 体制@B入场 分布（仅 floor / regime / regime_naive 有可比性；floor 作基准）
                if m in ('floor', 'floor_bsupp', 'regime', 'regime_naive'):
                    long_entries = [lp for lp in closed if lp['side'] == 'long']
                    for lp in long_entries:
                        rg_entry = rarr[lp['entry_idx']] if lp['entry_idx'] < len(rarr) else 'range'
                        regime_rows.append(dict(mode=m, regime_at_b=rg_entry,
                                                pnl=round_pnl(lp), win=1 if round_pnl(lp) > 0 else 0))
                # 打印
                tag = MODE_LABEL[m]
                print(f'  {tag:22s} 笔={nr:2d} 胜={wins:2d} 胜率={wr:5.1f}%  P&L={tot:+7.2f}%  均/笔={avg:+.2f}%')

    # ---- 汇总：每模式聚合 ----
    print('\n' + '=' * 72)
    print('汇总（9 个 symbol-day 聚合）')
    print('=' * 72)
    summary = {}
    for m in MODES:
        mrows = [r for r in rows if r['mode'] == m]
        nr = sum(r['n_rounds'] for r in mrows)
        wins = sum(r['n_win'] for r in mrows)
        tot = sum(r['pnl_pct'] for r in mrows)
        wr = wins / nr * 100 if nr else 0.0
        eq = equity[m]
        eq_curve = np.array(eq[1:])  # 9 点
        peak = np.maximum.accumulate(eq_curve)
        dd = (eq_curve / peak - 1.0) * 100
        max_dd = dd.min()
        summary[m] = dict(n_rounds=nr, n_win=wins, win_rate=wr, sum_pnl=tot,
                          final_eq=eq_curve[-1] * 100, max_dd=max_dd,
                          avg_per_day=tot / len(DAYS))
        print(f'  {MODE_LABEL[m]:22s} 笔={nr:3d} 胜率={wr:5.1f}%  累计P&L={tot:+7.2f}%  '
              f'净值={eq_curve[-1]*100:6.1f}  最大回撤={max_dd:6.2f}%')

    # ---- 体制@B入场 盈亏 ----
    print('\n--- 体制@B入场 盈亏（floor 基准 vs regime 去除的下行B）---')
    regime_summary = {}
    for m in ('floor', 'regime', 'regime_naive'):
        for rg in ('up', 'down', 'range'):
            sub = [rr for rr in regime_rows if rr['mode'] == m and rr['regime_at_b'] == rg]
            if not sub:
                continue
            pn = sum(x['pnl'] for x in sub)
            wn = sum(x['win'] for x in sub)
            regime_summary[(m, rg)] = (len(sub), pn, wn / len(sub) * 100)
            print(f'  {MODE_LABEL[m]:22s} B@[{rg:5s}] 笔={len(sub):2d} 胜率={wn/len(sub)*100:5.1f}%  P&L={pn:+7.2f}%')

    # ---- 写 CSV ----
    out_dir = os.path.join(ROOT, 'output')
    os.makedirs(out_dir, exist_ok=True)
    df_rows = pd.DataFrame(rows)
    df_rows.to_csv(os.path.join(out_dir, 'diag_eval_multiday_20260723.csv'), index=False, encoding='utf-8-sig')
    df_reg = pd.DataFrame(regime_rows)
    df_reg.to_csv(os.path.join(out_dir, 'diag_eval_multiday_regime_20260723.csv'), index=False, encoding='utf-8-sig')

    # ---- 写 TXT 汇总 ----
    lines = []
    lines.append('tpoint v9 多日 OOS 门控对比汇总')
    lines.append(f'交易日: {",".join(DAYS)}  标的: {",".join(n for _,n in SYMS)}')
    lines.append(f'体制逐棒分布: up={regime_bar_counts["up"]} down={regime_bar_counts["down"]} range={regime_bar_counts["range"]}')
    lines.append('=' * 60)
    lines.append('模式聚合 (9 symbol-day):')
    for m in MODES:
        s = summary[m]
        lines.append(f'  {MODE_LABEL[m]:22s} 笔={s["n_rounds"]:3d} 胜率={s["win_rate"]:5.1f}% '
                     f'累计P&L={s["sum_pnl"]:+7.2f}% 净值={s["final_eq"]:6.1f} 最大回撤={s["max_dd"]:6.2f}%')
    lines.append('-' * 60)
    lines.append('体制@B入场 盈亏:')
    for m in ('floor', 'regime', 'regime_naive'):
        for rg in ('up', 'down', 'range'):
            if (m, rg) in regime_summary:
                cnt, pn, wr = regime_summary[(m, rg)]
                lines.append(f'  {MODE_LABEL[m]:22s} B@[{rg:5s}] 笔={cnt:2d} 胜率={wr:5.1f}% P&L={pn:+7.2f}%')
    txt_path = os.path.join(out_dir, 'diag_eval_multiday_20260723.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    # ---- 画图 ----
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))
    # 左：累计净值曲线
    ax = axes[0]
    x = np.arange(len(DAYS) * len(SYMS))
    for m in MODES:
        ax.plot(x, np.array(equity[m][1:]) * 100, marker='o', ms=3, label=MODE_LABEL[m])
    ax.axhline(100, color='gray', lw=0.8, ls='--')
    ax.set_xticks(x)
    ax.set_xticklabels(seq_labels, rotation=90, fontsize=6)
    ax.set_ylabel('累计净值 (起点=100)')
    ax.set_title('各模式累计净值曲线 (9 symbol-day)')
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    # 右：体制逐棒分布
    ax2 = axes[1]
    rg_labels = ['up', 'down', 'range']
    vals = [regime_bar_counts[k] for k in rg_labels]
    ax2.bar(rg_labels, vals, color=['#2ca02c', '#d62728', '#7f7f7f'])
    for i, v in enumerate(vals):
        ax2.text(i, v, str(v), ha='center', va='bottom', fontsize=9)
    ax2.set_ylabel('逐棒计数')
    ax2.set_title(f'intraday 体制分布 (全样本 {sum(vals)} 棒)')
    fig.tight_layout()
    png_path = os.path.join(out_dir, 'diag_eval_multiday_20260723.png')
    fig.savefig(png_path, dpi=130)
    plt.close(fig)

    print(f'\n✅ CSV : {os.path.join(out_dir, "diag_eval_multiday_20260723.csv")}')
    print(f'✅ CSV : {os.path.join(out_dir, "diag_eval_multiday_regime_20260723.csv")}')
    print(f'✅ TXT : {txt_path}')
    print(f'✅ PNG : {png_path}')


if __name__ == '__main__':
    main()
