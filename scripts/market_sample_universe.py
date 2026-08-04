# -*- coding: utf-8 -*-
"""生成全市场分层抽样名单（板块分层 + 固定种子可复现）。

回答"8 标的是否具有全市场通用性"的第一步：8 标的样本（候选池 7 只半导体/科创板
+ watchlist 3 只）高度集中在科创板/半导体板块，需从 F 盘 4149 只全市场 1m 库
按板块分层抽样，验证强度阈值结论能否外推。

分层：沪主板 / 深主板 / 创业板 / 科创板 / 北交所(920) / ETF-LOF
过滤：交易日数 >= MIN_DAYS（数据量不足无统计意义）
排除：8 标的基线样本本身（避免样本污染）
种子：42（固定，可复现）
输出：output/market_sample_universe.json
"""
import glob
import json
import os
import random
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

DATA_DIR = 'F:/keyfactor_data/1m'
BASELINE = ['688146.SH', '600206.SH', '688347.SH', '600584.SH',
            '688766.SH', '161129.SZ', '513310.SH', '688111.SH']
MIN_DAYS = 30
OUT = os.path.join(BASE, 'output', 'market_sample_universe.json')

# 每板块目标抽样数（ETF/LOF 池小，按比例）
PER_SECTOR = {'sh_main': 12, 'sz_main': 12, 'chinext': 12, 'star': 12,
              'bse': 8, 'etf_lof': 12}


def sector_of(code):
    """按代码段分板块（与市场惯例一致）。"""
    if code.startswith(('600', '601', '603', '605')):
        return 'sh_main'
    if code.startswith(('000', '001', '002', '003')):
        return 'sz_main'
    if code.startswith(('300', '301')):
        return 'chinext'
    if code.startswith(('688', '689')):
        return 'star'
    if code.startswith('920'):
        return 'bse'
    if code.startswith(('1', '5')):
        return 'etf_lof'
    return 'other'


def count_days(path):
    try:
        df = pd.read_csv(path, usecols=['trade_date'])
        return df['trade_date'].nunique()
    except Exception as e:
        print(f'  ! 读取失败 {os.path.basename(path)}: {e}', flush=True)
        return 0


def main():
    files = [f for f in glob.glob(f'{DATA_DIR}/*_1m.csv') if not f.endswith('.bad')]
    buckets = {k: [] for k in list(PER_SECTOR) + ['other']}
    n_skip = {'baseline': 0, 'other': 0, 'short': 0}
    n_bad = 0
    for idx, f in enumerate(files):
        if idx % 500 == 0:
            print(f'  进度 {idx}/{len(files)}', flush=True)
        code = os.path.basename(f).replace('_1m.csv', '')
        if code in BASELINE:
            n_skip['baseline'] += 1
            continue
        s = sector_of(code)
        if s == 'other':
            n_skip['other'] += 1
            continue
        d = count_days(f)
        if d < MIN_DAYS:
            if d == 0:
                n_bad += 1
            n_skip['short'] += 1
            continue
        buckets[s].append({'code': code, 'days': d})

    print(f'=== 抽样池统计（排除 8 标的基线 + 数据不足 {MIN_DAYS} 天） ===')
    random.seed(42)
    sample = {}
    for s in PER_SECTOR:
        items = sorted(buckets[s], key=lambda x: x['code'])
        n = min(PER_SECTOR[s], len(items))
        sel = random.sample(items, n)
        sample[s] = sel
        codes = ','.join(x['code'] for x in sel)
        print(f'{s:10s} 池={len(items):4d} 抽={n:2d} -> {codes}')
    print(f'跳过: 基线={n_skip["baseline"]} 其他板块={n_skip["other"]} 数据不足/坏文件={n_skip["short"]}(其中坏={n_bad})', flush=True)

    meta = {
        'seed': 42, 'min_days': MIN_DAYS, 'generated': '2026-08-01',
        'note': '全市场分层抽样，验证 macd_min_hist_diff 强度阈值的通用性',
        'sample': sample,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    total = sum(len(v) for v in sample.values())
    print(f'=== 共 {total} 只 → {OUT} ===')


if __name__ == '__main__':
    main()
