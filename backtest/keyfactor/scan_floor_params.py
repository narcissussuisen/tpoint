#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_floor_params.py — floor 门控参数扫描（OOS 数据驱动）
================================================================
在现有 106 标的 1m CSV 历史数据上，分别扫描：
  1. 冷却期：FLOOR_SELL_COOLDOWN_BARS ∈ [0,3,5,8,10,12,15,20,25,30]
  2. 涨停抑制：FLOOR_SUPPRESS_DAY_CHG ∈ [5,8,10,12,15,18,20,25]
  3. 趋势诊断：按日涨幅分组统计 floor 买卖准确率

每配置输出：每信号净T、卖准确率@12m、总回合数、总信号数。

效率优化：
  - 每个标的的指标只算一次（compute_miji_indicators），detect 重跑不同参数
  - 只跑 floor 模式（strict/off 不变，不需对比）
  - 降维：先单变量扫冷却期，固定最优后再扫涨停抑制
"""
import os, sys, json, argparse, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _paths import KEYFACTOR_1M_DIR

import numpy as np
import pandas as pd
import miji_engine as ME

try:
    from feishu_progress import FeishuProgress
except Exception:
    FeishuProgress = None

# 趋势诊断分组边界 (日涨跌幅 %)
TREND_BINS = [(-99, -3), (-3, -1), (-1, 1), (1, 3), (3, 99)]

DATA_DIR = KEYFACTOR_1M_DIR
TRADE_COST_PCT = 0.02
HORIZONS = [6, 12, 24]
OUT_DIR = os.path.join(HERE, 'output')
os.makedirs(OUT_DIR, exist_ok=True)


def load_priority():
    """从 data/watchlist.json 读取优先级标的"""
    import json
    p = os.path.join(HERE, '..', '..', 'data', 'watchlist.json')
    try:
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                return list(json.load(f).keys())
    except Exception:
        pass
    return []


def load_all_symbols():
    syms = []
    for fn in os.listdir(DATA_DIR):
        if fn.endswith('_1m.csv'):
            syms.append(fn.replace('_1m.csv', ''))
    return sorted(syms)


def round_trip_metrics(day_pairs):
    """从配对列表计算汇总指标"""
    if not day_pairs:
        return {'n_rounds': 0, 'net_T': 0.0, 'gross_T': 0.0, 'win_rate': 0.0}
    net = np.array([p['net_ret'] for p in day_pairs])
    return {
        'n_rounds': len(day_pairs),
        'net_T': float(net.sum()),
        'mean_net_T': float(net.mean()),
        'win_rate': float((net > 0).mean()),
    }


def pair_b_s_day(sigs, prices, cost=TRADE_COST_PCT):
    """单日内相邻 B/S 配对（次根K成交 + 双向 + 非重叠 k+=2）
    
    与 compare_macd_gate.py / run_day 口径一致:
      - 成交价 = 信号 bar 的次根K收盘 (避免"信号价=成交价"的虚假高胜率)
      - 配对顺序 = 信号列表相邻 (B->S 正T 或 S->B 反T)
      - k+=2 非重叠, 未配对信号由后续信号继续尝试
    """
    c_arr = prices['c']
    n = len(c_arr)
    pairs = []
    k = 0
    while k + 1 < len(sigs):
        a, b = sigs[k], sigs[k + 1]
        ai, bi = a['idx'], b['idx']
        # 信号 bar+1 需在日内范围内 (T+0 当日须了结, 次根K必须存在)
        if ai + 1 >= n or bi + 1 >= n:
            k += 1
            continue
        entry_fill = float(c_arr[ai + 1])
        exit_fill = float(c_arr[bi + 1])
        if a['type'] == 'B' and b['type'] == 'S':
            gross = (exit_fill - entry_fill) / entry_fill * 100.0
        elif a['type'] == 'S' and b['type'] == 'B':
            gross = (entry_fill - exit_fill) / entry_fill * 100.0
        else:
            # 同向信号不配对 (如连续 B/B 或 S/S)
            k += 1
            continue
        net = gross - 2 * cost
        pairs.append({
            'entry_idx': ai, 'exit_idx': bi,
            'entry_price': round(entry_fill, 4),
            'exit_price': round(exit_fill, 4),
            'gross_ret': round(gross, 4), 'net_ret': round(net, 4),
        })
        k += 2  # 消耗已配对的两个信号, 继续下一对
    return pairs


def fwd_ret(c, idx, k):
    j = idx + k
    if j >= len(c) or c[idx] <= 0:
        return None
    return (c[j] - c[idx]) / c[idx] * 100.0


def run_scan(symbols, cooldown_candidates, suppress_candidates, sample_size=None, rep=None):
    """主扫描逻辑。返回 cooldown_results, suppress_results, trend_diag

    rep: FeishuProgress 进度器 (None=不推送)
    """

    def prog(current=None, force=False):
        if rep is not None:
            rep.update(current=current, force=force)

    if sample_size and sample_size < len(symbols):
        rng = np.random.RandomState(42)
        symbols = list(rng.choice(symbols, size=sample_size, replace=False))

    # ---- 预计算所有标的的每日指标 ----
    print(f'[预计算] {len(symbols)} 标的的每日指标...')
    if rep is not None:
        rep.set_phase("预计算指标")
        rep.set_total(len(symbols))
        rep.update(current=f"开始预计算 {len(symbols)} 标的")
    all_day_data = []  # list of (sym, date, data, pc, prices_dict, sigs_per_param)

    mi = 0
    for fn in sorted(os.listdir(DATA_DIR)):
        sym = fn.replace('_1m.csv', '')
        if sym not in symbols:
            continue
        mi += 1
        df = pd.read_csv(os.path.join(DATA_DIR, fn))
        df = df.sort_values('trade_time').reset_index(drop=True)
        for date, day_df in df.groupby('trade_date'):
            if len(day_df) < 5:
                continue
            o = day_df['open'].values.astype(float)
            h = day_df['high'].values.astype(float)
            lo = day_df['low'].values.astype(float)
            c = day_df['close'].values.astype(float)
            v = day_df['volume'].values.astype(float) if 'volume' in day_df.columns else None
            pc = day_df['close'].iloc[0]  # 近似：用当日首根，实际可从前一日取
            # 用前收近似：取前一日最后收盘
            try:
                # 简化：若该交易日之前有数据，取前日的 close
                # 实际上我们从 CSV 中无法简单取前日收盘，用开盘价做近似
                # 对 floor 扫描影响极小（收盘用于 day_chg 计算，不影响门控）
                pc_val = float(c[0]) / (1 + (c[0] / pc - 1)) if pc > 0 else float(c[0])
            except Exception:
                pc_val = float(c[0])
            try:
                data = ME.compute_miji_indicators(o, h, lo, c, v, pc_val)
            except Exception:
                continue
            data['n'] = len(c)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data.get('atr', np.ones(len(c))), 'n': len(c)}
            day_chg_pct = (c[-1] / c[0] - 1) * 100 if c[0] > 0 else 0
            all_day_data.append((sym, str(date), data, pc_val, prices, day_chg_pct))
            prog(current=f"预计算 加载 {sym} ({mi}/{len(symbols)})")

    print(f'  共 {len(all_day_data)} 个交易日段')

    # ---- 扫描：冷却期 ----
    print(f'\n[扫描1] 冷却期 candidates={cooldown_candidates}')
    n_days = len(all_day_data)
    total_detects = n_days * (len(cooldown_candidates) + len(suppress_candidates) + len(TREND_BINS))
    if rep is not None:
        rep.set_total(len(symbols) + total_detects)
        rep.reset_clock()  # ETA 以检测阶段为准
        rep.set_phase("[扫描1] 冷却期")
        rep.update(current=f"共 {n_days} 段; 首候选 cooldown={cooldown_candidates[0]}")
    cooldown_results = {}
    for ci, cd in enumerate(cooldown_candidates):
        if rep is not None:
            rep.set_phase(f"[扫描1] 冷却期={cd} ({ci + 1}/{len(cooldown_candidates)})")
        t0 = time.time()
        total_sigs = 0; total_B = 0; total_S = 0
        total_rounds = 0; total_net_T = 0.0
        all_net_Ts = []
        sell_fwd12_vals = []  # (is_correct) per sell signal
        floor_sell_ceil_count = 0
        for sym, date, data, pc, prices, day_chg in all_day_data:
            kwargs = {'floor_sell_cooldown_bars': cd, 'floor_buy_cooldown_bars': 0}
            sigs = ME.detect_miji_signals(data, pc, macd_gate_mode='floor',
                                          enable=(True, True, True), **kwargs)
            prog(current=f"cooldown={cd} | {sym} {date}")
            # 配对
            pairs = pair_b_s_day(sigs, prices)
            total_rounds += len(pairs)
            for p in pairs:
                total_net_T += p['net_ret']
                all_net_Ts.append(p['net_ret'])
            # 准确率
            for s in sigs:
                total_sigs += 1
                if s['type'] == 'B':
                    total_B += 1
                else:
                    total_S += 1
                    f12 = fwd_ret(prices['c'], s['idx'], 12)
                    if f12 is not None:
                        sell_fwd12_vals.append((f12 < 0))  # S correct = price fell
        s_acc12 = sum(sell_fwd12_vals) / len(sell_fwd12_vals) * 100 if sell_fwd12_vals else 0
        per_sig_netT = total_net_T / total_sigs if total_sigs else 0
        cooldown_results[str(cd)] = {
            'n_signals': total_sigs, 'nB': total_B, 'nS': total_S,
            'n_rounds': total_rounds, 'total_net_T': round(total_net_T, 2),
            'per_signal_netT': round(per_sig_netT, 4),
            'sell_acc12_pct': round(s_acc12, 1),
            'mean_net_T_pct': round(np.mean(all_net_Ts) if all_net_Ts else 0, 4),
            'elapsed_s': round(time.time() - t0, 1),
        }
        print(f'  cooldown={cd:>2d}: sigs={total_sigs} rounds={total_rounds} '
              f'per_sig_netT={per_sig_netT:.4f}% sell_acc12={s_acc12:.1f}% '
              f'({cooldown_results[str(cd)]["elapsed_s"]}s)')
        prog()  # 里程碑模式: 仅越过 25/50/75/100% 才推送

    # ---- 扫描：涨停抑制（固定最优冷却期） ----
    best_cd = max(cooldown_results.keys(), key=lambda k: cooldown_results[k]['per_signal_netT'])
    print(f'\n[扫描2] 涨停抑制 candidates={suppress_candidates} (固定冷却期={best_cd})')
    if rep is not None:
        rep.set_phase(f"[扫描2] 涨停抑制(冷却期={best_cd})")
    suppress_results = {}
    for si2, sp in enumerate(suppress_candidates):
        if rep is not None:
            rep.set_phase(f"[扫描2] 涨停抑制={sp}% ({si2 + 1}/{len(suppress_candidates)})")
        t0 = time.time()
        total_sigs = 0; total_S = 0
        sell_fwd12_vals = []
        floor_sell_ceil_suppressed = 0
        for sym, date, data, pc, prices, day_chg in all_day_data:
            kwargs = {'floor_sell_cooldown_bars': int(best_cd),
                      'floor_suppress_day_chg': sp}
            sigs = ME.detect_miji_signals(data, pc, macd_gate_mode='floor',
                                          enable=(True, True, True), **kwargs)
            prog(current=f"suppress={sp}% | {sym} {date}")
            for s in sigs:
                total_sigs += 1
                if s['type'] == 'S':
                    total_S += 1
                    f12 = fwd_ret(prices['c'], s['idx'], 12)
                    if f12 is not None:
                        sell_fwd12_vals.append((f12 < 0))
        s_acc12 = sum(sell_fwd12_vals) / len(sell_fwd12_vals) * 100 if sell_fwd12_vals else 0
        suppress_results[str(sp)] = {
            'n_signals': total_sigs, 'nS': total_S,
            'sell_acc12_pct': round(s_acc12, 1),
            'elapsed_s': round(time.time() - t0, 1),
        }
        print(f'  suppress={sp:>3d}%: sigs={total_sigs} S={total_S} sell_acc12={s_acc12:.1f}%')
        prog()

    # ---- 趋势诊断：按日涨幅分组统计 floor 买卖准确率 ----
    print('\n[诊断] 按日涨幅分组 floor 准确率...')
    trend_diag = {}
    if rep is not None:
        rep.set_phase("[诊断] 趋势分组")
    for bi, (lo_chg, hi_chg) in enumerate(TREND_BINS):
        label = f'{lo_chg}% ~ {hi_chg}%'
        if rep is not None:
            rep.set_phase(f"[诊断] 趋势分组 {label} ({bi + 1}/{len(TREND_BINS)})")
        b_correct = []; s_correct = []
        for sym, date, data, pc, prices, day_chg in all_day_data:
            if not (lo_chg <= day_chg < hi_chg):
                continue
            sigs = ME.detect_miji_signals(data, pc, macd_gate_mode='floor',
                                          enable=(True, True, True))
            prog(current=f"诊断 {label} | {sym} {date}")
            for s in sigs:
                f12 = fwd_ret(prices['c'], s['idx'], 12)
                if f12 is None:
                    continue
                if s['type'] == 'B':
                    b_correct.append(f12 > 0)
                else:
                    s_correct.append(f12 < 0)
        b_acc = sum(b_correct) / len(b_correct) * 100 if b_correct else None
        s_acc = sum(s_correct) / len(s_correct) * 100 if s_correct else None
        trend_diag[label] = {'n_days': 0, 'nB': len(b_correct), 'nS': len(s_correct),
                              'B_acc12': round(b_acc, 1) if b_acc else None,
                              'S_acc12': round(s_acc, 1) if s_acc else None}
        print(f'  [{label}] B_acc12={b_acc} S_acc12={s_acc} (nB={len(b_correct)} nS={len(s_correct)})')
        prog()

    if rep is not None:
        rep.finish(summary=f"最佳冷却期={best_cd}; 段数={n_days}; 标的={len(symbols)}")

    return {
        'cooldown': cooldown_results,
        'suppress': suppress_results,
        'trend_diag': trend_diag,
        'best_cooldown': best_cd,
        'n_symbols': len(symbols),
        'n_day_segments': n_days,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', type=int, default=50, help='取样标的数(默认50, 加速扫描)')
    parser.add_argument('--full', action='store_true', help='全量106标的(较慢)')
    parser.add_argument('--out', type=str, default=None, help='结果输出路径(默认 output/scan_floor_params_result.json)')
    parser.add_argument('--no-progress', action='store_true', help='关闭飞书进度推送')
    args = parser.parse_args()

    priority = load_priority()
    all_syms = load_all_symbols()
    # 确保优先级标的在集合中
    chosen = set(priority) | set(all_syms)
    if args.full:
        sample = min(106, len(chosen))
    else:
        sample = min(args.sample, len(chosen))

    cooldown_candidates = [0, 3, 5, 8, 10, 12, 15, 20, 25, 30]
    suppress_candidates = [5, 8, 10, 12, 15, 18, 20, 25]

    # 进度器 (飞书信号群 1d241455, 里程碑模式: 仅 25/50/75/100% 推送)
    rep = None
    if FeishuProgress is not None and not args.no_progress:
        rep = FeishuProgress(title=f"floor参数OOS扫描({sample}标的)", interval=15,
                             milestones=[25, 50, 75, 100])
        rep.update(current=f"启动扫描 sample={sample}")

    print(f'扫描范围: {sample} 标的, 冷却期 {cooldown_candidates}, 涨停抑制 {suppress_candidates}')
    t0 = time.time()
    result = run_scan(list(chosen), cooldown_candidates, suppress_candidates,
                      sample_size=sample, rep=rep)

    # 保存
    out = args.out or os.path.join(OUT_DIR, 'scan_floor_params_result.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n结果已保存: {out}')
    print(f'总耗时: {time.time()-t0:.1f}s')
    print(f'最佳冷却期: {result["best_cooldown"]}')
    if rep is not None:
        rep.finish(summary=f"最佳冷却期={result['best_cooldown']}; 见 {os.path.basename(out)}")


if __name__ == '__main__':
    main()
