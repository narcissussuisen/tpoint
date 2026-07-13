#!/usr/bin/env python3
"""
v9 回测脚本 — 部署到openclaw服务器运行, 对比v8/v9命中率
算法层走 v9_indicators (与monitor_v9/selftest一致)
数据源: tickflow (服务器已配置)
用法: python backtest_v9.py [天数]   默认回测最近5个交易日
注意: intraday接口对历史日期的支持取决于tickflow实现, 若仅返回当天则回测当日
"""
import sys, os, json, random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from datasource import MootdxDataSource as TickFlow
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from v9_indicators import compute_indicators, detect_signals

CST = timezone(timedelta(hours=8))
tf = TickFlow()

TARGETS = {
    '300975.SZ': '商络电子',
    '601869.SH': '长飞光纤',
    '603938.SH': '三孚股份',
    '300395.SZ': '菲利华',
    '301526.SZ': '国际复材',
    '300757.SZ': '罗博特科',
    '688820.SH': '盛合晶微',
}


def get_pc(sym, date_str):
    """取date_str前一交易日收盘价"""
    d = tf.klines.get(sym, period='1d', count=15, as_dataframe=True).sort_values('trade_date')
    for _, row in d.iterrows():
        if str(row['trade_date'])[:10] == date_str:
            idx = d.index.get_loc(row.name)
            return float(d['close'].iloc[idx-1]) if idx > 0 else float(row['close'])
    return float(d['close'].iloc[-2]) if len(d) >= 2 else float(d['close'].iloc[-1])


def get_close(sym, date_str):
    """取date_str当日收盘价"""
    d = tf.klines.get(sym, period='1d', count=15, as_dataframe=True).sort_values('trade_date')
    for _, row in d.iterrows():
        if str(row['trade_date'])[:10] == date_str:
            return float(row['close']), float(row['open'])
    return 0.0, 0.0


def compute_v9_signals(sym, name, date_str):
    """v9: VWAP+ATR+趋势+量价+温度"""
    df = tf.klines.intraday(sym, as_dataframe=True)
    if df is None or len(df) < 5:
        return []
    df = df.sort_values('trade_time').reset_index(drop=True)
    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o = df['open'].values.astype(float) if 'open' in df.columns else c.copy()
    has_vol = 'volume' in df.columns
    v = df['volume'].values.astype(float) if has_vol else None
    pc = get_pc(sym, date_str)
    data = compute_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    sigs = detect_signals(data, pc)
    for s in sigs:
        s['name'] = name
        s['time'] = str(df['trade_time'].iloc[s['idx']])[11:16]
    return sigs


def compute_v8_signals(sym, name, date_str):
    """v8 LONGCROSS 对比基准"""
    df = tf.klines.intraday(sym, as_dataframe=True)
    if df is None or len(df) < 5:
        return []
    df = df.sort_values('trade_time').reset_index(drop=True)
    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    pc = get_pc(sym, date_str)
    n = len(c)
    eh = np.maximum.accumulate(h)
    el = np.minimum.accumulate(lo)
    g1 = np.maximum(pc, eh)
    g2 = np.minimum(pc, el)
    g3 = g1 - g2
    sup = g2 + g3 * 0.5 / 9
    res = g2 + g3 * 8.0 / 9
    sigs = []
    for i in range(2, n):
        if c[i-2] <= sup[i-2] and c[i-1] <= sup[i-1] and c[i] > sup[i]:
            sigs.append({'type': 'B', 'idx': i, 'price': round(float(c[i]), 2),
                         'time': str(df['trade_time'].iloc[i])[11:16], 'name': name})
        if c[i-2] >= res[i-2] and c[i-1] >= res[i-1] and c[i] < res[i]:
            sigs.append({'type': 'S', 'idx': i, 'price': round(float(c[i]), 2),
                         'time': str(df['trade_time'].iloc[i])[11:16], 'name': name})
    return sigs


def calc_accuracy(signals, close_price):
    """命中率: B需收盘>入场, S需收盘<入场"""
    hb = sb = tb = 0; hs = ss = ts = 0
    for s in signals:
        if s['type'] == 'B':
            tb += 1
            if close_price > s['price']: hb += 1
        else:
            ts += 1
            if close_price < s['price']: hs += 1
    return {
        'B': f"{hb}/{tb}={hb/tb*100:.0f}%" if tb else "0/0=-",
        'S': f"{hs}/{ts}={hs/ts*100:.0f}%" if ts else "0/0=-",
        'overall': f"{hb+hs}/{tb+ts}={(hb+hs)/(tb+ts)*100:.0f}%" if (tb+ts) else "0/0=-",
        'total': tb + ts, 'hits': hb + hs,
    }


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    random.seed(42)
    print("=" * 72)
    print("📊 v8 vs v9 回测对比 (VWAP+ATR+趋势+量价+温度)")
    print("=" * 72)

    # 取最近N个交易日
    d = tf.klines.get('300975.SZ', period='1d', count=days+10, as_dataframe=True).sort_values('trade_date')
    all_dates = [str(d['trade_date'].iloc[i])[:10] for i in range(len(d))]
    test_dates = [dt for dt in all_dates if datetime.strptime(dt, '%Y-%m-%d').weekday() < 5][-days:]
    test_stocks = list(TARGETS.items())

    print(f"📅 回测日期: {test_dates}")
    print(f"📈 标的: {list(TARGETS.values())}\n")

    agg_v8 = defaultdict(lambda: [0, 0])  # [hits, total]
    agg_v9 = defaultdict(lambda: [0, 0])
    detail = []

    for sym, name in test_stocks:
        for date_str in test_dates:
            try:
                close_p, _ = get_close(sym, date_str)
                if close_p == 0:
                    continue
                v8 = compute_v8_signals(sym, name, date_str)
                v9 = compute_v9_signals(sym, name, date_str)
                v8a = calc_accuracy(v8, close_p)
                v9a = calc_accuracy(v9, close_p)
                agg_v8['B'][1] += int(v8a['B'].split('/')[1].split('=')[0]) if '/0=' not in v8a['B'] else 0
                # 简化: 直接用hits/total
                for s in v8:
                    k = s['type']
                    agg_v8[k][1] += 1
                    if (k == 'B' and close_p > s['price']) or (k == 'S' and close_p < s['price']):
                        agg_v8[k][0] += 1
                for s in v9:
                    k = s['type']
                    agg_v9[k][1] += 1
                    if (k == 'B' and close_p > s['price']) or (k == 'S' and close_p < s['price']):
                        agg_v9[k][0] += 1
                detail.append((f"{name}_{date_str}", v8a, v9a, len(v8), len(v9)))
            except Exception as e:
                print(f"  ⚠️ {name} {date_str}: {e}")

    print(f"{'='*72}\n逐标的逐日对比\n{'='*72}")
    for key, v8a, v9a, n8, n9 in detail:
        print(f"\n  {key}")
        print(f"  v8: {n8}信号  B {v8a['B']}  S {v8a['S']}  → {v8a['overall']}")
        print(f"  v9: {n9}信号  B {v9a['B']}  S {v9a['S']}  → {v9a['overall']}")

    print(f"\n{'='*72}\n📊 汇总\n{'='*72}")
    for k in ('B', 'S'):
        v8h, v8t = agg_v8[k]
        v9h, v9t = agg_v9[k]
        v8r = f"{v8h}/{v8t}={v8h/v8t*100:.0f}%" if v8t else "0/0"
        v9r = f"{v9h}/{v9t}={v9h/v9t*100:.0f}%" if v9t else "0/0"
        print(f"  {k}信号  v8: {v8r}  v9: {v9r}")
    v8t_all = sum(agg_v8[k][1] for k in ('B', 'S'))
    v8h_all = sum(agg_v8[k][0] for k in ('B', 'S'))
    v9t_all = sum(agg_v9[k][1] for k in ('B', 'S'))
    v9h_all = sum(agg_v9[k][0] for k in ('B', 'S'))
    print(f"\n  总命中  v8: {v8h_all}/{v8t_all}={v8h_all/v8t_all*100:.0f}%  "
          f"v9: {v9h_all}/{v9t_all}={v9h_all/v9t_all*100:.0f}%" if (v8t_all and v9t_all) else "  数据不足")
    print(f"  信号量  v8: {v8t_all}  v9: {v9t_all}  (v9应通过量价确认收敛)")


if __name__ == '__main__':
    main()
