#!/usr/bin/env python3
"""
tickflow 分钟级 v9 回测 — 真实验证(B路径)
tickflow klines.get(period='1m', count=5000) 可拉约1月分钟历史
按日切分跑v9(日内VWAP), 统计真实信号+命中率(T+30min/T+60min/到收盘)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import os
import numpy as np
import pandas as pd
from indicators import compute_indicators, detect_signals

def _load_targets():
    """动态加载标的：data/watchlist.json → backtest_data/ CSV 发现（单一真相源）。"""
    import json as _j, os as _o
    _p = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), '..', 'data', 'watchlist.json')
    try:
        if _o.path.exists(_p):
            with open(_p, encoding='utf-8') as _f:
                return _j.load(_f)
    except Exception:
        pass
    _bd = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), 'backtest_data')
    if _o.path.isdir(_bd):
        _t = {}
        for _fn in _o.listdir(_bd):
            if _fn.endswith('_1m.csv'):
                _s = _fn.replace('_1m.csv', '')
                _t[_s] = _s
        return _t
    return {}

TARGETS = _load_targets()


def backtest_minute(sym, name):
    """拉1月分钟历史, 按日切分跑v9, 返回信号列表(含命中率)"""
    # 读本地CSV(离线回测,不再调tickflow,省钱)
    fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data', f'{sym}_1m.csv')
    if not os.path.exists(fpath):
        return [], 0
    df = pd.read_csv(fpath)
    if len(df) < 240:
        return [], 0
    df = df.sort_values('trade_time').reset_index(drop=True)
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['trade_time'].dt.strftime('%Y-%m-%d')
    all_sigs = []
    n_days = df['date'].nunique()
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
        sigs = detect_signals(data, pc)
        n = len(day_df)
        for s in sigs:
            idx = s['idx']
            s['date'] = date
            s['time'] = str(day_df['trade_time'].iloc[idx])[11:16]
            s['name'] = name
            # 命中率: T+30min / T+60min / 到收盘
            f30 = min(idx + 30, n - 1)
            f60 = min(idx + 60, n - 1)
            fc = n - 1
            c30 = float(c[f30]); c60 = float(c[f60]); cc = float(c[fc])
            s['ret_30min'] = round((c30 - s['price']) / s['price'] * 100, 2)
            s['ret_60min'] = round((c60 - s['price']) / s['price'] * 100, 2)
            s['ret_close'] = round((cc - s['price']) / s['price'] * 100, 2)
            # B命中=涨, S命中=跌
            s['hit_30'] = (s['type'] == 'B' and c30 > s['price']) or (s['type'] == 'S' and c30 < s['price'])
            s['hit_60'] = (s['type'] == 'B' and c60 > s['price']) or (s['type'] == 'S' and c60 < s['price'])
            s['hit_close'] = (s['type'] == 'B' and cc > s['price']) or (s['type'] == 'S' and cc < s['price'])
            all_sigs.append(s)
    return all_sigs, n_days


def main():
    lines = []
    def p(s=''):
        print(s); lines.append(s)

    p("=" * 78)
    p("tickflow 分钟级 v9 回测 — 真实验证 (1月历史, 按日切分日内VWAP)")
    p("=" * 78)

    all_sigs = []
    p(f"\n{'标的':<10} {'交易日':>5} {'总信号':>5} {'B':>3} {'S':>3} {'B胜率30':>7} {'S胜率30':>7} {'B胜率60':>7} {'B收盘':>7} {'S收盘':>7}")
    p("-" * 78)
    for sym, name in TARGETS.items():
        try:
            sigs, n_days = backtest_minute(sym, name)
            if not sigs:
                p(f"{name:<10} {n_days:>5} 0信号")
                continue
            all_sigs.extend(sigs)
            b = [s for s in sigs if s['type'] == 'B']
            s_list = [s for s in sigs if s['type'] == 'S']
            b30 = sum(s['hit_30'] for s in b) / len(b) * 100 if b else 0
            s30 = sum(s['hit_30'] for s in s_list) / len(s_list) * 100 if s_list else 0
            b60 = sum(s['hit_60'] for s in b) / len(b) * 100 if b else 0
            bc = sum(s['hit_close'] for s in b) / len(b) * 100 if b else 0
            sc = sum(s['hit_close'] for s in s_list) / len(s_list) * 100 if s_list else 0
            p(f"{name:<10} {n_days:>5} {len(sigs):>5} {len(b):>3} {len(s_list):>3} {b30:>6.0f}% {s30:>6.0f}% {b60:>6.0f}% {bc:>6.0f}% {sc:>6.0f}%")
        except Exception as e:
            p(f"{name:<10} 异常: {e}")

    # 汇总
    p(f"\n{'='*78}\n汇总 ({len(all_sigs)}信号)")
    p("=" * 78)
    if all_sigs:
        b = [s for s in all_sigs if s['type'] == 'B']
        s_list = [s for s in all_sigs if s['type'] == 'S']
        p(f"B信号: {len(b)}条")
        if b:
            p(f"  T+30min胜率: {sum(x['hit_30'] for x in b)/len(b)*100:.1f}% | 平均收益: {np.mean([x['ret_30min'] for x in b]):.2f}%")
            p(f"  T+60min胜率: {sum(x['hit_60'] for x in b)/len(b)*100:.1f}% | 平均收益: {np.mean([x['ret_60min'] for x in b]):.2f}%")
            p(f"  到收盘胜率:  {sum(x['hit_close'] for x in b)/len(b)*100:.1f}% | 平均收益: {np.mean([x['ret_close'] for x in b]):.2f}%")
        p(f"S信号: {len(s_list)}条")
        if s_list:
            p(f"  T+30min胜率: {sum(x['hit_30'] for x in s_list)/len(s_list)*100:.1f}% | 平均收益: {np.mean([x['ret_30min'] for x in s_list]):.2f}%")
            p(f"  T+60min胜率: {sum(x['hit_60'] for x in s_list)/len(s_list)*100:.1f}% | 平均收益: {np.mean([x['ret_60min'] for x in s_list]):.2f}%")
            p(f"  到收盘胜率:  {sum(x['hit_close'] for x in s_list)/len(s_list)*100:.1f}% | 平均收益: {np.mean([x['ret_close'] for x in s_list]):.2f}%")

        # 信号样本
        p(f"\n信号样本(前10):")
        for s in all_sigs[:10]:
            p(f"  [{s['date']} {s['time']}] {s['name']} {s['type']} 价={s['price']} 量比={s['vol_ratio']} 温度={s['temp']} {s['reason']} → 30min:{s['ret_30min']}% 60min:{s['ret_60min']}%")

    p(f"\n{'='*78}")
    p("注: 分钟级回测(日内VWAP), T+30/60min/收盘命中率。这是v9做T策略的真实验证。")
    p("=" * 78)

    # 写报告
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'research', 'v9-minute-backtest-report.md')
    with open(report, 'w', encoding='utf-8') as f:
        f.write("# v9 分钟级回测报告 (tickflow, 真实验证)\n\n")
        f.write(f"生成: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write("## 方法\n- tickflow klines.get(period='1m', count=5000) 拉约1月分钟历史\n- 按日切分跑v9(日内VWAP/ATR/趋势)\n- 命中率: B=T+30/60min/收盘价格上涨, S=下跌\n\n## 结果\n```\n" + '\n'.join(lines) + "\n```\n")
    print(f"\n📄 报告: {report}")


if __name__ == '__main__':
    main()
