#!/usr/bin/env python3
"""universe 快速版: 每 market 单次 get_security_list(不带分页循环), 受调用方 timeout 约束。
目的: 在 mootdx 网络不稳时也能拿到可用抽样框(即便非全量)。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from datasource import tdx_client
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'keyfactor_data')
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, 'universe_pool.csv')
MKT_LABEL = {0: 'SZ', 1: 'SH', 2: 'BJ'}

def classify(code, market):
    c = str(code)
    if market == 0:
        if c.startswith(('300', '301')): return '创业板'
        if c.startswith(('002', '003')): return '中小板'
        if c.startswith(('000', '001')): return '深主板'
        if c.startswith('200'): return '深B'
        return '深其他'
    if market == 1:
        if c.startswith('688'): return '科创板'
        if c.startswith('60'): return '沪主板'
        if c.startswith('900'): return '沪B'
        return '沪其他'
    return '北交所'

def is_st(name):
    n = str(name)
    return ('ST' in n.upper()) or ('*' in n) or ('退' in n)

def main():
    print("=== universe 快速枚举 (单次/市场, 受 timeout) ===")
    cli = tdx_client()
    rows = []
    for mkt in (0, 1, 2):
        try:
            part = cli.get_security_list(market=mkt, start=0)
        except Exception as e:
            print(f"  ⚠️ market={mkt} 异常: {e}")
            part = None
        if not part:
            print(f"  market={mkt}: 空")
            continue
        print(f"  market={mkt} ({MKT_LABEL[mkt]}): {len(part)} 条 (单次)")
        for r in part:
            code = str(r.get('code', '')).strip()
            name = str(r.get('name', '')).strip()
            if not code:
                continue
            rows.append({'code': code, 'name': name, 'market': mkt,
                         'market_label': MKT_LABEL[mkt],
                         'sym': f"{code}.{MKT_LABEL[mkt]}",
                         'board': classify(code, mkt),
                         'is_b_share': code.startswith(('200', '900')),
                         'is_st': is_st(name)})
    df = pd.DataFrame(rows).drop_duplicates(subset=['code']).reset_index(drop=True)
    df.to_csv(OUT, index=False, encoding='utf-8-sig')
    a = df[(~df['is_st']) & (~df['is_b_share'])]
    print(f"\n  总={len(df)} ST={int(df['is_st'].sum())} B股={int(df['is_b_share'].sum())}")
    print(f"  A股非ST(抽样框)={len(a)}  分市场={a['market_label'].value_counts().to_dict()}")
    print(f"  落地: {OUT}")

if __name__ == '__main__':
    main()
