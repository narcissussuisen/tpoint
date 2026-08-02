# -*- coding: utf-8 -*-
"""fetch_turnover.py — 独立多进程换手率拉取（2026-08-02）

mootdx 逐只拉取可能挂起，独立成脚本 + 每只硬超时 + 多进程并行。
产出 data/t0_turnover.json: {sym: {turnover_pct, shares_source, error}}。
用法: python scripts/fetch_turnover.py [--procs 8]
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DATA_DIR = os.path.join(BASE, 'data')
F_DATA = r'F:\keyfactor_data\1m'

# T+0 全部 42 只 + watchlist 对照
WL_T0 = ['161129.SZ', '513310.SH']


def list_t0_symbols():
    out = []
    for fn in sorted(os.listdir(F_DATA)):
        if not fn.endswith('_1m.csv'):
            continue
        code = fn[:-len('_1m.csv')]
        if code.startswith(('1', '5')):
            out.append(code)
    return out


def _fetch_one(sym):
    """单只换手率，内部异常兜底。"""
    try:
        from core.datasource import MootdxDataSource
    except Exception:
        from datasource import MootdxDataSource
    try:
        ds = MootdxDataSource()
        df = ds.klines.get(sym, period='1d', count=30)
        if df is None or len(df) == 0:
            return sym, {'turnover_pct': None, 'error': 'no_daily_kline'}
        recent = df.tail(22)
        code = sym.split('.')[0]
        market = 0 if sym.endswith('.SZ') else 1
        fin = ds.client.finance(symbol=code, market=market)
        if fin is None or not len(fin):
            return sym, {'turnover_pct': None, 'error': 'no_finance'}
        float_share = float(fin.iloc[0].get('liutongguben', 0) or 0)
        total_share = float(fin.iloc[0].get('zongguben', 0) or 0)
        share = float_share if float_share > 0 else total_share
        if share <= 0:
            return sym, {'turnover_pct': None, 'error': 'no_shares'}
        avg_vol = recent['volume'].mean() * 100.0
        return sym, {'turnover_pct': round(avg_vol / share * 100.0, 2),
                     'source': 'float' if float_share > 0 else 'total'}
    except Exception as e:
        return sym, {'turnover_pct': None, 'error': str(e)[:80]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--procs', type=int, default=8)
    args = ap.parse_args()
    symbols = list_t0_symbols()
    print(f'拉取 {len(symbols)} 只换手率，procs={args.procs}')
    out = {}
    with ProcessPoolExecutor(max_workers=args.procs) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in symbols}
        for fut in futs:
            try:
                sym, info = fut.result(timeout=40)
                out[sym] = info
                print(f'  {sym:<12} 换手{info["turnover_pct"] if info["turnover_pct"] else "-":>8} '
                      f'({info.get("source", "-")}) {info.get("error", "")}')
            except FutureTimeout:
                out[futs[fut]] = {'turnover_pct': None, 'error': 'timeout_40s'}
                print(f'  {futs[fut]:<12} TIMEOUT')
            except Exception as e:
                out[futs[fut]] = {'turnover_pct': None, 'error': str(e)[:80]}
                print(f'  {futs[fut]:<12} ERR {e}')
    path = os.path.join(DATA_DIR, 't0_turnover.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    ok = sum(1 for v in out.values() if v.get('turnover_pct') is not None)
    print(f'💾 完成 {ok}/{len(symbols)} → {path}')


if __name__ == '__main__':
    main()
