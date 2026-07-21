#!/usr/env python3
"""
Phase 0 — 枚举大A股非ST universe。
数据源: mootdx 原生 `cli.client.get_security_list(market, start)` (StdQuotes 无此方法, 必须用裸 pytdx 客户端)。
  - 每页 1000 条, 需分页 (start=0,1000,2000... 直到 <1000 或空)。
  - 列表混入「板块表头」(主板Ａ股/创业板...) 与「指数」(999999 上证指数/000001...), 须过滤。
落地: KEYFACTOR_DATA_DIR（默认 F:\workbuddy\keyfactor_data）/universe_pool.csv
列: code,name,market,market_label,sym,board,is_b_share,is_st
"""
import sys, os, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'core'))
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

from datasource import tdx_client
import pandas as pd

OUT_DIR = KEYFACTOR_DATA_DIR
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, 'universe_pool.csv')
MKT_LABEL = {0: 'SZ', 1: 'SH', 2: 'BJ'}
HEADER_WORDS = ('指数', 'Ａ股', 'B股', '基金', '债', '货', '板', '申', '购', '赎', 'CDR', 'ETF', 'LOF')
# 正向白名单: 仅保留真实 A股 股票代码段 (其余基金/债券/指数/B股 一律丢弃)
ALLOW = re.compile(r'^(000|001|002|003|300|301|600|601|603|605|688|689|8\d{5}|4\d{5})$')

def keep(code, name):
    c = str(code).strip()
    if not ALLOW.match(c):
        return False
    n = str(name)
    if any(w in n for w in HEADER_WORDS):
        return False
    return True

def classify(code, market):
    c = str(code)
    if market == 0:  # 深圳
        if c.startswith(('300', '301')): return '创业板', False
        if c.startswith(('002', '003')): return '中小板', False
        if c.startswith(('000', '001')): return '深主板', False
        if c.startswith('200'): return '深B', True
        return '深其他', False
    if market == 1:  # 上海
        if c.startswith('688'): return '科创板', False
        if c.startswith('689'): return '科创板', False
        if c.startswith('60'): return '沪主板', False
        if c.startswith('900'): return '沪B', True
        return '沪其他', False
    if market == 2:  # 北交所
        return '北交所', False
    return '未知', False

def is_st(name):
    n = str(name)
    return ('ST' in n.upper()) or ('*' in n) or ('退' in n)

def page(cli, market):
    rows = []
    start = 0
    while True:
        try:
            part = cli.client.get_security_list(market, start)
        except Exception as e:
            print(f"  ⚠️ market={market} start={start} 异常: {e}")
            break
        if not part:
            break
        rows.extend(part)
        if len(part) < 1000:
            break
        start += 1000
    return rows

def sym_of(code, market):
    return f"{code}.{MKT_LABEL.get(market, '??')}"

def main():
    print("=== 枚举 universe (mootdx 原生 get_security_list, 分页1000) ===")
    cli = tdx_client()
    all_rows = []
    for mkt in (0, 1, 2):
        raw = page(cli, mkt)
        n_raw = len(raw)
        kept = 0
        for r in raw:
            code = str(r.get('code', '')).strip()
            name = str(r.get('name', '')).strip()
            if not keep(code, name):
                continue
            board, is_b = classify(code, mkt)
            all_rows.append({
                'code': code, 'name': name, 'market': mkt,
                'market_label': MKT_LABEL[mkt], 'sym': sym_of(code, mkt),
                'board': board, 'is_b_share': is_b, 'is_st': is_st(name),
            })
            kept += 1
        print(f"  market={mkt} ({MKT_LABEL[mkt]}): 原始 {n_raw} → 过滤后 {kept}")
    df = pd.DataFrame(all_rows)
    if len(df):
        df = df.drop_duplicates(subset=['code']).reset_index(drop=True)
    df.to_csv(OUT, index=False, encoding='utf-8-sig')
    n_total = len(df)
    n_st = int(df['is_st'].sum()) if n_total else 0
    n_b = int(df['is_b_share'].sum()) if n_total else 0
    a_nonst = df[(~df['is_st']) & (~df['is_b_share'])] if n_total else df
    print(f"\n=== 枚举完成 ===")
    print(f"  总标的数: {n_total}")
    print(f"  ST: {n_st}  B股: {n_b}")
    print(f"  A股非ST (抽样池): {len(a_nonst)}")
    if len(a_nonst):
        print(f"  分市场: {a_nonst['market_label'].value_counts().to_dict()}")
        print(f"  分板块: {a_nonst['board'].value_counts().to_dict()}")
    print(f"  落地: {OUT}")

if __name__ == '__main__':
    main()
