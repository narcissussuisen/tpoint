#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_macd_gate.py — 三种 MACD 门控风格 沙箱回测对比 (ISOLATED)
=====================================================================
目的: 用历史 1m CSV 比较 tpoint 做T引擎在三种 MACD 门控模式下的
      - 信号数量 / 信号方向准确率 / 触发后前向收益
      - 做T回合(round-trip)收益与胜率
      - 因子归因 (gravity / vol_div / macd_div 命中率对比)

隔离保证 (不影响现生产):
  1. 仅 import 本地 `miji_engine` (纯 numpy 算法层, 无任何数据源/生产模块依赖)
     不 import core/monitor, core/miji_alpha, core/datasource, 不读生产配置
  2. 只读 `KEYFACTOR_DATA_DIR（默认 F:\workbuddy\keyfactor_data）/1m/*.csv` (历史离线数据)
  3. 输出仅写入 `backtest/keyfactor/output/` (新建, 不影响其它目录)
  4. 所有产物文件名带 `sandbox_gateway_` 前缀 + 模式后缀, HTML 顶部明确标注「沙箱」

方法学要点:
  - 每个 CSV 内含多交易日 1m 拼接; 按 trade_date 分段, 逐日独立跑引擎,
    避免 VWAP 跨日累积污染 & 每日信号上限(max_b/max_s=12)被跨日共享。
  - 前向收益 horizon = [6,12,24] 根 1m。
  - 做T回合: 同一交易日内相邻 B/S 配对; B->S = 正T多仓(多开空平), S->B = 反T空仓(空开多平)。
    方向准确率: B信号后12min价涨 且 S信号后12min价跌 视为正确。
  - 摩擦成本: 每腿 TRADE_COST_PCT (默认 0.02% = 双边 0.04%), 净收益 = 毛利 - 2*成本。

用法:
  python compare_macd_gate.py [--sample N] [--seed S] [--out OUTDIR]
"""

import os
import sys
import json
import random
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

import miji_engine as ME  # 仅隔离算法层

DATA_DIR = os.path.join(KEYFACTOR_DATA_DIR, '1m')
OUT_DIR = os.path.join(HERE, 'output')
HORIZONS = [6, 12, 24]
TRADE_COST_PCT = 0.02  # 单边成本, 百分比

# 优先级标的：从 data/watchlist.json 动态读取（单一真相源，2026-07-21 移除硬编码）
def _load_priority():
    import json as _j, os as _o
    _p = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), '..', '..', 'data', 'watchlist.json')
    try:
        if _o.path.exists(_p):
            with open(_p, encoding='utf-8') as _f:
                wl = _j.load(_f)
            if wl:
                return list(wl.keys())
    except Exception:
        pass
    return []

PRIORITY = _load_priority()

GATE_MODES = ['strict', 'off', 'floor']


# ----------------------------------------------------------------------------
# 数据加载 (只读本地 CSV)
# ----------------------------------------------------------------------------
def load_symbol_csv(path):
    df = pd.read_csv(path, dtype={'symbol': str, 'name': str})
    if 'trade_time' in df.columns:
        df['trade_time'] = pd.to_datetime(df['trade_time'])
    df = df.sort_values('trade_time').reset_index(drop=True)
    return df


def run_day(day_df, gate_mode):
    """对单交易日 1m df 跑引擎, 返回 (sigs, pairs)。"""
    o = day_df['open'].values.astype(float)
    h = day_df['high'].values.astype(float)
    lo = day_df['low'].values.astype(float)
    c = day_df['close'].values.astype(float)
    v = day_df['volume'].values.astype(float)
    if len(c) < 30:
        return [], []
    has_vol = float(np.sum(v)) > 0
    pc = float(c[0])
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    sigs = ME.detect_miji_signals(
        data, pc, start_idx=2, min_resonance=ME.RESONANCE_THRESHOLD,
        b_trend_filter=False, allow_reverse=True,
        macd_gate_mode=gate_mode, enable=(True, True, True),
    )
    # 前向收益 (日内)
    n = len(c)
    for s in sigs:
        i = s['idx']
        for hh in HORIZONS:
            if i + hh < n:
                s[f'fwd{hh}'] = round((c[i + hh] / c[i] - 1.0) * 100.0, 4)
            else:
                s[f'fwd{hh}'] = None
    # 配对做T回合 (相邻 B/S)
    # 成交模型: 信号K收盘触发 -> 次根K收盘挂单成交 (1根K执行滞后, 标准研究假设,
    #   避免"信号价=成交价"的均值回归套利的虚假高胜率)。含单边成本。
    pairs = []
    k = 0
    while k + 1 < len(sigs):
        a, b = sigs[k], sigs[k + 1]
        ai, bi = a['idx'], b['idx']
        # 末根K信号无法在当日次根成交 -> 跳过该回合 (T+0 须当日了结)
        if ai + 1 >= n or bi + 1 >= n:
            k += 1
            continue
        entry_fill = float(c[ai + 1])
        exit_fill = float(c[bi + 1])
        if a['type'] == 'B' and b['type'] == 'S':
            gross = (exit_fill - entry_fill) / entry_fill * 100.0
            direction = 'LONG'
        elif a['type'] == 'S' and b['type'] == 'B':
            gross = (entry_fill - exit_fill) / entry_fill * 100.0
            direction = 'SHORT'
        else:
            k += 1
            continue
        net = gross - 2 * TRADE_COST_PCT
        pairs.append({
            'direction': direction, 'entry_idx': ai, 'exit_idx': bi,
            'entry_price': round(entry_fill, 4), 'exit_price': round(exit_fill, 4),
            'gross_pct': round(gross, 4), 'net_pct': round(net, 4),
            'win': net > 0,
        })
        k += 2
    return sigs, pairs


# ----------------------------------------------------------------------------
# 单模式聚合
# ----------------------------------------------------------------------------
def aggregate_mode(sym_sigs, sym_pairs):
    """sym_sigs: list[ (sym,date,sig) ]; sym_pairs: list[ (sym,date,pair) ]"""
    nB = nS = 0
    b_fwd = {hh: [] for hh in HORIZONS}
    s_fwd = {hh: [] for hh in HORIZONS}
    b_acc_n = s_acc_n = 0  # 方向正确计数
    for sym, date, s in sym_sigs:
        if s['type'] == 'B':
            nB += 1
            for hh in HORIZONS:
                val = s.get(f'fwd{hh}')
                if val is not None:
                    b_fwd[hh].append(val)
            v12 = s.get('fwd12')
            if v12 is not None and v12 > 0:
                b_acc_n += 1
        else:
            nS += 1
            for hh in HORIZONS:
                val = s.get(f'fwd{hh}')
                if val is not None:
                    s_fwd[hh].append(val)
            v12 = s.get('fwd12')
            if v12 is not None and v12 < 0:
                s_acc_n += 1

    nT = len(sym_pairs)
    net_pnls = [p['net_pct'] for _, _, p in sym_pairs]
    wins = sum(1 for _, _, p in sym_pairs if p['win'])
    gross_pnls = [p['gross_pct'] for _, _, p in sym_pairs]

    def mean(x):
        return float(np.mean(x)) if x else None

    acc_denom = (sum(1 for _, _, s in sym_sigs if s['type'] == 'B' and s.get('fwd12') is not None)
                 + sum(1 for _, _, s in sym_sigs if s['type'] == 'S' and s.get('fwd12') is not None))
    acc_num = b_acc_n + s_acc_n

    summ = {
        'n_signals': nB + nS, 'nB': nB, 'nS': nS,
        'net_T_per_signal': round(float(np.sum(net_pnls)) / (nB + nS), 6) if (nB + nS) else None,
        'mean_fwd_B': {hh: mean(b_fwd[hh]) for hh in HORIZONS},
        'mean_fwd_S': {hh: mean(s_fwd[hh]) for hh in HORIZONS},
        'dir_accuracy': round(acc_num / acc_denom, 4) if acc_denom else None,
        'dir_accuracy_B': round(b_acc_n / nB, 4) if nB else None,
        'dir_accuracy_S': round(s_acc_n / nS, 4) if nS else None,
        'n_roundtrip': nT,
        'mean_gross_T': mean(gross_pnls),
        'mean_net_T': mean(net_pnls),
        'total_net_T': round(float(np.sum(net_pnls)), 4) if net_pnls else 0.0,
        'win_rate_T': round(wins / nT, 4) if nT else None,
    }
    return summ


# ----------------------------------------------------------------------------
# 因子归因
# ----------------------------------------------------------------------------
def factor_attribution(sym_sigs):
    """按因子命中(g/vd/md)分组的 fwd12 均值。仅看 B 信号买点质量。"""
    fac_map = {'g': 'gravity', 'vd': 'vol_div', 'md': 'macd_div'}
    out = {}
    for key, fac in fac_map.items():
        on, off = [], []
        for sym, date, s in sym_sigs:
            if s['type'] != 'B':
                continue
            f = s['factors'][fac]
            v12 = s.get('fwd12')
            if v12 is None:
                continue
            # 买点: 该因子 == +1 视为"参与"
            if f == 1:
                on.append(v12)
            else:
                off.append(v12)
        out[key] = {
            'n_on': len(on), 'n_off': len(off),
            'mean_fwd12_on': float(np.mean(on)) if on else None,
            'mean_fwd12_off': float(np.mean(off)) if off else None,
        }
    return out


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', type=int, default=100, help='随机采样标的数量(不含优先级)')
    ap.add_argument('--seed', type=int, default=20260720)
    ap.add_argument('--out', type=str, default=OUT_DIR)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    random.seed(args.seed)

    # 选股: 优先级 + 随机采样
    all_csv = sorted(f for f in os.listdir(DATA_DIR) if f.endswith('_1m.csv'))
    all_syms = [f[:-len('_1m.csv')] for f in all_csv]
    priority = [s for s in PRIORITY if s in all_syms]
    rest = [s for s in all_syms if s not in priority]
    sampled = random.sample(rest, min(args.sample, len(rest)))
    chosen = priority + sampled
    print(f'[选股] 优先级 {len(priority)} + 随机 {len(sampled)} = 共 {len(chosen)} 只')

    # 结果容器
    results = {mode: {'sigs': [], 'pairs': [], 'equity': [], 'factor': None} for mode in GATE_MODES}

    for si, sym in enumerate(chosen, 1):
        path = os.path.join(DATA_DIR, sym + '_1m.csv')
        try:
            df = load_symbol_csv(path)
        except Exception as e:
            print(f'  [warn] 加载 {sym} 失败: {e}')
            continue
        if 'trade_date' not in df.columns:
            continue
        # 逐交易日分段
        for date, day_df in df.groupby('trade_date'):
            for mode in GATE_MODES:
                sigs, pairs = run_day(day_df, mode)
                for s in sigs:
                    results[mode]['sigs'].append((sym, date, s))
                for p in pairs:
                    results[mode]['pairs'].append((sym, date, p))
        if si % 20 == 0 or si == len(chosen):
            print(f'  [进度] {si}/{len(chosen)} 标的已处理')

    # 聚合 + 输出
    summary_all = {}
    meta = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scope': f'{len(chosen)} 只标的(优先级{len(priority)}+随机{len(sampled)}), 历史1m, 按交易日分段',
        'seed': args.seed,
        'trade_cost_pct_per_leg': TRADE_COST_PCT,
        'horizons': HORIZONS,
        'isolation': 'SANDBOX — 仅读 keyfactor_data/1m, 仅写 output/, 不读生产配置/数据源',
    }

    for mode in GATE_MODES:
        summ = aggregate_mode(results[mode]['sigs'], results[mode]['pairs'])
        summ['factor_attribution'] = factor_attribution(results[mode]['sigs'])
        # 权益曲线 (按时间序累加 net T)
        pairs_sorted = sorted(results[mode]['pairs'], key=lambda x: (str(x[1]), x[2]['exit_idx']))
        eq = 0.0
        equity_rows = []
        for sym, date, p in pairs_sorted:
            eq += p['net_pct']
            equity_rows.append({'sym': sym, 'date': str(date), 'net_pct': p['net_pct'], 'cum_net_pct': round(eq, 4)})
        summ['final_cum_net_pct'] = round(eq, 4)
        summary_all[mode] = summ

        # 标准三文件 (sandbox 标识)
        pre = f'sandbox_gateway_{mode}'
        # trades.csv
        trades_df = pd.DataFrame(
            [{'sym': s, 'date': str(d), **p} for s, d, p in results[mode]['pairs']]
        )
        trades_df.to_csv(os.path.join(args.out, f'{pre}_trades.csv'), index=False)
        # equity.csv
        eq_df = pd.DataFrame(equity_rows)
        eq_df.to_csv(os.path.join(args.out, f'{pre}_equity.csv'), index=False)
        # summary.json
        with open(os.path.join(args.out, f'{pre}_summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summ, f, ensure_ascii=False, indent=2)

    # 汇总对比 json
    with open(os.path.join(args.out, 'sandbox_gateway_summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'modes': summary_all}, f, ensure_ascii=False, indent=2)

    print(f'[完成] 汇总写入 {args.out}/sandbox_gateway_summary.json')
    for mode in GATE_MODES:
        s = summary_all[mode]
        print(f'  {mode:7s}: 信号={s["n_signals"]:5d}(B{s["nB"]}/S{s["nS"]}) '
              f'准确率={s["dir_accuracy"]} T回合={s["n_roundtrip"]:5d} '
              f'均净T={s["mean_net_T"]} 胜率={s["win_rate_T"]} 累计净T={s["final_cum_net_pct"]}%')

    return meta, summary_all


if __name__ == '__main__':
    main()
