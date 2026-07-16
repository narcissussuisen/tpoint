#!/usr/bin/env python3
"""
keyfactor 共享工具: 1m CSV 加载 + v9.1.0 引擎调用 + 前向收益 + 归因聚合。
纯 numpy/pandas, 不依赖网络。可在已有 seed CSV 上直接验证管线。

重要: v9.1.0 引擎中 pc(昨收) 仅用于 display 的 day_chg, 三个因子函数
(gravity_signal / volume_divergence_signal / macd_divergence_signal) 均不使用 pc。
故 pc 取值不影响信号/因子逻辑, 此处传 pc=c[0] 即可。
"""
import sys, os
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import miji_engine as ME

HORIZONS = [6, 12, 24]  # 前向收益 horizon (根 1m)

def load_1m(path):
    """读取 seed/下载的 1m CSV, 返回 (df, name, sym)。
    schema: symbol,name,timestamp,trade_date,trade_time,open,high,low,close,volume,amount
    """
    df = pd.read_csv(path, dtype={'symbol': str, 'name': str})
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df = df.sort_values('trade_time').reset_index(drop=True)
    return df

def run_engine(df, enable=(True, True, True), min_resonance=2):
    """对单标的 1m df 跑 v9.1.0 引擎, 返回信号 list。
    每信号含 type/idx/price/chg/resonance_score/factors/detail。
    """
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    has_vol = float(np.sum(v)) > 0
    pc = float(c[0]) if len(c) > 0 else 0.0
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_resonance,
                                  b_trend_filter=False, allow_reverse=True, enable=enable)
    return sigs

def fwd_rets(df, sigs):
    """对每个信号算前向收益 (close[i+h]/close[i]-1)*100, h in HORIZONS。
    返回 sigs (就地附加 fwd6/fwd12/fwd24 字段)。"""
    c = df['close'].values.astype(float)
    n = len(c)
    for s in sigs:
        i = s['idx']
        rec = {}
        for hh in HORIZONS:
            if i + hh < n:
                rec[hh] = (c[i + hh] / c[i] - 1.0) * 100.0
            else:
                rec[hh] = np.nan
        for hh in HORIZONS:
            s[f'fwd{hh}'] = round(float(rec[hh]), 4) if not np.isnan(rec[hh]) else None
    return sigs

def factor_presence(sig):
    """返回该信号各因子在其方向上的"是否参与"(1/0)。
    B信号: gravity==+1 / vol_div==+1 / macd_div==+1 算参与;
    S信号: gravity==-1 / vol_div==-1 / macd_div==-1 算参与。"""
    f = sig['factors']
    if sig['type'] == 'B':
        return {'gravity': 1 if f['gravity'] == 1 else 0,
                'vol_div': 1 if f['vol_div'] == 1 else 0,
                'macd_div': 1 if f['macd_div'] == 1 else 0}
    else:
        return {'gravity': 1 if f['gravity'] == -1 else 0,
                'vol_div': 1 if f['vol_div'] == -1 else 0,
                'macd_div': 1 if f['macd_div'] == -1 else 0}

def build_attr_rows(sigs, df):
    rows = []
    for s in sigs:
        fp = factor_presence(s)
        row = {
            'type': s['type'], 'idx': s['idx'], 'price': s['price'],
            'resonance_score': s['resonance_score'],
            'g': fp['gravity'], 'vd': fp['vol_div'], 'md': fp['macd_div'],
        }
        for hh in HORIZONS:
            row[f'fwd{hh}'] = s.get(f'fwd{hh}')
        rows.append(row)
    return rows

def aggregate(rows):
    """按"因子是否参与"分组, 统计信号数与平均前向收益。
    返回 dict: 每个因子 (g/vd/md) 在 参与 vs 不参与 时的均值前向收益与计数。"""
    import numpy as np
    out = {}
    for fac in ('g', 'vd', 'md'):
        sub_on = [r for r in rows if r[fac] == 1]
        sub_off = [r for r in rows if r[fac] == 0]
        rec = {'n_on': len(sub_on), 'n_off': len(sub_off)}
        for hh in HORIZONS:
            on_vals = [r[f'fwd{hh}'] for r in sub_on if r[f'fwd{hh}'] is not None]
            off_vals = [r[f'fwd{hh}'] for r in sub_off if r[f'fwd{hh}'] is not None]
            rec[f'mean_fwd{hh}_on'] = float(np.mean(on_vals)) if on_vals else None
            rec[f'mean_fwd{hh}_off'] = float(np.mean(off_vals)) if off_vals else None
        out[fac] = rec
    return out

def summarize_signals(sigs):
    nB = sum(1 for s in sigs if s['type'] == 'B')
    nS = sum(1 for s in sigs if s['type'] == 'S')
    scoreB = [s['resonance_score'] for s in sigs if s['type'] == 'B']
    scoreS = [s['resonance_score'] for s in sigs if s['type'] == 'S']
    return {
        'n_total': len(sigs), 'nB': nB, 'nS': nS,
        'mean_score_B': float(np.mean(scoreB)) if scoreB else 0.0,
        'mean_score_S': float(np.mean(scoreS)) if scoreS else 0.0,
    }
