#!/usr/bin/env python3
"""
Phase 0 — 从「有效 A股 代码空间」直接随机抽样 (绕开 mootdx 脆弱的证券列表枚举)。
  有效前缀 (真实 A股 股票代码段):
    SZ: 000/001/002/003 (主板/中小板) 300/301 (创业板)
    SH: 600/601/603/605 (主板) 688/689 (科创板)
  每个前缀 000-999 全生成 -> 全代码空间 -> 均匀随机抽 N=400 (种子42)。
  说明: 这是 A股代码空间的均匀随机样本 ≈ universe 随机抽样; 下载器会自动跳过
        退市/不存在代码(返回空), 故「落地成功」的集合即干净 A股非ST 样本。
  落地: KEYFACTOR_DATA_DIR（默认 F:\workbuddy\keyfactor_data）/sample_manifest.csv (含 code/sym/name/market/board)
"""
import os, random
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

OUT = os.path.join(DATA, 'sample_manifest.csv')
N = 400
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
    cands = []
    for p in SZ_PREF + SH_PREF:
        for i in range(1000):
            cands.append(p + f'{i:03d}')
    rng = random.Random(SEED)
    rng.shuffle(cands)
    picked = cands[:N]
    rows = []
    for code in picked:
        mkt = 'SZ' if code.startswith(tuple(SZ_PREF)) else 'SH'
        sym = f'{code}.{mkt}'
        rows.append({'code': code, 'sym': sym, 'name': sym,
                    'market': 0 if mkt == 'SZ' else 1,
                    'market_label': mkt, 'board': board_of(code)})
    df = pd.DataFrame(rows).sort_values(['market_label', 'code']).reset_index(drop=True)
    df.to_csv(OUT, index=False, encoding='utf-8-sig')
    print(f"=== 代码空间随机抽样: {len(df)} 只 (种子 {SEED}) ===")
    print(f"  分市场: {df['market_label'].value_counts().to_dict()}")
    print(f"  分板块: {df['board'].value_counts().to_dict()}")
    print(f"  落地: {OUT}")
    print("\n前 12 只预览:")
    for _, r in df.head(12).iterrows():
        print(f"  {r['sym']:12s} [{r['board']}]")

if __name__ == '__main__':
    main()
