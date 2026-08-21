# -*- coding: utf-8 -*-
"""
experiment_b_rebuild.py —— B 侧信号重构实验（干净数据）

诊断（2026-08-20 已确认）：B 信号 95% 在 trend=-1（下跌市）触发，forward 收益 5-60 根全负
（161129 +5根 -0.03% / 688111 +60根 -0.43%），正比例 39-51% → 防接飞刀确认太弱，纯"抄底"。

重构候选（均以"更强反转确认"为目标，保持 symbol-agnostic 比率口径）：
  R1 (基线)      : 现有 GeneralConfig（局部底 + RSI 超卖）
  R2 收盘>VWAP   : B 额外要求 close > vwap（站回均线上方才确认反转）
  R3 MACD翻正    : B 额外要求 MACD hist 由负转正（动能确认）
  R4 VWAP+MACD   : R2+R3 同时满足（最强确认）
  R5 双确认+RSI  : R4 + RSI 回升（rsi 较 3 根前上升）

输出：output/exp_b_rebuild_<date>.json（每变体：B信号数/forward 5-60根均值·正比例 + 正T WR/net）
用法：python scripts/experiment_b_rebuild.py --syms 161129.SZ,513310.SH,688111.SH
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GeneralConfig
from daily_signal_review import build_data
from exit_manager import simulate_day, make_config, cost_for_symbol
from exit_v3 import exit_v3

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = (np.array([float(x['open']) for x in rs]), np.array([float(x['high']) for x in rs]),
                   np.array([float(x['low']) for x in rs]), np.array([float(x['close']) for x in rs]),
                   np.array([float(x['volume']) for x in rs]))
    return days


def detect_b_variant(data, pc, variant):
    """B 侧变体检测：R1 原版 / R2~R5 加强确认。S 侧维持原逻辑（sell_threshold 0.45）。"""
    cfg = GeneralConfig(buy_threshold=0.45, sell_threshold=0.45, signal_gap=6,
                        b_downtrend_reversal=True)
    # 先生成基础信号
    sigs = detect_signals_general(data, pc, cfg)
    if variant == 'R1':
        return sigs
    c = data['c']; vwap = data['vwap']; hist = data['hist']; rsi = data['rsi']
    out = []
    for s in sigs:
        if s['type'] != 'B':
            out.append(s)
            continue
        i = s['idx']
        ok = True
        if variant in ('R2', 'R4', 'R5'):
            if not (c[i] > vwap[i]):
                ok = False
        if variant in ('R3', 'R4', 'R5'):
            if not (hist[i] > 0 and hist[i - 1] <= 0):
                ok = False
        if variant == 'R5':
            if not (rsi[i] > rsi[max(0, i - 3)]):
                ok = False
        if ok:
            out.append(s)
    return out


def forward_stats(sigs, data, c):
    fwd = {5: [], 15: [], 30: [], 60: []}
    n_b = 0
    for s in sigs:
        if s['type'] != 'B':
            continue
        n_b += 1
        i = s['idx']
        for k in (5, 15, 30, 60):
            if i + k < len(c):
                fwd[k].append((c[i + k] / s['price'] - 1) * 100)
    out = {'n_b': n_b}
    for k, v in fwd.items():
        if v:
            a = np.array(v)
            out[f'fwd{k}'] = {'mean': round(float(a.mean()), 4), 'pos_pct': round(100 * (a > 0).mean(), 1)}
        else:
            out[f'fwd{k}'] = {'mean': 0.0, 'pos_pct': 0.0}
    return out


def run_symbol(sym, variant, min_days=4):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days = load_days(path)
    dates = sorted(days.keys())
    if len(dates) < min_days:
        return {'sym': sym, 'error': f'insufficient_days({len(dates)})'}
    cost = cost_for_symbol(sym)
    fwd_all = {'5': [], '15': [], '30': [], '60': []}
    n_b_total = 0
    trips_v3 = []
    prev_close = None
    for d in dates:
        o, h, lo, c, v = days[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        sigs = detect_b_variant(data, pc, variant)
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'vwap': data['vwap'], 'hist': data['hist'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        # forward 统计（B 信号）
        st = forward_stats(sigs, data, c)
        n_b_total += st['n_b']
        for k in ('5', '15', '30', '60'):
            if f'fwd{k}' in st:
                fwd_all[k].append(st[f'fwd{k}'])
        # v3 正T 配对
        trips_v3.extend(exit_v3(sigs, prices, direction='long', cost=cost))
        prev_close = c[-1]
    # 聚合 forward
    agg = {'n_b': n_b_total}
    for k in ('5', '15', '30', '60'):
        entries = [x for x in fwd_all[k] if x['mean'] != 0.0 or x['pos_pct'] != 0.0]
        means = [x['mean'] for x in entries]
        poss = [x['pos_pct'] for x in entries]
        agg[f'fwd{k}'] = {'mean': round(float(np.mean(means)), 4) if means else 0.0,
                          'pos_pct': round(float(np.mean(poss)), 1) if poss else 0.0}
    # v3 正T 指标
    if trips_v3:
        rets = [t['ret_pct'] for t in trips_v3]
        wins = sum(1 for t in trips_v3 if t['ret_pct'] > 0)
        agg['v3_wr'] = round(100 * wins / len(trips_v3), 1)
        agg['v3_net'] = round(sum(rets), 2)
        agg['v3_n'] = len(trips_v3)
    else:
        agg.update(v3_wr=0.0, v3_net=0.0, v3_n=0)
    return {'sym': sym, 'days': len(dates), **agg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default='161129.SZ,513310.SH,688111.SH')
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]
    variants = ['R1', 'R2', 'R3', 'R4', 'R5']
    results = {}
    for v in variants:
        per = {}
        for sym in syms:
            r = run_symbol(sym, v)
            if 'error' in r:
                per[sym] = {'error': r['error']}
                continue
            per[sym] = r
            print(f'[{v}|{sym}] n_b={r["n_b"]} fwd5={r["fwd5"]["mean"]:+.3f}%({r["fwd5"]["pos_pct"]}%) '
                  f'fwd30={r["fwd30"]["mean"]:+.3f}%({r["fwd30"]["pos_pct"]}%) v3WR={r["v3_wr"]}% v3net={r["v3_net"]}%')
        # 池级
        pool = {'n_b': sum(r.get('n_b', 0) for r in per.values() if 'error' not in r)}
        for k in ('5', '15', '30', '60'):
            ms = [r[f'fwd{k}']['mean'] for r in per.values() if 'error' not in r and r['n_b'] > 0]
            ps = [r[f'fwd{k}']['pos_pct'] for r in per.values() if 'error' not in r and r['n_b'] > 0]
            pool[f'fwd{k}'] = {'mean': round(float(np.mean(ms)), 4) if ms else 0.0,
                               'pos_pct': round(float(np.mean(ps)), 1) if ps else 0.0}
        wr = [r['v3_wr'] for r in per.values() if 'error' not in r and r.get('v3_n', 0) > 0]
        nt = [r['v3_net'] for r in per.values() if 'error' not in r and r.get('v3_n', 0) > 0]
        pool['v3_wr'] = round(float(np.mean(wr)), 1) if wr else 0.0
        pool['v3_net'] = round(sum(nt), 2) if nt else 0.0
        results[v] = {'per_symbol': per, 'pool': pool}
        print(f'>>> [{v}] 池级 n_b={pool["n_b"]} fwd5={pool["fwd5"]["mean"]:+.3f}%({pool["fwd5"]["pos_pct"]}%) '
              f'fwd60={pool["fwd60"]["mean"]:+.3f}%({pool["fwd60"]["pos_pct"]}%) v3WR={pool["v3_wr"]}% v3net={pool["v3_net"]}%')
    fn = f'exp_b_rebuild_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump({'date': a.out_suffix, 'variants': results}, f, ensure_ascii=False, indent=2)
    print(f'JSON -> {os.path.join(OUT, fn)}')


if __name__ == '__main__':
    main()
