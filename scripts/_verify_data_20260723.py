#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时核验脚本：重拉三标的今日1m，检查时间戳连续性与数据完整性。"""
import sys, os
sys.path.insert(0, r'C:/Users/YZP/WorkBuddy/Claw/tpoint')
import pandas as pd
import numpy as np
from core.datasource import MootdxDataSource

SYMS = ['161129.SZ', '688347.SH', '513310.SH']
ds = MootdxDataSource()

def expected_minutes():
    """A股1m 预期分钟集合：上午 09:31-11:30, 下午 13:01-15:00 (共240根)。"""
    mins = []
    for h in range(9, 12):
        for m in range(0, 60):
            if (h == 9 and m < 31) or (h == 11 and m > 30):
                continue
            mins.append((h, m))
    for h in range(13, 16):
        for m in range(0, 60):
            if (h == 13 and m == 0) or (h == 15 and m > 0):
                continue  # 13:00 是午休, 不交易
            mins.append((h, m))
    return set(mins)

EXP = expected_minutes()
print(f'预期交易分钟数(无午休缺口): {len(EXP)}')

for sym in SYMS:
    print('\n' + '=' * 60)
    print(f'标的 {sym}')
    try:
        df = ds.intraday(sym)
    except Exception as e:
        print(f'  ERROR: {e}')
        continue
    if df is None or len(df) == 0:
        print('  NO DATA (mootdx+腾讯均无)')
        continue
    n = len(df)
    print(f'  实际棒数: {n}')
    print(f'  列: {list(df.columns)}')
    # 时间戳解析
    tt = df['trade_time']
    print(f'  trade_time dtype: {tt.dtype}')
    print(f'  head: {tt.head(2).tolist()}')
    print(f'  tail: {tt.tail(2).tolist()}')
    # 提取 (h,m)
    try:
        dt = pd.to_datetime(tt, errors='coerce')
    except Exception:
        dt = tt
    hm = set()
    has_real_time = False
    for t in dt:
        if pd.isna(t):
            continue
        hm.add((t.hour, t.minute))
        if not (t.hour == 0 and t.minute == 0):
            has_real_time = True
    if not has_real_time:
        print('  ⚠️ trade_time 为日期级(无真实时分) — 仅能用棒数推断连续性')
        # 用棒数推断
        diff = len(EXP) - n
        print(f'  棒数差(预期-实际): {diff}  (若>0 表示可能缺失约{diff}分钟)')
        continue
    missing = sorted(EXP - hm)
    extra = sorted(hm - EXP)
    print(f'  缺失分钟数: {len(missing)}')
    if missing:
        # 按上午/下午分组打印
        am = [f'{h:02d}:{m:02d}' for h,m in missing if h < 12]
        pm = [f'{h:02d}:{m:02d}' for h,m in missing if h >= 12]
        if am: print(f'    上午缺失: {am[:40]}{"..." if len(am)>40 else ""}')
        if pm: print(f'    下午缺失: {pm[:40]}{"..." if len(pm)>40 else ""}')
    if extra:
        print(f'  非交易时段异常分钟: {extra[:20]}')
    # 排序后相邻间隔检查（捕捉非整分钟的错位）
    seq = sorted(hm)
    print(f'  首棒: {seq[0][0]:02d}:{seq[0][1]:02d}  末棒: {seq[-1][0]:02d}:{seq[-1][1]:02d}')
    print(f'  连续性结论: {"✅ 完整无缺口" if not missing and not extra else "❌ 存在缺口/异常"}')
