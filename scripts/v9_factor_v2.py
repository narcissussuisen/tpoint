# -*- coding: utf-8 -*-
"""v9 因子第一性原理自迭代 (v2).
真实数据: 甘李药业 603087 通达信 bestip. 对比旧 v9(detect_signals) 与新 v2(detect_signals_v2).
设计: B低吸严(非下跌trend∈{0,1}+站回EMA20+超卖反转+放量), S高抛宽(任何趋势刺穿上轨+反转+放量).
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import numpy as np
import pandas as pd
from mootdx.quotes import Quotes
from v9_indicators import compute_indicators, detect_signals

ROOT = "C:/Users/YZP/WorkBuddy/Claw/tpoint"
SYM = "603087.SH"
DAYS = ["2026-07-09", "2026-07-10", "2026-07-13"]
cli = Quotes.factory(market='std', bestip=True)

frames = []
for off in (400, 800, 1200):
    df = cli.bars(symbol='603087', frequency=8, offset=off, market=0)
    if df is not None and len(df):
        frames.append(df)
raw = (pd.concat(frames, ignore_index=True)
       .drop_duplicates(subset=['datetime']).sort_values('datetime').reset_index(drop=True))
dt = pd.to_datetime(raw['datetime'])
raw['trade_date'] = dt.dt.strftime('%Y-%m-%d')
raw['trade_time'] = dt
d = cli.bars(symbol='603087', frequency=9, offset=30, market=0)
dd = pd.to_datetime(d['datetime']); d['td'] = dd.dt.strftime('%Y-%m-%d')
# PC = 前一交易日收盘（不是当日自身收盘！）
_daily = sorted([(r['td'], float(r['close'])) for _, r in d.iterrows()])
pc_map = {}
for i, (day, close) in enumerate(_daily):
    pc_map[day] = _daily[i-1][1] if i > 0 else close  # PC = 前一日收盘

def load(day):
    sub = raw[raw['trade_date'] == day].sort_values('datetime').reset_index(drop=True)
    o = sub['open'].values.astype(float); h = sub['high'].values.astype(float)
    lo = sub['low'].values.astype(float); c = sub['close'].values.astype(float)
    v = sub['volume'].values.astype(float)
    data = compute_indicators(o, h, lo, c, v, pc_map.get(day), has_vol=True)
    data['pc'] = pc_map.get(day, c[0])
    return data, c, [t.strftime('%H:%M') for t in sub['trade_time']]

DATA = {d: load(d) for d in DAYS}

# ========== v2 信号（第一性原理：均值回归+量价反转+动量确认，非对称趋势约束）==========
def detect_signals_v2(data, K1, K2, m, trend_b='strict', trend_s='none', gap=8,
                      start_idx=2, max_b=12, max_s=12):
    """B(低吸): 超卖区(刺穿VWAP-K1·ATR或极端下轨) + 止跌反转K线(长下影/阳线) + 收回下轨
               + 动量确认(站回EMA20 或 RSI回升) + 放量 + trend==1(上升回踩)
               + 日内跌幅>1%时额外要求RSI<35(真超卖, 排除下跌中继弱反弹)
    S(高抛): 超买区(刺穿上轨或极端上轨) + 见顶反转K线(长上影/阴线) + 近15分钟新高
               + RSI回落 + 放量 + 不限trend
    跨信号冷却: B后gap分钟内不发S, S后gap分钟内不发B."""
    n = data['n']; c = data['c']; o = data['o']; lo = data['lo']; h = data['h']
    vwap = data['vwap']; atr = data['atr']; trend = data['trend']; vr = data['vol_ratio']
    ema_f = data['ema_f']; rsi = data['rsi']; pc = data['pc']
    W = 15  # 局部极值窗口(分钟) — 拉长过滤大涨日中段噪音
    sigs = []; b_last = -999; s_last = -999; bc = 0; sc = 0
    for i in range(max(start_idx, 2), n):
        if atr[i] <= 0:
            continue
        lower_std = vwap[i] - K1 * atr[i]; lower_ext = vwap[i] - K2 * atr[i]
        upper_std = vwap[i] + K1 * atr[i]; upper_ext = vwap[i] + K2 * atr[i]
        is_yang = c[i] > o[i]; is_yin = c[i] < o[i]
        ls = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
        us = (h[i] - o[i]) if is_yin else (h[i] - c[i])
        day_chg = (c[i] / pc - 1) * 100 if pc > 0 else 0.0  # 日内涨跌幅
        # ---- B ----
        if bc < max_b and (i - b_last) >= gap and (i - s_last) >= gap:
            hit = (lo[i-1] <= lower_std) or (lo[i] <= lower_std) or (lo[i] <= lower_ext)
            reversion = (c[i] > lower_std) or (c[i] > lower_ext and ls >= atr[i])
            # 日内跌幅>1%: 要求阳线收盘(真买盘) + 实体≥0.3·ATR(非十字星) + RSI<35(真超卖) + 收盘高于前根(真改善) + EMA20上升(结构性拐头)
            if day_chg < -1.0:
                body = abs(c[i] - o[i])
                reversal_k = is_yang and (body >= 0.3 * atr[i])
                momentum = (rsi[i] < 35) and (rsi[i] > rsi[i-1]) and (c[i] > c[i-1]) and (ema_f[i] > ema_f[i-1])
            else:
                reversal_k = is_yang or (ls >= 0.5 * atr[i])
                momentum = (c[i] > ema_f[i]) or (rsi[i] > rsi[i-1])
            trend_ok = (trend_b == 'none') or (int(trend[i]) == 1)
            vol_ok = vr[i] >= m
            if hit and reversion and reversal_k and momentum and vol_ok and trend_ok:
                sigs.append({'type': 'B', 'idx': i, 'price': round(float(c[i]), 2),
                             'reason': '超卖反转' if lo[i] <= lower_ext else '回踩下轨',
                             'vol_ratio': round(float(vr[i]), 2), 'trend': int(trend[i]),
                             'rsi': round(float(rsi[i]), 1), 'day_chg': round(day_chg, 2)})
                b_last = i; bc += 1
        # ---- S ----
        if sc < max_s and (i - s_last) >= gap and (i - b_last) >= gap:
            hit = (h[i-1] >= upper_ext) or (h[i] >= upper_ext)   # 极端上轨(K2·ATR)才算真超买
            local_top = h[i] >= h[max(0, i-W):i+1].max()      # 近W分钟新高=真顶部
            reversal_k = is_yin or (us >= 0.5 * atr[i])
            momentum = (rsi[i] >= 55) and (rsi[i] < rsi[i-1]) and (c[i] < c[i-1])  # RSI≥55超买且回落+收盘低于前根=真见顶
            trend_ok = (trend_s == 'none') or (int(trend[i]) in (-1, 0))
            vol_ok = vr[i] >= m
            if hit and local_top and reversal_k and momentum and vol_ok and trend_ok:
                sigs.append({'type': 'S', 'idx': i, 'price': round(float(c[i]), 2),
                             'reason': '超买回落' if h[i] >= upper_ext else '反弹遇阻',
                             'vol_ratio': round(float(vr[i]), 2), 'trend': int(trend[i]),
                             'rsi': round(float(rsi[i]), 1), 'day_chg': round(day_chg, 2)})
                s_last = i; sc += 1
    return sigs

# ========== 真实性评估（ground truth: 信号后 horizon 分钟真实涨跌）==========
def evaluate(sigs, c, horizon=30, thr=0.3):
    nb = [s for s in sigs if s['type'] == 'B']
    ns = [s for s in sigs if s['type'] == 'S']
    b_real = s_real = 0; b_rets = []; s_rets = []
    for s in nb:
        i = s['idx']; e = min(len(c) - 1, i + horizon)
        mx = max(c[i+1:e+1]) if e > i else c[i]
        ret = (mx / c[i] - 1) * 100; b_rets.append(ret); b_real += ret > thr
        s['future_ret'] = round(ret, 3); s['real'] = bool(ret > thr)
    for s in ns:
        i = s['idx']; e = min(len(c) - 1, i + horizon)
        mn = min(c[i+1:e+1]) if e > i else c[i]
        ret = (mn / c[i] - 1) * 100; s_rets.append(ret); s_real += ret < -thr
        s['future_ret'] = round(ret, 3); s['real'] = bool(ret < -thr)
    total = len(nb) + len(ns); real = b_real + s_real
    return {'nb': len(nb), 'ns': len(ns), 'b_real': b_real, 's_real': s_real,
            'hit_rate': round(real/total*100, 1) if total else 0.0,
            'avg_b_ret': round(np.mean(b_rets), 2) if b_rets else 0.0,
            'avg_s_ret': round(np.mean(s_rets), 2) if s_rets else 0.0}

# ========== 1. 旧 v9 三天基线 ==========
print("=" * 70)
print("旧 v9 (detect_signals, trend==1才买 / S需trend∈{-1,0}) 三天基线:")
for d in DAYS:
    data, c, _ = DATA[d]
    ev = evaluate(detect_signals(data, pc_map[d]), c)
    print(f"  {d}: B={ev['nb']} S={ev['ns']} 真实命中率={ev['hit_rate']}%")

# ========== 2. 三天汇总网格搜索 ==========
print("=" * 70)
print("三天汇总网格搜索 (B严S宽; 目标: 真实B>=1 且 真实S>=1, 命中率高, 信号不过密):")
grid = []
for K1 in (0.8, 1.0):
    for K2 in (1.8, 2.0):
        for m in (1.2, 1.5):
            tnb = tns = tb = ts = 0
            for d in DAYS:
                data, c, _ = DATA[d]
                sigs = detect_signals_v2(data, K1, K2, m)
                ev = evaluate(sigs, c)
                tnb += ev['nb']; tns += ev['ns']; tb += ev['b_real']; ts += ev['s_real']
            total = tnb + tns; real = tb + ts
            hit = (real / total * 100) if total else 0.0
            score = hit - max(0, total - 12) * 3 - (0 if (tb >= 1 and ts >= 1) else 50)
            grid.append((score, K1, K2, m, hit, tb, ts, total))
grid.sort(key=lambda x: -x[0])
for sc, K1, K2, m, hit, tb, ts, total in grid[:6]:
    print(f"  score={sc:5.1f} K1={K1} K2={K2} m={m} -> 汇总B真{tb} S真{ts} 总{total} 命中={hit:.0f}%")

best = next(((K1, K2, m) for sc, K1, K2, m, hit, tb, ts, total in grid if tb >= 1 and ts >= 1),
            (grid[0][1], grid[0][2], grid[0][3]))
K1, K2, m = best
print(f"\n>>> 选定参数: K1={K1} K2={K2} m={m} (B: trend==1+动量确认+跌日RSI<35; S: 不限trend+近15min新高+RSI回落; 跨信号冷却gap={8})")

# ========== 3. 三天明细 ==========
print("=" * 70)
print("三天明细 (v2):")
detail = {}
for d in DAYS:
    data, c, times = DATA[d]
    sigs = detect_signals_v2(data, K1, K2, m)
    ev = evaluate(sigs, c)
    detail[d] = sigs
    print(f"\n  [{d}] 命中率={ev['hit_rate']}% B={ev['nb']}(真{ev['b_real']}) S={ev['ns']}(真{ev['s_real']})")
    for s in sigs:
        tag = '✅真' if s['real'] else '❌假'
        print(f"    {s['type']} {times[s['idx']]} @ {s['price']:.2f} {s['reason']} "
              f"量比{s['vol_ratio']} trend{s['trend']} RSI{s.get('rsi','?')} 日内{s.get('day_chg','?')}% -> 后30m {s['future_ret']:+.2f}% {tag}")

# ========== 4. 落盘 ==========
out = {'best_params': {'K1': K1, 'K2': K2, 'm': m,
                       'trend_b': 'soft', 'trend_s': 'none',
                       'horizon': 30, 'thr': 0.3},
       'grid_top': [{'score': sc, 'K1': a, 'K2': b, 'm': c2, 'hit': h, 'b_real': tb, 's_real': ts, 'total': t}
                    for sc, a, b, c2, h, tb, ts, t in grid[:6]],
       'days': {}}
for d in DAYS:
    data, c, times = DATA[d]
    out['days'][d] = {'sigs': detail[d], 'eval': evaluate(detail[d], c),
                      'c': [float(x) for x in c], 'vwap': [float(x) for x in data['vwap']],
                      'lower_std': [float(x) for x in (data['vwap'] - K1*data['atr'])],
                      'upper_std': [float(x) for x in (data['vwap'] + K1*data['atr'])],
                      'ema_f': [float(x) for x in data['ema_f']],
                      'times': times,
                      'open': float(c[0]), 'high': float(data['h'].max()),
                      'low': float(data['lo'].min()), 'close': float(c[-1]), 'pc': pc_map[d]}
json.dump(out, open(os.path.join(ROOT, "data", "v9_factor_v2_grid.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1,
          default=lambda o: o.item() if hasattr(o, 'item') else (o.tolist() if hasattr(o, 'tolist') else str(o)))
print("\n[ok] 落盘 data/v9_factor_v2_grid.json")
