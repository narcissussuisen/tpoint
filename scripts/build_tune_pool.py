#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_tune_pool.py — 两段式调参：40 只调参池抽取（2026-08-02 P2 落地）

背景：259 万样本报告结论落地后的两段式调参（防过拟合）：
  第一段：40 只调参池网格搜索（本脚本产出调参池）
  第二段：watchlist 5 只独立验证（不进调参池）

抽取协议（用户 2026-08-02 确认）：
  - 来源：F:\\keyfactor_data\\1m\\（4149 只 tickflow 1m）
  - 数据天数 ≥ 120 天优先（行数 ≥ 28000 作代理；300058/600570/688111 类 34995 行≈145 天）
  - 板块分层：T+0（ETF/LOF 15xxxx/51xxxx/56xxxx/58xxxx）+ T+1 个股（沪深主板/创业板/科创板）
  - 排除 8 只基线样本（161129/513310/688111/600584/688146/600206/688347/688766）
    与 watchlist 验证集（300058/600570 属验证段，不进调参池）
  - 随机种子固定 seed=20260801 保证可复现

用法：
  python scripts/build_tune_pool.py [--size 40] [--seed 20260801] [--min-rows 28000]
输出：
  data/tune_pool_40.json  [{symbol, name, days, rows, board, group}]
"""
import argparse
import glob
import json
import os
import random

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = 'F:/keyfactor_data/1m'

# 8 只基线样本（2026-08-01 首测）+ 验证集 watchlist（300058/600570 属验证段）
EXCLUDE = {
    '161129.SZ', '513310.SH', '688111.SH',  # 旧 watchlist
    '688146.SH', '600206.SH', '688347.SH', '600584.SH', '688766.SH',  # 候选池 5
    '300058.SZ', '600570.SH',  # 新 watchlist 验证集（不进调参池）
}


def classify(sym):
    """板块分层：返回 (board, group)。board=沪主板/深主板/创业板/科创板/北交所/ETF/LOF。"""
    code, mkt = sym.split('.')
    num = code[0]
    if mkt == 'SH' and num == '6':
        # 688/689 开头 = 科创板；60x 开头 = 沪主板
        if code[:3] in ('688', '689'):
            return '科创板', 'T+1'
        return '沪主板', 'T+1'
    if mkt == 'SZ' and num == '0':
        return '深主板', 'T+1'
    if mkt == 'SZ' and num == '3':
        return '创业板', 'T+1'
    if mkt == 'SH' and num == '8':
        return '科创板', 'T+1'
    if mkt == 'BJ':
        return '北交所', 'T+1'
    if mkt == 'SH' and num == '5':
        # 5xxxxx = ETF/LOF 基金（沪市 T+0）
        return 'ETF/LOF', 'T+0'
    if mkt == 'SZ' and num == '1':
        # 1xxxxx = ETF/LOF 基金（深市 T+0）
        return 'ETF/LOF', 'T+0'
    if mkt == 'SZ' and num == '2':
        return '深主板', 'T+1'
    return '其他', 'T+1'


def count_rows(path):
    """快速数行数（不读全文件）：用文件大小估算或扫末尾。这里用 readline 到 EOF 计数（1m 文件最大 35k 行，可接受）。"""
    n = 0
    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        for _ in f:
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description='两段式调参：40 只调参池抽取')
    ap.add_argument('--size', type=int, default=40, help='调参池大小（默认 40）')
    ap.add_argument('--seed', type=int, default=20260801, help='随机种子（默认 20260801）')
    ap.add_argument('--min-rows', type=int, default=28000, help='最小行数（默认 28000≈120天）')
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(DATA_DIR, '*_1m.csv')))
    print(f'📂 扫描 {DATA_DIR}: {len(paths)} 个文件')

    pool = []
    for p in paths:
        base = os.path.basename(p).replace('_1m.csv', '')
        if base in EXCLUDE:
            continue
        # 跳过 .bad 文件
        if base.endswith('.bad'):
            continue
        board, group = classify(base)
        if board == '其他':
            continue
        rows = count_rows(p)
        # T+0（ETF/LOF）数据天数天然少（F 盘 66 天≈16001 行），放宽门槛
        # T+1 个股要求 ≥min_rows（120天），T+0 基金要求 ≥15000 行（≈60天）
        min_rows_eff = args.min_rows if group == 'T+1' else 15000
        if rows < min_rows_eff:
            continue
        pool.append({'symbol': base, 'rows': rows, 'board': board, 'group': group})

    print(f'✅ 满足天数条件: T+1≥{args.min_rows}行 / T+0≥15000行 → {len(pool)} 只')

    # 板块分层抽样：T+0 与 T+1 各约 50%
    t0 = [x for x in pool if x['group'] == 'T+0']
    t1 = [x for x in pool if x['group'] == 'T+1']
    print(f'  T+0（ETF/LOF）: {len(t0)} 只 | T+1（个股）: {len(t1)} 只')

    rng = random.Random(args.seed)
    n_t0 = args.size // 2
    n_t1 = args.size - n_t0
    if len(t0) < n_t0 or len(t1) < n_t1:
        print(f'  ⚠️ T+0 或 T+1 数量不足（需要 {n_t0}/{n_t1}），按实际数量调整')
        n_t0 = min(n_t0, len(t0))
        n_t1 = min(n_t1, len(t1))
    sel_t0 = rng.sample(t0, n_t0)
    sel_t1 = rng.sample(t1, n_t1)
    selected = sel_t0 + sel_t1
    rng.shuffle(selected)

    out_path = os.path.join(BASE, 'data', f'tune_pool_{args.size}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'generated_at': '2026-08-02',
            'seed': args.seed,
            'min_rows': args.min_rows,
            'exclude': sorted(EXCLUDE),
            'pool': selected,
        }, f, ensure_ascii=False, indent=2)
    print(f'💾 调参池已写入 {out_path}')
    print(f'\n=== 调参池 {len(selected)} 只 ===')
    from collections import Counter
    boards = Counter(x['board'] for x in selected)
    for b, n in boards.items():
        print(f'  {b}: {n} 只')
    print('\n标的清单:')
    for x in selected:
        print(f"  {x['symbol']:12s} {x['board']:8s} {x['group']} {x['rows']}行")


if __name__ == '__main__':
    main()
