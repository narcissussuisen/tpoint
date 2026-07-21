#!/usr/bin/env python3
"""
Phase 0 (补) — 在现有 manifest 基础上, 追加 N 只「不重复」的有效 A股代码空间抽样,
把有效落地数从 ~165 顶到 >=250 (no-failure-mode)。
落地: 原地追加回 sample_manifest.csv (保留既有顺序, 新码接在末尾)。
"""
import os, random
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

OUT = os.path.join(DATA, 'sample_manifest.csv')
N_ADD = 400
SEED = 42

SZ_PREF = ['000', '001', '002', '003', '300', '301']
SH_PREF = ['600', '601', '603', '605', '688', '689']

def board_of(code):
    if code.startswith(('300', '301')): return '创业板'
    if code.startswith(('002', '003')): return '中小板'
    if code.startswith(('000', '001')): return '深主板'
    if code.startswith('688'): return '科创板'
    if code.startswith('689'): return '科创板'
    if code.startswith('60'): return '沪主板'
    return '其他'

def main():
    man = pd.read_csv(OUT, dtype={'code': str})
    existing = set(man['code'].tolist())
    print(f"现有 manifest: {len(man)} 只, 已用代码 {len(existing)}")

    # 全代码空间
    all_codes = []
    for p in SZ_PREF + SH_PREF:
        for i in range(1000):
            all_codes.append(p + f'{i:03d}')
    pool = [c for c in all_codes if c not in existing]

    rng = random.Random(SEED + 1)  # 与首轮不同的子种子, 避免重叠
    rng.shuffle(pool)
    picked = pool[:N_ADD]
    new_rows = []
    for code in picked:
        mkt = 'SZ' if code.startswith(tuple(SZ_PREF)) else 'SH'
        sym = f'{code}.{mkt}'
        new_rows.append({'code': code, 'sym': sym, 'name': sym,
                        'market': 0 if mkt == 'SZ' else 1,
                        'market_label': mkt, 'board': board_of(code)})
    new_df = pd.DataFrame(new_rows)
    merged = pd.concat([man, new_df], ignore_index=True)
    merged.to_csv(OUT, index=False, encoding='utf-8-sig')
    print(f"追加 {len(new_df)} 只不重复代码 -> manifest 现 {len(merged)} 只")
    print(f"  新增板块分布: {new_df['board'].value_counts().to_dict()}")

if __name__ == '__main__':
    main()
