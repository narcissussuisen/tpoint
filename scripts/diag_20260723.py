#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_20260723.py — tpoint v9 三问题根因诊断（与生产同引擎回放）

复刻 core/monitor.py detect_for 的信号生成 + 出场管理逻辑（floor 门控 + 移动止损），
对今日(2026-07-23)全天 1m 真实回放，定量回答：
  问题一 信号稀少：floor 门控 vs 纯引力 的抑制比；今日实际评估窗口(冻结在bar118)
  问题二 准确率低：原始信号各 horizon 命中率 + 引擎实际回合 P&L（含移动止损）
  问题三 移动止损不匹配：基准价/激活/出场轨迹 + 入场时偏移(fill at c[i] vs c[i+1])
"""
import os
import sys
import json

# 复刻生产门控：run_monitor.bat L9 设 MACD_GATE_MODE=floor
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
diag_data = []   # 收集 (sym,name,df,events,data) 供绘图

# ---- 复刻 monitor 常量 ----
COLDOWN_BARS = 3
MAX_B_DAILY = 12
MAX_S_DAILY = 12
MAX_SIZE_PCT = 8
EXIT_CFG = dict(use_stop=False, use_time=False, use_trailing=True,
                trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)


def strength_size(g_dev_pct, m_present):
    strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
    return 4 if strong else 2


def fetch(sym):
    ds = MootdxDataSource()
    df = ds.klines.intraday(sym, as_dataframe=True)
    df = df.sort_values('trade_time').reset_index(drop=True)
    # 前收 pc：日K（与 monitor compute 口径一致）
    d = ds.klines.get(sym, period='1d', count=60, as_dataframe=True)
    d = d.sort_values('trade_date')
    today = pd.Timestamp.now().strftime('%Y%m%d')
    last_date = str(d['trade_date'].iloc[-1])[:10]
    pc = float(d['close'].iloc[-2]) if last_date == today else float(d['close'].iloc[-1])
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


def simulate(sym, name, data, ban_downtrend_b=False):
    """复刻 detect_for 信号+出场，返回 events 列表 与 诊断中间量。
    ban_downtrend_b: True 时空仓且 trend==-1 时禁止开多(measure ① 消融开关)，
                     但仍允许用 B 信号平掉已有空仓(cover 不受影响)。
    """
    c = data['c']; lo = data['lo']; vwap = data['vwap']; atr = data['atr']
    trend = data['trend']; n = data['n']
    df = data['df']
    trade_times = df['trade_time'].values if df is not None else None

    st = {}
    pos = None
    events = []          # 每笔信号/出场
    closed = []          # 已平回合(多+空), 用于完整 P&L
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
            # 2) 反向信号自然平仓
            if not exited and EXIT_CFG['s_signal_exit']:
                if side == 'long':
                    ts, rs = miji.check_s_trigger(data, i)
                    if ts:
                        events.append(('X', 'S', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], rs))
                        closed.append(dict(side='long', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                           max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                           exit_reason='S', size=pos['size_pct']))
                        pos = None; exited = True
                else:
                    tb, rb = miji.check_b_trigger(data, i)
                    if tb:
                        events.append(('X', 'B', i, bt, float(c[i]), pos['entry_price'], pos['size_pct'], rb))
                        closed.append(dict(side='short', entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                                           max_fav=pos['max_fav'], exit_price=float(c[i]), exit_idx=i,
                                           exit_reason='B', size=pos['size_pct']))
                        pos = None; exited = True
            # 3) 移动止损
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

        # 空仓：双向触发
        tb, rb = miji.check_b_trigger(data, i)
        ts, rs = miji.check_s_trigger(data, i)
        if not (tb or ts):
            st[bar_key] = 1
            continue
        if tb:
            # measure ① 消融：空仓且处于下跌趋势时禁止开多(接飞刀)，但保留 B 平空仓(cover)
            if ban_downtrend_b and pos is None and data['trend'][i] == -1:
                st[bar_key] = 1
                continue
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

    # 收盘强制平仓（标记 EOD），避免隔夜裸仓不计入 P&L
    overnight = pos is not None
    if pos is not None:
        ei = n - 1
        ebt = str(trade_times[ei])[11:16] if trade_times is not None and ei < len(trade_times) else ''
        events.append(('X', 'EOD', ei, ebt, float(c[ei]), pos['entry_price'], pos['size_pct'], ''))
        closed.append(dict(side=pos['side'], entry=pos['entry_price'], entry_idx=pos['entry_idx'],
                           max_fav=pos['max_fav'], exit_price=float(c[ei]), exit_idx=ei,
                           exit_reason='EOD', size=pos['size_pct']))
        pos = None
    return events, closed, overnight, n


def fwd_ret(c, i, k):
    j = min(i + k, len(c) - 1)
    return (c[j] / c[i] - 1) * 100


def main():
    print('=' * 70)
    print('tpoint v9 三问题根因诊断  |  日期 2026-07-23  |  门控=floor  |  与生产同引擎回放')
    print('=' * 70)
    all_rows = []
    for sym, name in SYMS:
        print(f'\n########## {sym} {name} ##########')
        try:
            df, pc = fetch(sym)
        except Exception as e:
            print(f'  ⚠️ 数据获取失败: {e}')
            continue
        if df is None or len(df) == 0:
            print('  ⚠️ 无 1m 数据')
            continue
        data = build_data(df, pc)
        c = data['c']; n = data['n']
        print(f'  pc(前收)={pc:.3f}  1m bar数={n}  时段 {str(df["trade_time"].iloc[0])[11:16]}~{str(df["trade_time"].iloc[-1])[11:16]}')

        # ---------- 问题一：信号稀少 ----------
        grav_long = 0; grav_short = 0; trig_b = 0; trig_s = 0
        for i in range(2, n):
            if data['atr'][i] <= 0:
                continue
            gf, _ = miji.gravity_signal(c, data['vwap'], data['atr'], i)
            if gf == 1:
                grav_long += 1
            elif gf == -1:
                grav_short += 1
            tb, _ = miji.check_b_trigger(data, i)
            ts, _ = miji.check_s_trigger(data, i)
            if tb:
                trig_b += 1
            if ts:
                trig_s += 1
        print('\n--- [问题一] 信号稀少（全天 {n} 根可评估 bar）---'.format(n=n))
        print(f'  纯引力超卖(buy)bar数 = {grav_long}   纯引力超买(sell)bar数 = {grav_short}')
        print(f'  floor门控实际放行 B = {trig_b}   S = {trig_s}')
        if grav_long:
            print(f'  买点抑制比: 纯引力 {grav_long} → floor放行 {trig_b}  (过滤 {(1-trig_b/grav_long)*100:.1f}%)')
        if grav_short:
            print(f'  卖点抑制比: 纯引力 {grav_short} → floor放行 {trig_s}  (过滤 {(1-trig_s/grav_short)*100:.1f}%)')
        print('  ⇒ 主因: floor 门控要求「MACD背离 或 价格地板(15棒新低+偏离VWAP≤-1.5%)」,')
        print('           把大量纯引力波动过滤掉; 叠加 SIGNAL_GAP=8棒(8分钟)间隔。')
        # 今日实际评估窗口（冻结在 bar118）
        events, closed, overnight, _ = simulate(sym, name, data)
        bs_in_eval = [e for e in events if e[0] in ('B', 'S') and e[2] <= 118]
        bs_frozen = [e for e in events if e[0] in ('B', 'S') and e[2] > 118]
        print(f'  今日实跑仅评估到 bar118(≈11:28冻结): 评估窗内买卖信号={len(bs_in_eval)}, 冻结后未评估={len(bs_frozen)}')

        # ---------- 问题二：准确率低 ----------
        b_entries = [e for e in events if e[0] == 'B']
        s_entries = [e for e in events if e[0] == 'S']
        print('\n--- [问题二] 准确率低 ---')
        print(f'  引擎生成 B={len(b_entries)}  S={len(s_entries)}')
        # 原始信号命中率（forward return>0）
        for k in (3, 5, 10, 20):
            bw = sum(1 for e in b_entries if fwd_ret(c, e[2], k) > 0)
            sw = sum(1 for e in s_entries if -fwd_ret(c, e[2], k) > 0)  # 做空盈利=价格跌
            btot = len(b_entries); stot = len(s_entries)
            br = f'{bw/btot*100:.0f}%' if btot else 'NA'
            sr = f'{sw/stot*100:.0f}%' if stot else 'NA'
            print(f'  +{k}棒 命中率: B={br}({bw}/{btot})  S={sr}({sw}/{stot})')
        # 趋势背景：B在下跌趋势(trend==-1)中的占比
        b_downtrend = sum(1 for e in b_entries if data['trend'][e[2]] == -1)
        print(f'  B买点中处于下跌趋势(trend==-1)占比 = {b_downtrend}/{len(b_entries)} '
              f'({b_downtrend/len(b_entries)*100:.0f}%)  → 接飞刀风险' if b_entries else '  (无B)')

        # 引擎实际回合 P&L（含移动止损 + 收盘强制平仓）
        win = 0; loss = 0; tot = 0.0
        for lp in closed:
            if lp['side'] == 'long':
                r = (lp['exit_price'] - lp['entry']) / lp['entry'] * 100
            else:
                r = (lp['entry'] - lp['exit_price']) / lp['entry'] * 100
            tot += r
            if r > 0: win += 1
            else: loss += 1
        print(f'  引擎实际回合(含EOD强平): 笔数={len(closed)} 盈利={win} 亏损={loss} 累计P&L={tot:.2f}%')
        n_trail = sum(1 for lp in closed if lp['exit_reason'] == 'TRAIL')
        n_eod = sum(1 for lp in closed if lp['exit_reason'] == 'EOD')
        n_s = sum(1 for lp in closed if lp['exit_reason'] in ('S', 'B'))
        print(f'    出场构成: 移动止损={n_trail}  反向信号平={n_s}  收盘强平={n_eod}')
        if n_eod:
            print('  ⚠️ 有仓位收盘未平被强制按收盘价结算(引擎本身无EOD强平→真实会隔夜裸仓)')
        if overnight:
            print('  ⚠️ 模拟末仍标记隔夜(理论); 已按EOD强平计入P&L')

        # ---------- 问题三：移动止损不匹配 ----------
        print('\n--- [问题三] 移动止损不匹配 ---')
        print(f'  EXIT_CFG: trailing 激活={EXIT_CFG["trail_activate_pct"]}% 回撤={EXIT_CFG["trail_pct"]}% '
              f's_signal_exit={EXIT_CFG["s_signal_exit"]} 硬止损/时间止损=关')
        print('  基准价 = max_fav(持仓以来最高收盘); 入场价 = c[i](信号bar收盘); 推送价 = c[i]')
        # 入场时偏移：假设用户在下一根bar成交
        gap_sum = 0.0; gap_n = 0; gaps = []
        for e in b_entries:
            i = e[2]
            if i + 1 < n:
                g = (c[i + 1] - c[i]) / c[i] * 100
                gap_sum += g; gap_n += 1; gaps.append(g)
        if gap_n:
            print(f'  入场时偏移(用户延1棒成交 vs 引擎c[i]): 均值 {gap_sum/gap_n:+.3f}%  中位 '
                  f'{np.median(gaps):+.3f}%  (多头为正=入场更贵→劣化)')
        # 移动止损激活情况
        act = [lp for lp in closed if lp['exit_reason'] == 'TRAIL']
        s_exit = [lp for lp in closed if lp['exit_reason'] in ('S', 'B')]
        print(f'  被平回合: 移动止损(TRAIL)={len(act)}  反向信号平={len(s_exit)}')
        print('  ⇒ 若浮盈从未达 +0.4%, 移动止损永不激活, 仅靠反向S或隔夜平仓 → 亏损仓不止损')
        print('  ⇒ 推送卡片显示「触及下轨/上轨」=VWAP±1.0·ATR, 并非移动止损线 → 语义错位')

        # 汇总行（CSV）
        for e in events:
            all_rows.append({
                'sym': sym, 'name': name, 'type': e[0], 'sub': e[1], 'idx': e[2],
                'time': e[3], 'price': round(e[4], 3), 'entry': round(e[5], 3) if e[5] else '',
                'size': e[6], 'detail': e[7],
            })

        # 收集绘图数据（循环内，逐标的）
        diag_data.append((sym, name, df, events, data))

    # 写 CSV
    out = os.path.join(ROOT, 'output', 'diag_20260723_trades.csv')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pd.DataFrame(all_rows).to_csv(out, index=False, encoding='utf-8-sig')
    print(f'\n[CSV] 事件明细 -> {out}')

    # ---- 绘图：三标的走势 + B/S 标记 + VWAP，直观展示"接飞刀" ----
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        try:  # 注册系统中文字体，避免 CJK 字形缺失(显示为方块)
            fm.fontManager.addfont(r'C:/Windows/Fonts/simhei.ttf')
            plt.rcParams['font.sans-serif'] = ['SimHei']
            plt.rcParams['axes.unicode_minus'] = False
        except Exception:
            pass
        fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=False)
        for ax, (sym, name, df, events, data) in zip(axes, diag_data):
            c = data['c']; vwap = data['vwap']
            t = pd.to_datetime(df['trade_time'])
            ax.plot(t, c, color='#444', lw=0.8, label='close')
            ax.plot(t, vwap, color='#1f77b4', lw=0.8, ls='--', label='VWAP')
            for e in events:
                et, sub, idx, bt, price = e[0], e[1], e[2], e[3], e[4]
                if et == 'B':
                    ax.scatter(t.iloc[idx], price, color='green', marker='^', s=55, zorder=5)
                elif et == 'S':
                    ax.scatter(t.iloc[idx], price, color='red', marker='v', s=55, zorder=5)
                elif et == 'X' and sub == 'TRAIL':
                    ax.scatter(t.iloc[idx], price, color='blue', marker='x', s=40, zorder=5)
            ax.set_title(f'{sym} {name}  |  B=绿▲ S=红▼ 移动止损=蓝×', fontsize=10)
            ax.legend(loc='upper left', fontsize=7)
            ax.tick_params(axis='x', labelsize=7)
            ax.grid(alpha=0.25)
        fig.tight_layout()
        png = os.path.join(ROOT, 'output', 'diag_20260723_signals.png')
        fig.savefig(png, dpi=110)
        plt.close(fig)
        print(f'[PNG] 信号标记图 -> {png}')
    except Exception as ex:
        print(f'[plot] 跳过绘图: {ex}')


if __name__ == '__main__':
    main()
