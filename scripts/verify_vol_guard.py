# -*- coding: utf-8 -*-
"""
verify_vol_guard.py — P2-1 高波动守卫效果量化验证（轮次2-5 迭代交付）

对 07-30/31 高波动日（振幅 9.8%/9.4%，基线 5.6%）重跑 floor 门控信号，
A/B 对比"守卫开（HIGH_VOL_GUARD=1，地板/天花板×1.5）" vs "守卫关（=0）"：
  - 信号总数（B/S）变化
  - 失效信号数（向前验证无有利波动，即"接飞刀/卖飞"类）变化
  - 有效信号数变化
判定：守卫应减少"失效买入"（下跌日接飞刀）与"失效卖出"（上涨日卖飞），
     即 失效数下降 / 有效率提升 = 守卫有效。

用法：
  python scripts/verify_vol_guard.py [--date 2026-07-31]
"""
import argparse
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np  # noqa: E402
from datasource import MootdxDataSource  # noqa: E402
from miji_alpha import compute_miji_indicators, detect_miji_signals, HIGH_VOL_GUARD  # noqa: E402
import miji_alpha as ma  # noqa: E402
import monitor as M  # noqa: E402

VALID_THR = 0.15  # 与复盘一致


def load_day(ds, sym, day):
    """加载当日 1m 数据（mootdx 主源）+ 指标。"""
    df = ds.historical_1m(sym, day)
    if df is None or len(df) < 5:
        return None
    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o = df['open'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    # 昨收：用前一日收盘（简化，日K取）
    d = ds.klines.get(sym, period='1d', count=30)
    pc = None
    if d is not None and len(d):
        d = d.sort_values('trade_date').reset_index(drop=True)
        dates = list(d['trade_date'])
        if day in dates:
            i = dates.index(day)
            pc = float(d['close'].iloc[i - 1]) if i > 0 else float(d['close'].iloc[0])
    if pc is None or pc <= 0:
        pc = float(c[0])  # 兜底用首根
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=v is not None)
    data['df'] = df
    return data, pc


def evaluate(sigs, c, price_of):
    """向前验证：返回 (总信号, 有效, 失效, 有效列表)。B 有利=上涨, S 有利=下跌。"""
    total = len(sigs)
    valid = 0
    invalid = 0
    for s in sigs:
        i = s['idx']
        if i >= len(c) - 1:
            continue
        price = s['price']
        fwd = c[i + 1:]
        if s['type'] == 'B':
            best = (fwd.max() - price) / price * 100.0
        else:
            best = (price - fwd.min()) / price * 100.0
        if best > VALID_THR:
            valid += 1
        else:
            invalid += 1
    return total, valid, invalid


def evaluate_floor_only(sigs, c):
    """只统计由 floor 通道（价格地板/天花板）触发的信号，看守卫是否过滤掉浅层伪信号。
    floor 信号 = detail 含 '地板'/'天花板'。"""
    floor_sigs = [s for s in sigs if '地板' in s.get('detail', '') or '天花板' in s.get('detail', '')]
    total = len(floor_sigs)
    valid = 0
    for s in floor_sigs:
        i = s['idx']
        if i >= len(c) - 1:
            continue
        price = s['price']
        fwd = c[i + 1:]
        best = ((fwd.max() - price) / price * 100.0) if s['type'] == 'B' else ((price - fwd.min()) / price * 100.0)
        if best > VALID_THR:
            valid += 1
    return total, valid, total - valid


def main():
    ap = argparse.ArgumentParser(description='P2-1 高波动守卫 A/B 验证')
    ap.add_argument('--date', default='2026-07-31', help='验证日期（默认 07-31）')
    ap.add_argument('--syms', nargs='*', default=['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH'])
    args = ap.parse_args()

    ds = MootdxDataSource()
    print(f'═══ P2-1 高波动守卫 A/B 验证 · {args.date} ═══')
    print(f'{"标的":12s} {"守卫":4s} {"总信号":>6s} {"有效":>4s} {"失效":>4s} {"有效率":>8s} {"floor信号":>8s} {"floor有效":>8s} {"floor失效":>8s}')
    rows = []
    for sym in args.syms:
        loaded = load_day(ds, sym, args.date)
        if loaded is None:
            print(f'{sym:12s} 数据不可用')
            continue
        data, pc = loaded
        c = data['c']
        for guard in (True, False):
            ma.HIGH_VOL_GUARD = guard  # 切换守卫（模块级开关，detect_miji_signals 实时读取）
            sigs = detect_miji_signals(data, pc, macd_gate_mode='floor',
                                       b_trend_filter=False)
            total, valid, invalid = evaluate(sigs, c, None)
            ft, fv, fi = evaluate_floor_only(sigs, c)
            eff = valid / total * 100 if total else 0
            rows.append((sym, guard, total, valid, invalid, eff, ft, fv, fi))
            print(f'{sym:12s} {"开" if guard else "关":4s} {total:6d} {valid:4d} {invalid:4d} {eff:7.1f}% {ft:8d} {fv:8d} {fi:8d}')

    # 汇总判定：核心看 floor 通道（守卫直接作用域）的"失效信号"变化
    print('\n─── 汇总（floor 通道 = 守卫直接作用域） ───')
    on = [r for r in rows if r[1]]
    off = [r for r in rows if not r[1]]
    on_ft = sum(r[6] for r in on); on_fv = sum(r[7] for r in on); on_fi = sum(r[8] for r in on)
    off_ft = sum(r[6] for r in off); off_fv = sum(r[7] for r in off); off_fi = sum(r[8] for r in off)
    on_tot = sum(r[2] for r in on); off_tot = sum(r[2] for r in off)
    on_inv = sum(r[4] for r in on); off_inv = sum(r[4] for r in off)
    print(f'守卫开: floor信号 {on_ft}, floor有效 {on_fv}, floor失效 {on_fi}, '
          f'floor有效率 {(on_fv/on_ft*100) if on_ft else 0:.1f}%')
    print(f'守卫关: floor信号 {off_ft}, floor有效 {off_fv}, floor失效 {off_fi}, '
          f'floor有效率 {(off_fv/off_ft*100) if off_ft else 0:.1f}%')
    print(f'守卫效果: floor信号 {"↓" if on_ft < off_ft else "↑"} {off_ft-on_ft} 条, '
          f'floor失效 {"↓" if on_fi < off_fi else "↑"} {off_fi-on_fi} 条, '
          f'floor有效率 {"↑" if (on_fv/on_ft if on_ft else 0) > (off_fv/off_ft if off_ft else 0) else "↓"}')
    print(f'全通道: 总信号 {off_tot}→{on_tot}, 总失效 {off_inv}→{on_inv}, '
          f'总有效率 {(on_tot-on_inv)/on_tot*100 if on_tot else 0:.1f}% vs {(off_tot-off_inv)/off_tot*100 if off_tot else 0:.1f}%')
    ma.HIGH_VOL_GUARD = True  # 复原


if __name__ == '__main__':
    main()
