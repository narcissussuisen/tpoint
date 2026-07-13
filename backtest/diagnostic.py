"""诊断: 原始B信号(无RSI/回撤门控)的特征与结果对比, 找出赢/输差异."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import numpy as np
import pandas as pd
from indicators import compute_indicators

TARGETS = {
    '300975.SZ': '商络电子', '601869.SH': '长飞光纤', '603938.SH': '三孚股份',
    '300395.SZ': '菲利华', '301526.SZ': '国际复材',
    '300757.SZ': '罗博特科', '688820.SH': '盛合晶微',
}
K1 = 1.0; K2 = 2.0; VOL_THRESHOLD = 1.5

def orig_b_trigger(data, i):
    """复刻原始(无RSI/回撤门控)B逻辑, 仅用于诊断."""
    if data['atr'][i] <= 0:
        return False, ''
    c = data['c']; o = data['o']; lo = data['lo']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']
    vol_ratio = data['vol_ratio']; has_vol = data['has_vol']
    lower_std = vwap[i] - K1 * atr[i]
    lower_ext = vwap[i] - K2 * atr[i]
    is_yang = c[i] > o[i]
    lower_shadow = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
    if trend[i] != 1:
        return False, ''
    if not (is_yang or lower_shadow >= 0.5 * atr[i]):
        return False, ''
    triggered = False; reason = ''
    if (c[i-1] <= lower_std or lo[i-1] <= lower_std) and c[i] > lower_std:
        triggered = True; reason = '回踩下轨'
    elif lo[i] <= lower_ext and lower_shadow >= atr[i]:
        triggered = True; reason = '极端超卖反弹'
    if triggered and has_vol and vol_ratio[i] < VOL_THRESHOLD:
        return False, ''
    return triggered, reason

rows = []
for sym, name in TARGETS.items():
    fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data', f'{sym}_1m.csv')
    if not os.path.exists(fpath):
        continue
    df = pd.read_csv(fpath).sort_values('trade_time').reset_index(drop=True)
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['trade_time'].dt.strftime('%Y-%m-%d')
    for date, day_df in df.groupby('date'):
        day_df = day_df.reset_index(drop=True)
        if len(day_df) < 60:
            continue
        o = day_df['open'].values.astype(float); h = day_df['high'].values.astype(float)
        lo = day_df['low'].values.astype(float); c = day_df['close'].values.astype(float)
        v = day_df['volume'].values.astype(float); n = len(day_df)
        pc = float(day_df['open'].iloc[0])
        data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
        for i in range(2, n):
            tb, rb = orig_b_trigger(data, i)
            if not tb:
                continue
            # 特征
            rsi = data['rsi'][i]
            win_hi = c[max(0,i-20):i+1]; win_lo = lo[max(0,i-20):i+1]
            dd = (np.max(win_hi) - np.min(win_lo)) / np.max(win_hi) if np.max(win_hi) > 0 else 0
            vr = data['vol_ratio'][i]; temp = data['temp'][i]
            dev = (c[i] - data['vwap'][i]) / data['vwap'][i] * 100  # 当前价相对VWAP偏离
            # 结果 T+30
            f30 = min(idx := i + 30, n-1)
            ret30 = (c[f30] - c[i]) / c[i] * 100
            hit = ret30 > 0
            rows.append({'name': name, 'rsi': rsi, 'dd': dd*100, 'vr': vr,
                         'temp': temp, 'dev': dev, 'ret30': ret30, 'hit': hit, 'reason': rb})

df = pd.DataFrame(rows)
print(f"总B信号: {len(df)}")
w = df[df.hit]; l = df[~df.hit]
print(f"赢: {len(w)} ({len(w)/len(df)*100:.1f}%)  输: {len(l)}")
print()
print("=== 赢家 vs 输家 特征均值 ===")
for col in ['rsi', 'dd', 'vr', 'temp', 'dev']:
    print(f"  {col:6s}: 赢={w[col].mean():.2f}  输={l[col].mean():.2f}  差={w[col].mean()-l[col].mean():+.2f}")
print()
print("=== 按RSI分箱胜率 ===")
for lo_b, hi_b in [(0,40),(40,50),(50,60),(60,100)]:
    sub = df[(df.rsi>=lo_b)&(df.rsi<hi_b)]
    if len(sub):
        print(f"  RSI[{lo_b},{hi_b}): n={len(sub)} 胜率={sub.hit.mean()*100:.1f}%")
print()
print("=== 按回撤深度分箱胜率 ===")
for lo_b, hi_b in [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,100)]:
    sub = df[(df.dd>=lo_b)&(df.dd<hi_b)]
    if len(sub):
        print(f"  回撤[{lo_b}%,{hi_b}%): n={len(sub)} 胜率={sub.hit.mean()*100:.1f}%")
print()
print("=== 按量比分箱胜率 ===")
for lo_b, hi_b in [(0,1.2),(1.2,1.5),(1.5,2.0),(2.0,100)]:
    sub = df[(df.vr>=lo_b)&(df.vr<hi_b)]
    if len(sub):
        print(f"  量比[{lo_b},{hi_b}): n={len(sub)} 胜率={sub.hit.mean()*100:.1f}%")
print()
print("=== 按reason ===")
for r in df.reason.unique():
    sub = df[df.reason==r]
    print(f"  {r}: n={len(sub)} 胜率={sub.hit.mean()*100:.1f}%")
print()
print("=== B盈亏比 (ret30) ===")
print(f"  赢家平均收益: {w.ret30.mean():.3f}%  输家平均收益: {l.ret30.mean():.3f}%")
if l.ret30.mean() != 0:
    print(f"  盈亏比: {abs(w.ret30.mean()/l.ret30.mean()):.2f}:1")
# 高量比子集
hv = df[df.vr >= 2.0]
print(f"\n=== 量比≥2.0子集 (n={len(hv)}) ===")
print(f"  胜率: {hv.hit.mean()*100:.1f}%  赢均收益: {hv[hv.hit].ret30.mean():.3f}%  输均收益: {hv[~hv.hit].ret30.mean():.3f}%")
