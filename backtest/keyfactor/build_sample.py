#!/usr/bin/env python3
"""
Phase 0 — 从 universe_pool.csv 分层抽样 250 只 (大/中/小盘 + 板块分层代理)
落地: backtest/keyfactor_data/sample_manifest.csv
分层键: (market_label, board) 作为市值/板块代理 (无需额外市值接口)。
配额: 按各层样本量比例分配, 保证每层>=1 (若层非空), 总数=250, 可复现(seed=42)。
"""
import sys, os
import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'keyfactor_data')
POOL = os.path.join(DATA, 'universe_pool.csv')
OUT = os.path.join(DATA, 'sample_manifest.csv')
N = 250
SEED = 42

def proportional_quota(sizes, n):
    """按 sizes 比例分配总数 n, 每层至少1(若 size>=1), 返回各层配额(list)。"""
    total = sum(sizes)
    quotas = [max(1, round(n * s / total)) if s >= 1 else 0 for s in sizes]
    # 校正到总和=n: 先处理超额(从最大的层减), 再处理不足(向最大的层加)
    diff = n - sum(quotas)
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i])
    idx = 0
    while diff != 0:
        i = order[idx % len(order)]
        if diff > 0:
            if quotas[i] < sizes[i]:
                quotas[i] += 1; diff -= 1
        else:  # diff < 0
            if quotas[i] > 1:
                quotas[i] -= 1; diff += 1
        idx += 1
        if idx > 100000:
            break
    return quotas

def main():
    if not os.path.exists(POOL):
        print(f"⚠️ 未找到 {POOL}, 请先跑 universe.py")
        return
    df = pd.read_csv(POOL, dtype={'code': str})
    pool = df[(~df['is_st']) & (~df['is_b_share'])].copy().reset_index(drop=True)
    # 分层键
    pool['stratum'] = pool['market_label'].astype(str) + '|' + pool['board'].astype(str)
    strata = pool['stratum'].drop_duplicates().tolist()
    sizes = [int((pool['stratum'] == s).sum()) for s in strata]
    quotas = proportional_quota(sizes, N)
    rng = np.random.default_rng(SEED)
    picked = []
    for s, q in zip(strata, quotas):
        sub = pool[pool['stratum'] == s]
        take = min(q, len(sub))
        idx = rng.choice(sub.index.values, size=take, replace=False)
        picked.append(sub.loc[idx])
    sample = pd.concat(picked).drop_duplicates(subset=['code']).reset_index(drop=True)
    # 若因去重/取整不足250, 从剩余池补足
    if len(sample) < N:
        remain = pool[~pool['code'].isin(sample['code'])].index.values
        extra = rng.choice(remain, size=min(N - len(sample), len(remain)), replace=False)
        sample = pd.concat([sample, pool.loc[extra]]).reset_index(drop=True)
    sample = sample.sort_values(['market_label', 'code']).reset_index(drop=True)
    sample.to_csv(OUT, index=False, encoding='utf-8-sig')
    print(f"=== 抽样完成: {len(sample)} 只 (目标 {N}) ===")
    print(f"分市场: {sample['market_label'].value_counts().to_dict()}")
    print(f"分板块: {sample['board'].value_counts().to_dict()}")
    print(f"分层配额: " + ", ".join(f"{s.split('|')[1]}={q}" for s, q in zip(strata, quotas)))
    print(f"落地: {OUT}")
    print("\n前10只预览:")
    for _, r in sample.head(10).iterrows():
        print(f"  {r['sym']:12s} {r['name']:10s} [{r['board']}]")

if __name__ == '__main__':
    main()
