# -*- coding: utf-8 -*-
"""D 候选策略 —— 权威信号层 + 前向回测 (干净方法, 无任何前视偏差)

这是 feat/v9.4.0-floord-candidate 分支的 D 策略**唯一权威定义**。
所有回测 / 后续生产集成都必须从这里 import, 不得在各脚本里重复实现信号层。

本模块解决了前几轮诊断的四重偏差(对应旧 diagnose_floor_combo_0724.py 等后视镜脚本):

  偏差1 (pc seed / regime 黏隔夜): 旧诊断把 pc=昨收喂给指标, 导致 EMA 被隔夜跳空黏住、
        不编码日内趋势。
        -> 本模块 regime 用「日内 seed」EMA(当日第一根收盘做 seed, 每日重置), 纯因果。

  偏差2 (漏顶/漏底): 原 _is_new_high 用 收盘价 比 前窗最高价, 顶部反转 bar 收盘回落 ->
        结构性漏掉真实顶/底。
        -> 本模块用 BAR 自身 HIGH/LOW 取极值 (is_swing_high/low)。

  偏差3 (后视镜 P&L): 旧诊断用「向前固定 N 根持有」评估信号质量, 含未来信息。
        -> 本模块 forward_backtest 用真正的前向回测: 信号 bar 下一根开盘入场,
           ATR 止损 / D 卖点反转出场 / 14:55 EOD 强平, 计算真实可落袋 P&L。

  偏差4 (未来反转确认参与触发): 旧 combo 用「极值后 N 根多数反向」做触发条件, 含未来信息。
        -> 本模块 d_signals 只用信号 bar 及之前的数据 (因果条件); 若需反转确认仅作
           审计列, 绝不参与触发。

信号语义 (T+0 正向 T):
  买: 日内上行 regime(EMA_f >= EMA_s) 且 swing_low 且 偏离 VWAP <= -K*ATR%
  卖: swing_high 且 偏离 VWAP >= +K*ATR%   (卖点作为正向 T 的反转出场触发, regime 无关)
  仅开多、不隔夜、首信号为买。
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = 'F:/keyfactor_data/1m'


# ---------- 数据 ----------
def load_day(code, day, data_dir=DATA_DIR):
    """读取单标的单交易日 1m, 计算指标, 返回 dict。数据缺失返回 None。"""
    p = os.path.join(data_dir, f'{code}_1m.csv')
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, encoding='utf-8-sig')
    df['tt'] = df['trade_time'].astype(str).str.split(' ').str[-1]
    d = df[df['trade_date'] == day].reset_index(drop=True)
    if len(d) == 0:
        return None
    o = d['open'].values.astype(float)
    h = d['high'].astype(float)
    lo = d['low'].astype(float)
    c = d['close'].values.astype(float)
    # 数据清洗: mootdx 导出偶发 5.8e-39 哨兵量(损坏), 用线性插值+当日中位数兜底,
    # 避免污染 VWAP。纯数据修正, 不含任何未来信息。
    vs = pd.Series(d['volume'].values.astype(float))
    vs = vs.mask(vs <= 1e-6)
    vs = vs.interpolate(method='linear', limit=30).bfill().ffill()
    if vs.isna().any():
        vs = vs.fillna(vs.median())
    v = vs.values.astype(float)
    tt = d['tt'].values
    # 日内 seed: pc 用当日首根收盘(仅影响 chg/temp, D 不用); 指标 ATR/VWAP/MACD 不依赖 pc
    pc = float(c[0])
    import sys
    sys.path.insert(0, os.path.join(ROOT, 'core'))
    import miji_alpha as MA
    data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    return {'code': code, 'day': day, 'o': o, 'h': h, 'lo': lo,
            'c': c, 'v': v, 'tt': tt, 'data': data, 'n': len(c)}


# ---------- 修正后的极值判定 (用 BAR 自身 HIGH/LOW) ----------
def is_swing_low(lo, i, w):
    if i < 1:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def is_swing_high(h, i, w):
    if i < 1:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


# ---------- D 信号层 (纯因果) ----------
def d_signals(day, K, WL, ema_f_span, ema_s_span, _ema=None, _sl=None, _sh=None):
    """返回因果买/卖索引 + regime 数组。

    买: 上行regime(EMA_f>=EMA_s) 且 swing_low 且 偏离VWAP <= -K*ATR%
    卖: swing_high 且 偏离VWAP >= +K*ATR%  (卖点作正向T出场触发, regime无关)
    regime 用日内 seed EMA(每日重置, 首根收盘做 seed, 不掺隔夜跳空)。

    _ema/_sl/_sh 为可选预计算缓存 (见 d_candidate_backtest.load_all 的逐日预计算),
    传入可避免网格内层重复计算 EMA / swing, 大幅加速。
    """
    c = day['c']; h = day['h']; lo = day['lo']
    vwap = day['data']['vwap']; atr = day['data']['atr']; n = day['n']

    # 日内 seed EMA: 仅用当日close, adjust=False -> 首根 EMA=首根收盘, 不掺隔夜跳空
    if _ema is not None:
        ema_f, ema_s = _ema
    else:
        ema_f = pd.Series(c).ewm(span=ema_f_span, adjust=False).mean().values
        ema_s = pd.Series(c).ewm(span=ema_s_span, adjust=False).mean().values
    uptrend = ema_f >= ema_s

    if _sl is not None and _sh is not None:
        sl_arr, sh_arr = _sl, _sh
    else:
        sl_arr = np.array([is_swing_low(lo, i, WL) for i in range(n)], dtype=bool)
        sh_arr = np.array([is_swing_high(h, i, WL) for i in range(n)], dtype=bool)

    buys, sells = [], []
    for i in range(WL, n):
        if atr[i] <= 0:
            continue
        ap = atr[i] / vwap[i] * 100.0
        g = (c[i] - vwap[i]) / vwap[i] * 100.0
        thr = K * ap
        if sl_arr[i] and g <= -thr and uptrend[i]:
            buys.append(i)
        if sh_arr[i] and g >= thr:
            sells.append(i)
    return buys, sells, uptrend


# ---------- 前向回测: T+0 正向T ----------
def forward_backtest(day, buys, sells, k_stop, rev_exit, start_idx=2):
    """信号 bar 下一根开盘入场(无前视); 出场: ATR止损 / D卖点反转 / 14:55 EOD强平。正向T不开空。"""
    o = day['o']; h = day['h']; lo = day['lo']; c = day['c']; tt = day['tt']
    atr = day['data']['atr']; n = day['n']
    buy_set = set(buys); sell_set = set(sells)

    trades = []
    pos = None          # {'entry_idx','entry'}
    pending = None      # {'bar':i+1,'signal_idx':i}

    for i in range(start_idx, n):
        # 1) 执行挂起的下一根开盘入场
        if pending is not None and pending['bar'] == i:
            pos = {'entry_idx': pending['signal_idx'], 'entry': o[i]}
            pending = None
        # 2) 持仓则检查出场
        if pos is not None:
            stop_px = pos['entry'] - k_stop * atr[pos['entry_idx']]
            if lo[i] <= stop_px:
                pnl = (stop_px - pos['entry']) / pos['entry'] * 100.0
                trades.append({'entry_idx': pos['entry_idx'], 'exit_idx': i,
                               'entry': pos['entry'], 'exit': stop_px,
                               'pnl': pnl, 'reason': 'STOP'})
                pos = None
                continue
            if rev_exit and i in sell_set:
                exit_px = o[i + 1] if i + 1 < n else c[i]
                pnl = (exit_px - pos['entry']) / pos['entry'] * 100.0
                trades.append({'entry_idx': pos['entry_idx'], 'exit_idx': i,
                               'entry': pos['entry'], 'exit': exit_px,
                               'pnl': pnl, 'reason': 'REV'})
                pos = None
                continue
            if tt[i] >= '14:55:00':
                pnl = (c[i] - pos['entry']) / pos['entry'] * 100.0
                trades.append({'entry_idx': pos['entry_idx'], 'exit_idx': i,
                               'entry': pos['entry'], 'exit': c[i],
                               'pnl': pnl, 'reason': 'EOD'})
                pos = None
                continue
        # 3) 空仓则登记下一根入场
        if pos is None and pending is None and i in buy_set:
            pending = {'bar': i + 1, 'signal_idx': i}
    # 收盘仍持仓 -> 强制 EOD
    if pos is not None:
        pnl = (c[n - 1] - pos['entry']) / pos['entry'] * 100.0
        trades.append({'entry_idx': pos['entry_idx'], 'exit_idx': n - 1,
                       'entry': pos['entry'], 'exit': c[n - 1],
                       'pnl': pnl, 'reason': 'EOD_FORCE'})
    return trades


def agg(trades):
    if not trades:
        return {'n': 0, 'win': 0, 'win_rate': None, 'tot_ret': 0.0,
                'pf': None, 'avg': 0.0}
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    gross_w = sum(wins); gross_l = sum(losses)
    wr = len(wins) / len(pnls) if pnls else 0
    pf = (gross_w / gross_l) if gross_l > 0 else (float('inf') if gross_w > 0 else 0)
    return {'n': len(pnls), 'win': len(wins), 'win_rate': round(wr * 100, 2),
            'tot_ret': round(sum(pnls), 3),
            'pf': (round(pf, 3) if pf != float('inf') else None),
            'avg': round(sum(pnls) / len(pnls), 3)}
