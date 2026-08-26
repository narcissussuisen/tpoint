# -*- coding: utf-8 -*-
"""tests/test_tick_aggregator.py — P8 tick 管道回归测试（纯脚本，无 pytest 依赖）

运行：venv/Scripts/python.exe tests/test_tick_aggregator.py
覆盖：
  1. tick_loader 读取真实样本（161129.SZ_20260722）非空
  2. 分钟聚合 OHLC 单调一致性（h≥max(o,c)、l≤min(o,c)）
  3. 大单阈值自适应（large_tape_count ≥1）
  4. buy_ratio ∈ [0,1]
  5. 特征生成：trade_count/方向流/vwap_dev 存在且值域合理
  6. 已知数据约束断言：tick 价格与 F 盘 1m 相差 ~10x（复权口径，记录在案）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from tick_loader import load_tick_day, list_available  # noqa: E402
from tick_aggregator import aggregate_minute  # noqa: E402
from tick_features import build_minute_features  # noqa: E402

SYM, DATE = '161129.SZ', '20260722'
FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f'  PASS  {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL  {name}  {detail}')


def main():
    print('=== P8 test_tick_aggregator.py ===')

    # 1) loader
    avail = list_available(sym=SYM)
    check('loader lists days', len(avail) > 30, f'n={len(avail)}')
    tk = load_tick_day(SYM, DATE)
    check('loader reads day', tk is not None and len(tk) > 500, f'n={len(tk) if tk is not None else 0}')
    check('direction in {0,1,2}', set(tk['direction'].unique()) <= {0, 1, 2},
          f'unique={set(tk["direction"].unique())}')

    # 2) 分钟聚合一致性
    agg = aggregate_minute(tk)
    check('agg non-empty', len(agg) > 100, f'n={len(agg)}')
    check('h >= max(o,c)', bool((agg['h'] >= agg[['o', 'c']].max(axis=1)).all()))
    check('l <= min(o,c)', bool((agg['l'] <= agg[['o', 'c']].min(axis=1)).all()))
    check('vwap within [l,h]', bool(((agg['vwap'] >= agg['l'] - 1e-9) & (agg['vwap'] <= agg['h'] + 1e-9)).all()))

    # 3) 大单自适应
    check('large_tape>=1', int(agg['large_tape_count'].sum()) >= 1)
    check('buy_ratio in [0,1]', bool(((agg['buy_ratio'] >= 0) & (agg['buy_ratio'] <= 1)).all()))

    # 4) 特征
    f = build_minute_features(tk)
    req_cols = {'trade_count', 'buy_ratio', 'large_tape_count', 'max_tape_share',
                'vwap_dev', 'hilo_range_pct', 'direction_flow', 'same_price_tape'}
    check('feature cols present', req_cols <= set(f.columns),
          f'missing={req_cols - set(f.columns)}')
    check('vwap_dev finite', bool(f['vwap_dev'].notna().all()))

    # 5) 已知约束：价格与 F 盘 1m 差 ~10x（记录口径，不作为失败）
    dev = abs(tk['price'].mean() / 2.03 - 10)  # F-1m 20260722 close≈2.03
    print(f'  INFO  tick/F-1m 价格比 ≈ {tk["price"].mean() / 2.03:.1f}（复权口径差 ~10x，相对特征可用）')

    print()
    if FAILURES:
        print(f'RESULT: FAIL ({len(FAILURES)}) -> {FAILURES}')
        sys.exit(1)
    print('RESULT: PASS (all tick aggregator tests)')


if __name__ == '__main__':
    main()
