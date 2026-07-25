# -*- coding: utf-8 -*-
"""候选分支 feat/v9.4.0-floord-candidate —— D 策略 Walk-forward OOS 验证 (干净方法)

信号层来自 d_strategy (权威定义, 无任何前视偏差)。本脚本负责:
  - 扩张窗口 + 重优化 walk-forward: 对测试日 k, 在 days[0:k] 上网格优化参数, 在 day[k] 评估 (OOS)
  - 末段 holdout 全程不进任何优化窗口, 作 headline OOS 结论
  - 同口径基线: 三因子共振(resonance) 也用同一前向回测框架, 同步扩张窗口优化出场参数
  - 指标: 胜率 / 总收益 / PF / 交易笔数 / 每笔均值, 按标的与按日拆解

运行: ./venv/Scripts/python.exe backtest/keyfactor/d_candidate_backtest.py
"""
import sys, os, json, itertools
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

from d_strategy import (load_day, d_signals, forward_backtest, agg,
                         is_swing_low, is_swing_high)
import miji_engine as ME

OUT = os.path.join(ROOT, 'output', 'd_candidate_20260726')
os.makedirs(OUT, exist_ok=True)

# 5 只 T+0: 161129/513310 已有21日; 518880/159985/513040 需 fetch
SYMBOLS = {
    '161129.SZ': '原油LOF',
    '513310.SH': '中韩半导体ETF',
    '518880.SH': '黄金ETF',
    '159985.SZ': '豆粕ETF',
    '513040.SH': '跨境ETF',
}
# 21 交易日 (与现有两标的一致窗口)
DAYS = [
    '2026-06-26', '2026-06-29', '2026-06-30', '2026-07-01', '2026-07-02',
    '2026-07-03', '2026-07-06', '2026-07-07', '2026-07-08', '2026-07-09',
    '2026-07-10', '2026-07-13', '2026-07-14', '2026-07-15', '2026-07-16',
    '2026-07-17', '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23',
    '2026-07-24',
]
MIN_TRAIN = 8   # 扩张窗口最少训练日
HOLD = 4        # 末段 holdout 日数

# D 网格 (优化 K, WL, EMA, k_stop, rev_exit)
GRID_D = {
    'K': [1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
    'WL': [15, 20, 30, 40],
    'EMA': [(10, 30), (20, 60), (30, 90)],
    'k_stop': [1.5, 2.0, 2.5, 3.0, 4.0],
    'rev_exit': [True, False],
}
# 共振网格 (仅出场参数随样本优化)
GRID_R = {
    'k_stop': [1.5, 2.0, 2.5, 3.0, 4.0],
    'rev_exit': [True, False],
}
MIN_TR_TRAIN = 4  # 优化时要求训练集至少这么多笔交易, 避免退化解


def load_all():
    cache = {}
    missing = []
    for code in SYMBOLS:
        for day in DAYS:
            d = load_day(code, day)
            if d is None:
                missing.append((code, day))
            else:
                cache[(code, day)] = d
    # 逐日预计算 EMA(各 span) 与 swing(各 WL) 缓存, 避免网格内层重复计算
    ema_spans = set(GRID_D['EMA'])
    wl_set = set(GRID_D['WL'])
    for (code, day), d in cache.items():
        c = d['c']; h = d['h']; lo = d['lo']; n = d['n']
        ema_cache = {}
        for (ef, es) in ema_spans:
            ef_a = pd.Series(c).ewm(span=ef, adjust=False).mean().values
            es_a = pd.Series(c).ewm(span=es, adjust=False).mean().values
            ema_cache[(ef, es)] = (ef_a, es_a)
        sl_cache = {w: np.array([is_swing_low(lo, i, w) for i in range(n)], dtype=bool)
                    for w in wl_set}
        sh_cache = {w: np.array([is_swing_high(h, i, w) for i in range(n)], dtype=bool)
                    for w in wl_set}
        d['_ema_cache'] = ema_cache
        d['_sl_cache'] = sl_cache
        d['_sh_cache'] = sh_cache
    return cache, missing


def resonance_signals(day, k_stop, rev_exit):
    data = day['data']
    pc = float(day['c'][0])
    sigs = ME.detect_miji_signals(data, pc, start_idx=2,
                                  min_resonance=ME.RESONANCE_THRESHOLD,
                                  macd_gate_mode='resonance',
                                  b_trend_filter=False)
    buys = [s['idx'] for s in sigs if s['type'] == 'B']
    sells = [s['idx'] for s in sigs if s['type'] == 'S']
    return buys, sells


def eval_day(day, mode, p):
    """用给定参数在单日上产生交易 (正向T, 下一根开盘入场)。"""
    if mode == 'D':
        ef, es = p['ef'], p['es']
        _ema = day['_ema_cache'].get((ef, es))
        _sl = day['_sl_cache'].get(p['WL'])
        _sh = day['_sh_cache'].get(p['WL'])
        buys, sells, _ = d_signals(day, p['K'], p['WL'], ef, es,
                                   _ema=_ema, _sl=_sl, _sh=_sh)
    else:
        buys, sells = resonance_signals(day, p['k_stop'], p['rev_exit'])
    tr = forward_backtest(day, buys, sells, p['k_stop'], p['rev_exit'])
    for t in tr:
        t['symbol'] = day['code']
        t['day'] = day['day']
    return tr


def score(a):
    if a['n'] == 0:
        return -1e9
    pf = a['pf']
    pf_v = 1e9 if pf is None else pf
    return pf_v * 1000.0 + (a['win_rate'] or 0.0)


def optimize(train_days, mode):
    """在 train_days 上网格优化, 返回 (best_params, best_agg)。"""
    grid = GRID_D if mode == 'D' else GRID_R
    keys = list(grid.keys())
    best_score = -1e18
    best_params = None
    best_agg = None
    for vals in itertools.product(*[grid[k] for k in keys]):
        p = dict(zip(keys, vals))
        if mode == 'D':
            p['ef'], p['es'] = p.pop('EMA')  # EMA 是元组
        all_tr = []
        for day in train_days:
            all_tr += eval_day(day, mode, p)
        a = agg(all_tr)
        s = score(a)
        if s > best_score:
            best_score = s
            best_params = dict(p)
            best_agg = a
    return best_params, best_agg


def walk_forward(cache):
    n = len(DAYS)
    rows = []          # 每个 (mode, segment) 一行, 用于轨迹与聚合
    oos_trades = {'D': [], 'R': []}   # 所有 WF 测试日 (非holdout) 的交易
    hold_trades = {'D': [], 'R': []}
    per_sym = {'D': {}, 'R': {}}       # symbol -> list of trade
    hold_per_sym = {'D': {}, 'R': {}}

    # ---- Walk-forward: 测试日 k in [MIN_TRAIN, n-HOLD) ----
    for k in range(MIN_TRAIN, n - HOLD):
        train_days = [cache[(s, DAYS[j])] for j in range(k) for s in SYMBOLS
                      if (s, DAYS[j]) in cache]
        test_day = DAYS[k]
        test_days = [cache[(s, test_day)] for s in SYMBOLS if (s, test_day) in cache]
        for mode in ('D', 'R'):
            bp, _ = optimize(train_days, mode)
            tr = []
            for day in test_days:
                tr += eval_day(day, mode, bp)
            a = agg(tr)
            rec = {'mode': mode, 'segment': test_day, 'is_holdout': False,
                   'params': bp, 'n_tr': a['n'], 'win_rate': a['win_rate'],
                   'tot_ret': a['tot_ret'], 'pf': a['pf'], 'avg': a['avg']}
            rows.append(rec)
            oos_trades[mode] += tr
            for t in tr:
                per_sym[mode].setdefault(t['symbol'], []).append(t)
        print(f'  WF 测试日 {test_day} 完成', flush=True)

    # ---- Holdout: 末 HOLD 日, 参数在 days[0:n-HOLD] 上优化一次 ----
    train_days = [cache[(s, DAYS[j])] for j in range(n - HOLD) for s in SYMBOLS
                  if (s, DAYS[j]) in cache]
    hold_days = [DAYS[j] for j in range(n - HOLD, n)]
    for mode in ('D', 'R'):
        bp, _ = optimize(train_days, mode)
        tr = []
        for hd in hold_days:
            for s in SYMBOLS:
                if (s, hd) in cache:
                    tr += eval_day(cache[(s, hd)], mode, bp)
        a = agg(tr)
        rows.append({'mode': mode, 'segment': 'HOLDOUT', 'is_holdout': True,
                     'params': bp, 'n_tr': a['n'], 'win_rate': a['win_rate'],
                     'tot_ret': a['tot_ret'], 'pf': a['pf'], 'avg': a['avg']})
        hold_trades[mode] += tr
        for t in tr:
            hold_per_sym[mode].setdefault(t['symbol'], []).append(t)

    return rows, oos_trades, hold_trades, per_sym, hold_per_sym


def per_symbol_table(trade_map):
    out = {}
    for sym, tr in trade_map.items():
        a = agg(tr)
        out[sym] = a
    return out


def main():
    print('加载数据 ...')
    cache, missing = load_all()
    print(f'  已载入 {len(cache)} 个 (标的,日); 缺失 {len(missing)}')
    if missing:
        print('  缺失样本:', missing[:10], '...' if len(missing) > 10 else '')

    print('跑 walk-forward (扩张窗口+重优化) ...')
    rows, oos_trades, hold_trades, per_sym, hold_per_sym = walk_forward(cache)

    # 聚合
    def agg_seg(trmap):
        allt = []
        for v in trmap.values():
            allt += v
        return agg(allt)

    summary = {
        'symbols': SYMBOLS, 'days': DAYS, 'n_days': len(DAYS),
        'min_train': MIN_TRAIN, 'hold': HOLD,
        'grid_D': GRID_D, 'grid_R': GRID_R,
        'missing': missing,
        'oos': {
            'D': {**agg(oos_trades['D']), 'per_symbol': per_symbol_table(per_sym['D'])},
            'R': {**agg(oos_trades['R']), 'per_symbol': per_symbol_table(per_sym['R'])},
        },
        'holdout': {
            'D': {**agg(hold_trades['D']), 'per_symbol': per_symbol_table(hold_per_sym['D'])},
            'R': {**agg(hold_trades['R']), 'per_symbol': per_symbol_table(hold_per_sym['R'])},
        },
    }

    # 轨迹 CSV (每个测试日/holdout 的 best 参数 + 指标)
    traj = []
    for r in rows:
        p = r['params']
        traj.append({
            'mode': r['mode'], 'segment': r['segment'], 'is_holdout': r['is_holdout'],
            'K': p.get('K', '-'), 'WL': p.get('WL', '-'),
            'EMA': f"{p.get('ef')},{p.get('es')}" if 'ef' in p else '-',
            'k_stop': p['k_stop'], 'rev_exit': p['rev_exit'],
            'n_tr': r['n_tr'], 'win_rate': r['win_rate'],
            'tot_ret': r['tot_ret'], 'pf': r['pf'], 'avg': r['avg'],
        })
    traj_df = pd.DataFrame(traj)
    traj_csv = os.path.join(OUT, 'd_wf_trajectory.csv')
    traj_df.to_csv(traj_csv, index=False, encoding='utf-8-sig')

    # 打印关键结论
    def fmt(a):
        return f"n={a['n']} wr={a['win_rate']} ret={a['tot_ret']} pf={a['pf']} avg={a['avg']}"
    print('\n=== OOS (walk-forward 测试日, 非holdout) ===')
    print('  D :', fmt(summary['oos']['D']))
    print('  R :', fmt(summary['oos']['R']))
    print('=== HOLDOUT (末 %d 日, 未参与优化) ===' % HOLD)
    print('  D :', fmt(summary['holdout']['D']))
    print('  R :', fmt(summary['holdout']['R']))

    with open(os.path.join(OUT, 'd_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print('\nwrote', traj_csv)
    print('wrote', os.path.join(OUT, 'd_summary.json'))
    return summary


if __name__ == '__main__':
    main()
