"""
复盘分析: tpoint 策略在 161129.SZ 上今日(2026-07-21)零信号原因排查
- 复刻 monitor 生产路径: 1m + 严格 MACD 门控(strict) + PC=昨收 + 无量价背离(VOL_DIV_ENABLED=False)
- 逐 bar 还原三因子(gravity / vol_div / macd_div)与门控结果, 定位"最近一次触发差多远"
- 对比近期正常出信号日(07-20 / 07-17)的市况差异
输出: 终端文本报告 + output/161129_0721_review.json + output/161129_0721_review.html
"""
import sys, json, math
import numpy as np
import pandas as pd
sys.path.insert(0, 'core')
from datasource import MootdxDataSource
from miji_alpha import (compute_miji_indicators, detect_miji_signals,
                        check_miji_trigger, VWAP_DEV_BUY, VWAP_DEV_SELL,
                        LOCAL_W, MACD_GATE_MODE)

SYM = '161129.SZ'
TODAY = '2026-07-21'
COMPARE_DAYS = ['2026-07-20', '2026-07-17']  # 07-20 实盘 B1/S2; 07-17 回放活跃
ds = MootdxDataSource()

def get_pc(day):
    d = ds.get(SYM, period='1d', count=40)
    if d is None or len(d) == 0:
        return None
    m = {dt: cl for dt, cl in zip(d['trade_date'].tolist(), d['close'].tolist())}
    sd = sorted(m.keys())
    if day in m:
        idx = sd.index(day)
        return float(m[sd[idx-1]]) if idx > 0 else float(m[day])
    before = [x for x in sd if x < day]
    return float(m[before[-1]]) if before else float(d['close'].iloc[-1])

def load_day(day, today=False):
    if today:
        df = ds.intraday(SYM)
    else:
        try:
            df = ds.historical_1m(SYM, day, offset=1000)
        except Exception as e:
            print(f"  [warn] {day} historical_1m failed: {e}")
            return None, None, None
    if df is None or len(df) == 0:
        return None, None, None
    df = df.sort_values('trade_time').reset_index(drop=True)
    o = df['open'].values.astype(float); h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    has_vol = bool(np.sum(v) > 0)
    pc = get_pc(day)
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    return df, data, pc

def analyze(day, df, data, pc, today=False):
    n = data['n']
    c = data['c']; vwap = data['vwap']; atr = data['atr']
    dev = np.where(vwap > 0, (c - vwap) / vwap * 100, 0.0)
    # 逐 bar 触发还原
    n_grav_b = n_grav_s = n_macd_b = n_macd_s = 0
    n_blocked_b = n_blocked_s = 0          # gravity 触发但 MACD 门控挡掉(strict)
    n_early_grav = 0                        # 早盘(i<LOCAL_W) gravity 触发
    n_strict_b = n_strict_s = 0             # 实际 strict 门控 raw 触发(bar 级)
    closest_b_gap = 1e9; closest_s_gap = -1e9   # dev 距 B/S 触发带的最小距离(%)
    band_b = []; band_s = []; near_misses = []
    for i in range(2, n):
        if atr[i] <= 0:
            continue
        # gravity 触发带(分)
        lower = vwap[i] - VWAP_DEV_BUY * atr[i]
        upper = vwap[i] + VWAP_DEV_SELL * atr[i]
        band_b.append((lower - vwap[i]) / vwap[i] * 100)   # 触发 B 所需 dev 上限
        band_s.append((upper - vwap[i]) / vwap[i] * 100)
        g, g_dev = (lambda r: (r[0], r[1]))(None) if False else (None, None)
        # 用模块函数算 gravity
        from miji_alpha import gravity_signal
        gf, gdev = gravity_signal(c, vwap, atr, i)
        if gf == 1:
            n_grav_b += 1
            gap = dev[i] - band_b[-1]   # <=0 表示触发
            closest_b_gap = min(closest_b_gap, gap)
            if i < LOCAL_W:
                n_early_grav += 1
        elif gf == -1:
            n_grav_s += 1
            gap = dev[i] - band_s[-1]
            closest_s_gap = max(closest_s_gap, gap)
        # MACD 因子
        from miji_alpha import macd_divergence_signal
        mf, md = macd_divergence_signal(df['high'].values.astype(float),
                                        df['low'].values.astype(float), c,
                                        data['dif'], data['dea'], data['hist'], i)
        if mf == 1:
            n_macd_b += 1
        elif mf == -1:
            n_macd_s += 1
        # strict 门控 raw 触发
        btrig, strig, bd, sd_, snap = check_miji_trigger(data, i, macd_gate_mode='strict')
        if btrig:
            n_strict_b += 1
        if strig:
            n_strict_s += 1
        # 记录 gravity 触发但被 MACD 挡掉的"近失"
        if gf == 1 and mf != 1:
            n_blocked_b += 1
            if len(near_misses) < 6:
                near_misses.append({'i': i, 't': str(df['trade_time'].iloc[i]),
                                    'price': round(float(c[i]), 4),
                                    'dev': round(float(gdev), 3),
                                    'macd': md or '(无背离)', 'side': 'B'})
        if gf == -1 and mf != -1:
            n_blocked_s += 1
            if len(near_misses) < 6:
                near_misses.append({'i': i, 't': str(df['trade_time'].iloc[i]),
                                    'price': round(float(c[i]), 4),
                                    'dev': round(float(gdev), 3),
                                    'macd': md or '(无背离)', 'side': 'S'})
    # 实际发出信号(含 gap/每日上限)
    sigs = detect_miji_signals(data, pc, macd_gate_mode='strict')
    # 市况统计
    day_chg = (c[-1] / pc - 1) * 100 if pc else 0
    rng = (c.max() - c.min()) / c.min() * 100 if c.min() > 0 else 0
    flat = int(np.sum((df['high'].values.astype(float) == df['low'].values.astype(float))))
    # 数据源判定(LOF 多为腾讯兜底, OHLC 合成)
    tencent_like = flat > 0.5 * n
    return {
        'day': day, 'today': today, 'n_bars': n,
        'time_start': str(df['trade_time'].iloc[0]), 'time_end': str(df['trade_time'].iloc[-1]),
        'pc': round(pc, 4) if pc else None,
        'open': round(float(c[0]), 4), 'close': round(float(c[-1]), 4),
        'high': round(float(c.max()), 4), 'low': round(float(c.min()), 4),
        'day_chg_pct': round(float(day_chg), 3),
        'range_pct': round(float(rng), 3),
        'vwap_last': round(float(vwap[-1]), 4),
        'atr_mean': round(float(np.mean(atr[atr > 0])), 5),
        'atr_last': round(float(atr[-1]), 5),
        'dev_min_pct': round(float(dev.min()), 3),
        'dev_max_pct': round(float(dev.max()), 3),
        'dev_last_pct': round(float(dev[-1]), 3),
        'band_b_dev_pct': round(float(np.mean(band_b)), 3) if band_b else None,  # 触发B所需 dev 上限(均值)
        'band_s_dev_pct': round(float(np.mean(band_s)), 3) if band_s else None,
        'closest_b_gap_pct': round(float(closest_b_gap), 3) if closest_b_gap != 1e9 else None,
        'closest_s_gap_pct': round(float(closest_s_gap), 3) if closest_s_gap != -1e9 else None,
        'n_gravity_b': n_grav_b, 'n_gravity_s': n_grav_s,
        'n_macd_b': n_macd_b, 'n_macd_s': n_macd_s,
        'n_early_gravity': n_early_grav,
        'n_blocked_by_macd_b': n_blocked_b, 'n_blocked_by_macd_s': n_blocked_s,
        'n_strict_raw_b': n_strict_b, 'n_strict_raw_s': n_strict_s,
        'n_signals': len(sigs),
        'signals': [{'type': s['type'], 't': str(df['trade_time'].iloc[s['idx']]),
                     'price': s['price'], 'chg': s['chg'], 'detail': s['detail']} for s in sigs],
        'near_misses': near_misses,
        'tencent_fallback': bool(tencent_like),
        'flat_bars': flat,
        'vol_sum': round(float(np.sum(df['volume'].values.astype(float))), 2),
    }

results = {}
print("=" * 70)
print("LOADING & ANALYZING 161129.SZ")
df_t, data_t, pc_t = load_day(TODAY, today=True)
if df_t is None:
    print("!! 今日数据获取失败")
    sys.exit(1)
results[TODAY] = analyze(TODAY, df_t, data_t, pc_t, today=True)
r = results[TODAY]
print(f"\n[今日 {TODAY}] bars={r['n_bars']} {r['time_start']}->{r['time_end']}  PC={r['pc']}")
print(f"  开/高/低/收={r['open']}/{r['high']}/{r['low']}/{r['close']}  当日涨跌={r['day_chg_pct']}%  振幅={r['range_pct']}%")
print(f"  VWAP末={r['vwap_last']}  ATR均值={r['atr_mean']}  ATR末={r['atr_last']}")
print(f"  dev范围=[{r['dev_min_pct']}%, {r['dev_max_pct']}%]  dev末={r['dev_last_pct']}%")
print(f"  触发带(均值): B需dev<={r['band_b_dev_pct']}%  S需dev>={r['band_s_dev_pct']}%")
print(f"  gravity触发bar: B={r['n_gravity_b']} S={r['n_gravity_s']} (早盘i<{LOCAL_W}: {r['n_early_gravity']})")
print(f"  MACD因子bar: B={r['n_macd_b']} S={r['n_macd_s']}")
print(f"  strict门控raw触发: B={r['n_strict_raw_b']} S={r['n_strict_raw_s']}")
print(f"  gravity触发但被MACD挡掉: B={r['n_blocked_by_macd_b']} S={r['n_blocked_by_macd_s']}")
print(f"  实际发出信号: {r['n_signals']}")
print(f"  近失(gravity触发/MACD未配合): {json.dumps(r['near_misses'], ensure_ascii=False)}")
print(f"  数据源: {'腾讯兜底(合成OHLC)' if r['tencent_fallback'] else 'mootdx原生'}  平盘bar={r['flat_bars']}/{r['n_bars']}  量总和={r['vol_sum']}")

for day in COMPARE_DAYS:
    df, data, pc = load_day(day)
    if df is None:
        print(f"\n[对比日 {day}] 数据缺失, 跳过")
        continue
    results[day] = analyze(day, df, data, pc)
    rr = results[day]
    print(f"\n[对比日 {day}] bars={rr['n_bars']} {rr['time_start']}->{rr['time_end']}  PC={rr['pc']}")
    print(f"  开/高/低/收={rr['open']}/{rr['high']}/{rr['low']}/{rr['close']}  当日涨跌={rr['day_chg_pct']}%  振幅={rr['range_pct']}%")
    print(f"  dev范围=[{rr['dev_min_pct']}%, {rr['dev_max_pct']}%]")
    print(f"  gravity触发bar: B={rr['n_gravity_b']} S={rr['n_gravity_s']}  MACD因子: B={rr['n_macd_b']} S={rr['n_macd_s']}")
    print(f"  strict门控raw触发: B={rr['n_strict_raw_b']} S={rr['n_strict_raw_s']}  实际信号={rr['n_signals']}")
    print(f"  信号明细: {json.dumps(rr['signals'], ensure_ascii=False)}")

with open('output/161129_0721_review.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n[done] -> output/161129_0721_review.json")
