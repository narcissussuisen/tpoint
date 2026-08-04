# -*- coding: utf-8 -*-
"""
factor_ablation_sweep.py — 因子层信号质量优化·第一轮
消融矩阵（gravity/vol_div/macd_div 组合 + 生产基线） + FLOOR_DEV_PCT 网格

目的：回答「v9.2.2 下每个因子对净收益的真实贡献」，为 macd_div 符号审查 / floor 联动寻优提供数据。
口径（与 backtest_screener 完全一致）：
  - 数据源：F 盘 tickflow 1m 历史库（F:/keyfactor_data/1m）
  - 引擎：core.miji_alpha.detect_miji_signals（生产同源），MACD_GATE_MODE=floor
  - 成本：cost_for_symbol（万一佣金不免五 / ETF 无印花税 / 北交所千0.575），净收益口径
  - 出场：PROD_CONFIG = 仅移动止损 act0.4/trail0.6 + S 信号出场，无硬止损/时间止损

组合说明（enable 三元组 = gravity, vol_div, macd_div）：
  - prod   : (1,0,1) vol_off —— 生产基线 v9.2.2（vol 因子函数内禁用）
  - vol_on : (1,0,1) vol_on  —— 与 prod 唯一差异 = vol 因子边际贡献
  - g_only / m_only / v_only / gv / vm / gvm / none

用法：
  python scripts/factor_ablation_sweep.py --matrix          # 消融矩阵 9 组合
  python scripts/factor_ablation_sweep.py --floor-sweep     # FLOOR_DEV_PCT 网格（prod 组合）
  python scripts/factor_ablation_sweep.py --all
  python scripts/factor_ablation_sweep.py --oos             # 附加 前70%/后30% 样本外切分
"""
import argparse
import datetime
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

os.environ.setdefault('MACD_GATE_MODE', 'floor')   # 必须在 import miji_alpha 前设置

import numpy as np  # noqa: E402

from core import miji_alpha  # noqa: E402
from core.exit_manager import (simulate_day, aggregate_metrics, make_config,  # noqa: E402
                               cost_for_symbol)
from scripts.backtest_screener import PROD_CONFIG, load_1m_csv, group_by_day, day_prev_close  # noqa: E402

# 8 标的（588000 缺 F 盘 1m 数据，已剔除；688347 已移出 watchlist 但保留作样本）
SYMBOLS = [
    '688146.SH', '600206.SH', '688347.SH', '600584.SH',
    '688766.SH', '161129.SZ', '513310.SH', '688111.SH',
]
DATA_DIR = os.environ.get('TP_1M_DIR', r'F:/keyfactor_data/1m')

# 消融组合: (name, enable, vol_force, vol_in_gate, gate_mode)
# 控制变量说明（2026-08-01 v3 定稿）：
#   生产架构放行 = MACD(strict基础) OR 地板(g_dev+新低)；vol 仅记录共振分数。
#   组合用「加减通道」回答业务问题，每个组合显式指定 gate_mode：
#     - prod       : v9.2.2 生产基线（floor = m门控 + 地板）
#     - no_floor   : prod 减地板 -> m 门控边际（gate=strict）
#     - floor_only : prod 减 m 基础通道 -> 地板+早盘g 边际（enable m 关，floor 地板照旧）
#     - no_g_early : prod 减早盘 g -> 早盘引力边际（enable g 关，地板 g_dev 照旧）
#     - vol_gate   : prod 加 vol 参与放行 -> vol 真实边际（唯一差异 vol_in_gate）
#     - g_only     : 纯引力假设（gate=off）
#     - v_only     : 纯 vol 假设（strict+vol_in_gate，g/m 关）
#     - gvm_floor  : 全开+地板（最强形态）
#     - none       : 空基线
COMBOS = [
    ('prod',       (True,  False, True),  False, False, 'floor'),
    ('no_floor',   (True,  False, True),  False, False, 'strict'),
    ('floor_only', (True,  False, False), False, False, 'floor'),
    ('no_g_early', (False, False, True),  False, False, 'floor'),
    ('vol_gate',   (True,  True,  True),  True,  True,  'floor'),
    ('g_only',     (True,  False, False), False, False, 'off'),
    ('v_only',     (False, True,  False), True,  True,  'strict'),
    ('gvm_floor',  (True,  True,  True),  True,  True,  'floor'),
    ('none',       (False, False, False), False, False, 'floor'),
]

FLOOR_GRID = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0]   # FLOOR_DEV_PCT 网格（prod 组合）


def run_symbol(csv_path, enable, floor_dev_pct, vol_force, vol_in_gate=False, macd_gate_mode='floor'):
    """单标的单组合回测。返回 (metrics, trips)。"""
    # monkey-patch 模块全局（detect_miji_signals 内部读取）
    miji_alpha.VOL_DIV_ENABLED = vol_force
    miji_alpha.FLOOR_DEV_PCT = floor_dev_pct
    miji_alpha.MACD_GATE_MODE = macd_gate_mode

    df = load_1m_csv(csv_path)
    if 'symbol' in df.columns:
        symbol = str(df['symbol'].iloc[0])
    else:
        symbol = os.path.basename(csv_path).replace('_1m.csv', '').replace('_5m.csv', '')
    cost = cost_for_symbol(symbol)
    mcfg = make_config(**PROD_CONFIG)

    all_trips = []
    day_count = 0
    for date, sub in group_by_day(df):
        pc = day_prev_close(df, date)
        if pc is None or pc <= 0:
            continue
        o = sub['open'].values.astype(float)
        h = sub['high'].values.astype(float)
        lo = sub['low'].values.astype(float)
        c = sub['close'].values.astype(float)
        v = sub['volume'].values.astype(float)
        data = miji_alpha.compute_miji_indicators(o, h, lo, c, v, pc)
        sigs = miji_alpha.detect_miji_signals(data, pc, enable=enable,
                                              macd_gate_mode=macd_gate_mode,
                                              vol_in_gate=vol_in_gate)
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data.get('trend'), 'n': data['n']}
        all_trips.extend(simulate_day(sigs, prices, mcfg, cost=cost))
        day_count += 1
    return aggregate_metrics(all_trips), all_trips


def run_symbol_oos(csv_path, enable, floor_dev_pct, vol_force, vol_in_gate=False, macd_gate_mode='floor'):
    """带样本内/外切分：按日期排序，前70%训练 / 后30%测试。返回 {train, test, full}。"""
    miji_alpha.VOL_DIV_ENABLED = vol_force
    miji_alpha.FLOOR_DEV_PCT = floor_dev_pct
    miji_alpha.MACD_GATE_MODE = macd_gate_mode

    df = load_1m_csv(csv_path)
    if 'symbol' in df.columns:
        symbol = str(df['symbol'].iloc[0])
    else:
        symbol = os.path.basename(csv_path).replace('_1m.csv', '').replace('_5m.csv', '')
    cost = cost_for_symbol(symbol)
    mcfg = make_config(**PROD_CONFIG)

    days = group_by_day(df)
    n = len(days)
    cut = int(n * 0.7)
    parts = {'train': days[:cut], 'test': days[cut:], 'full': days}
    out = {}
    for pname, pdays in parts.items():
        trips = []
        for date, sub in pdays:
            pc = day_prev_close(df, date)
            if pc is None or pc <= 0:
                continue
            o = sub['open'].values.astype(float)
            h = sub['high'].values.astype(float)
            lo = sub['low'].values.astype(float)
            c = sub['close'].values.astype(float)
            v = sub['volume'].values.astype(float)
            data = miji_alpha.compute_miji_indicators(o, h, lo, c, v, pc)
            sigs = miji_alpha.detect_miji_signals(data, pc, enable=enable,
                                                  macd_gate_mode=macd_gate_mode,
                                                  vol_in_gate=vol_in_gate)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                      'trend': data.get('trend'), 'n': data['n']}
            trips.extend(simulate_day(sigs, prices, mcfg, cost=cost))
        out[pname] = aggregate_metrics(trips)
    return out


def _fmt(m):
    return (f'笔{m["total"]:>3} 净胜率{m["win_rate"]:>5.1f}% '
            f'毛胜率{m["gross_win_rate"]:>5.1f}% 盈亏比{m["pl_ratio"]:>4.2f} '
            f'净收益{m["total_ret"]:>7.2f}% 年化{m["ann_ret_pct"]:>7.2f}%')


def run_matrix(oos=False, verbose=True):
    """消融矩阵：9 组合 × 8 标的。返回 {combo: {symbol: metrics}}。"""
    results = {}
    t0 = time.time()
    for name, enable, vol_force, vol_in_gate, gate_mode in COMBOS:
        results[name] = {}
        agg = {}
        for sym in SYMBOLS:
            p = os.path.join(DATA_DIR, f'{sym}_1m.csv')
            if not os.path.exists(p):
                if verbose:
                    print(f'  ⚠️ 缺数据 {sym}')
                results[name][sym] = {'error': 'missing'}
                continue
            try:
                if oos:
                    r = run_symbol_oos(p, enable, 1.5, vol_force, vol_in_gate=vol_in_gate,
                                       macd_gate_mode=gate_mode)
                    results[name][sym] = {k: r[k] for k in ('train', 'test', 'full')}
                    m = r['full']
                else:
                    m, _ = run_symbol(p, enable, 1.5, vol_force, vol_in_gate=vol_in_gate,
                                      macd_gate_mode=gate_mode)
                    results[name][sym] = m
                for k in ('total', 'win_rate', 'gross_win_rate', 'pl_ratio',
                          'total_ret', 'ann_ret_pct', 'max_drawdown_pct'):
                    agg[k] = agg.get(k, 0) + (m.get(k) or 0)
            except Exception as e:
                results[name][sym] = {'error': str(e)}
        if verbose:
            n_sym = len([s for s in SYMBOLS if os.path.exists(os.path.join(DATA_DIR, f'{s}_1m.csv'))])
            print(f'\n▌组合 {name:12s} enable={enable} vol_force={vol_force} vol_in_gate={vol_in_gate} gate={gate_mode}')
            for sym in SYMBOLS:
                r = results[name].get(sym, {})
                if 'error' in r:
                    print(f'    {sym}: ❌ {r["error"]}')
                else:
                    print(f'    {sym}: {_fmt(r)}')
    if verbose:
        print(f'\n⏱ 消融矩阵耗时 {time.time()-t0:.0f}s')
    return results


def run_floor_sweep(oos=False, verbose=True):
    """FLOOR_DEV_PCT 网格：6 档 × 8 标的（prod 组合）。返回 {floor: {symbol: metrics}}。"""
    results = {}
    t0 = time.time()
    for fv in FLOOR_GRID:
        results[str(fv)] = {}
        agg = {}
        for sym in SYMBOLS:
            p = os.path.join(DATA_DIR, f'{sym}_1m.csv')
            if not os.path.exists(p):
                continue
            try:
                if oos:
                    r = run_symbol_oos(p, (True, False, True), fv, False)
                    results[str(fv)][sym] = {k: r[k] for k in ('train', 'test', 'full')}
                    m = r['full']
                else:
                    m, _ = run_symbol(p, (True, False, True), fv, False)
                    results[str(fv)][sym] = m
                for k in ('total', 'win_rate', 'pl_ratio', 'total_ret'):
                    agg[k] = agg.get(k, 0) + (m.get(k) or 0)
            except Exception as e:
                results[str(fv)][sym] = {'error': str(e)}
        if verbose:
            print(f'\n▌FLOOR_DEV_PCT={fv}')
            for sym in SYMBOLS:
                r = results[str(fv)].get(sym, {})
                if 'error' in r:
                    print(f'    {sym}: ❌ {r["error"]}')
                else:
                    print(f'    {sym}: {_fmt(r)}')
    if verbose:
        print(f'\n⏱ floor 网格耗时 {time.time()-t0:.0f}s')
    return results


def aggregate_across(results):
    """跨 8 标的汇总：净胜率=胜的笔数/总笔数（笔级加权），总净收益=各标的总和。"""
    out = {}
    for combo, by_sym in results.items():
        tot = wins = gross_wins = 0
        ret = 0.0
        pl_wins = pl_losses = 0.0
        for sym, m in by_sym.items():
            if 'error' in m:
                continue
            n = m.get('total') or 0
            tot += n
            wins += n * (m.get('win_rate') or 0) / 100.0
            gross_wins += n * (m.get('gross_win_rate') or 0) / 100.0
            ret += m.get('total_ret') or 0.0
            pl_wins += n * (m.get('avg_win') or 0)
            pl_losses += n * (m.get('avg_loss') or 0)
        out[combo] = {
            'total': tot,
            'win_rate': round(wins / tot * 100, 1) if tot else 0.0,
            'gross_win_rate': round(gross_wins / tot * 100, 1) if tot else 0.0,
            'total_ret': round(ret, 2),
            'avg_win': round(pl_wins / tot, 3) if tot else 0.0,
            'avg_loss': round(pl_losses / tot, 3) if tot else 0.0,
        }
    return out


def main():
    ap = argparse.ArgumentParser(description='因子消融矩阵 + FLOOR 网格')
    ap.add_argument('--matrix', action='store_true', help='跑消融矩阵')
    ap.add_argument('--floor-sweep', action='store_true', help='跑 FLOOR_DEV_PCT 网格')
    ap.add_argument('--all', action='store_true', help='跑全部')
    ap.add_argument('--oos', action='store_true', help='附加前70%/后30%样本外切分')
    ap.add_argument('--out', help='JSON 输出路径')
    args = ap.parse_args()

    if not (args.matrix or args.floor_sweep or args.all):
        ap.print_help()
        return

    date_str = datetime.date.today().strftime('%Y-%m-%d')
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbols': SYMBOLS,
        'data_dir': DATA_DIR,
        'config': PROD_CONFIG,
        'combos': COMBOS,
        'floor_grid': FLOOR_GRID,
    }
    if args.matrix or args.all:
        print('=' * 70)
        print('🔬 消融矩阵（9 组合 × 8 标的 · MACD_GATE_MODE=floor · 万一费率）')
        print('=' * 70)
        payload['matrix'] = run_matrix(oos=args.oos)
    if args.floor_sweep or args.all:
        print('\n' + '=' * 70)
        print('📐 FLOOR_DEV_PCT 网格（prod 组合 × 6 档）')
        print('=' * 70)
        payload['floor_sweep'] = run_floor_sweep(oos=args.oos)

    # 跨标的汇总（笔级加权）
    if 'matrix' in payload:
        payload['matrix_agg'] = aggregate_across(payload['matrix'])
    if 'floor_sweep' in payload:
        payload['floor_agg'] = aggregate_across(payload['floor_sweep'])

    out_path = args.out or os.path.join(BASE, 'output', f'factor_ablation_{date_str}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n💾 结果已写入 {out_path}')


if __name__ == '__main__':
    main()
