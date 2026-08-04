#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""rb_oscillator_eval.py — R-B：高低点反转因子离线验证（0805 迭代 · 效果说话）

背景：T+0 双标的（161129/513310）有波动零信号，用户点名「简单 RSI/KDJ 都能提示阶段高低点」。
本脚本把 RSI/KDJ 反转作为**独立触发源**在 F盘全历史上离线重放，用数据回答「值不值得入库」。

因子变体（1m K线，信号 bar close 进场，禁前视）：
  RSI(14)：RSI 上穿 20 → B；下穿 80 → S（极端反转）
  RSI(14) 宽松：上穿 30 → B；下穿 70 → S
  KDJ(9,3,3)：J 值由负上拐（J<0→回升）→ B；J>100 下拐 → S
出场：simulate_day 生产出场（trail 0.4/0.6 + S信号 + EOD），成本=万一+印花+滑点2bps/边
频控：同方向冷却 3 bar、单仓位（与生产一致）
指标：净胜率/盈亏比/总收益/最大回撤/夏普/日均信号数（用户七项口径）
标的：5 只 watchlist 全跑（重点看零信号双 ETF）
产出：output/rb_oscillator_<date>.json
"""
import os, sys, json, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np
import factor_optimizer as FO
from exit_manager import aggregate_metrics

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))
COOLDOWN = 3


def rsi14(c):
    delta = np.diff(c, prepend=c[0])
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    ru = np.convolve(up, np.ones(14) / 14, 'same')
    rd = np.convolve(dn, np.ones(14) / 14, 'same')
    rs = np.where(rd > 0, ru / rd, 100.0)
    return 100 - 100 / (1 + rs)


def kdj(c, h, lo, n=9):
    k = np.zeros(len(c)); d = np.zeros(len(c))
    k[0] = d[0] = 50.0
    for i in range(1, len(c)):
        lo_n = lo[max(0, i - n + 1):i + 1].min()
        hi_n = h[max(0, i - n + 1):i + 1].max()
        rsv = (c[i] - lo_n) / (hi_n - lo_n) * 100 if hi_n > lo_n else 50.0
        k[i] = 2 / 3 * k[i - 1] + 1 / 3 * rsv
        d[i] = 2 / 3 * d[i - 1] + 1 / 3 * k[i]
    return 3 * k - 2 * d   # J


def gen_sigs(data, variant):
    """反转信号 → simulate_day 输入 [{'type','idx','price','reason'}]。单仓位+3bar冷却。"""
    c = data['c']; n = data['n']
    h, lo = data['h'], data['lo']
    out = []
    last_b = last_s = -999
    if variant.startswith('rsi'):
        thr_b, thr_s = (20, 80) if variant == 'rsi_20_80' else (30, 70)
        r = rsi14(c)
        for i in range(15, n):
            if r[i - 1] < thr_b <= r[i] and i - last_b > COOLDOWN:      # 超卖回升 → B
                out.append({'type': 'B', 'idx': i, 'price': float(c[i]), 'reason': f'RSI上穿{thr_b}'})
                last_b = i
            elif r[i - 1] > thr_s >= r[i] and i - last_s > COOLDOWN:    # 超买回落 → S
                out.append({'type': 'S', 'idx': i, 'price': float(c[i]), 'reason': f'RSI下穿{thr_s}'})
                last_s = i
    else:  # kdj
        j = kdj(c, h, lo)
        for i in range(10, n):
            if j[i - 1] < 0 <= j[i] and i - last_b > COOLDOWN:          # J 负值上拐 → B
                out.append({'type': 'B', 'idx': i, 'price': float(c[i]), 'reason': 'KDJ_J<0上拐'})
                last_b = i
            elif j[i - 1] > 100 >= j[i] and i - last_s > COOLDOWN:      # J>100 下拐 → S
                out.append({'type': 'S', 'idx': i, 'price': float(c[i]), 'reason': 'KDJ_J>100下拐'})
                last_s = i
    return out


def main():
    date = datetime.date.today().strftime('%Y-%m-%d')
    variants = ['rsi_20_80', 'rsi_30_70', 'kdj_j']
    rep = {'date': date, 'variants': variants,
           'note': '反转因子独立触发源离线重放；出场=生产trail0.4/0.6+S+EOD；成本生产口径', 'symbols': {}}
    for sym, name in wl.items():
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            rep['symbols'][sym] = {'error': str(e)}
            continue
        sres = {}
        for v in variants:
            all_trips, n_sig = [], 0
            for d, data, g in days:
                data['sym'] = sym
                sigs = gen_sigs(data, v)
                n_sig += len(sigs)
                all_trips.extend(FO.eval_config([(d, data, sigs)], *FO.CUR_TRAIL))
            m = aggregate_metrics(all_trips)
            sres[v] = {'n_trips': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
                       'total_ret': m.get('total_ret_pct', 0), 'max_dd': m.get('max_drawdown_pct'),
                       'sharpe': m.get('sharpe'), 'sig_per_day': round(n_sig / max(len(days), 1), 2)}
        rep['symbols'][sym] = {'name': name, 'n_days': len(days), 'results': sres}
        print(f"[{sym} {name}] " + ' | '.join(
            f"{v}: sig/d={sres[v]['sig_per_day']} wr={sres[v]['win_rate']}% pl={sres[v]['pl_ratio']}"
            for v in variants), flush=True)
    out = os.path.join(ROOT, 'output', f'rb_oscillator_{date}.json')
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'[ok] {out}')


if __name__ == '__main__':
    main()
