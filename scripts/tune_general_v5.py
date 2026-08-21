# -*- coding: utf-8 -*-
"""
tune_general_v5.py —— v5/GT 参数收紧 + regime 门控 迭代调参（干净数据）

数据：F:/keyfactor_data/1m_clean（2026-08-20 清洗后，剔 ms 合成段/垃圾vol/时段外）
基线：buy_threshold=0.45 / sell_threshold=0.45 / signal_gap=6（G1 FAIL，WR 21.9%）

扫描维度：
  1. threshold ∈ {0.45, 0.50, 0.55, 0.60}（收紧：少而精）
  2. regime 门控（低波日降频）：当日 ATR 中位数 < 20 日 ATR 中位 × 0.85 → max_b/s × 0.5
     （用已收盘 20 日滚动，禁前视；仅影响当日信号配额，不改变信号本身）
  3. signal_gap ∈ {6, 8}（节奏）

判定：池级 WR ≥ 55% 且 n ≥ 20（G1）；同时看净 ret 改善。
输出：output/tune_general_v5_<date>.json + 控制台对比表。
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GeneralConfig
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')
SYMBOLS = ['161129.SZ', '513310.SH', '688111.SH', '603039.SH', '300058.SZ', '600570.SH']
NAME = {'603039.SH':'泛微网络','688111.SH':'金山办公','300058.SZ':'蓝色光标','600570.SH':'恒生电子',
        '161129.SZ':'原油LOF易方达','513310.SH':'中概互联网ETF'}


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = (np.array([float(x['open']) for x in rs], dtype=float),
                   np.array([float(x['high']) for x in rs], dtype=float),
                   np.array([float(x['low']) for x in rs], dtype=float),
                   np.array([float(x['close']) for x in rs], dtype=float),
                   np.array([float(x['volume']) for x in rs], dtype=float))
    return days


def day_atr_median(h, lo, c):
    tr = np.maximum(h - lo, np.maximum(abs(h - np.roll(c, 1)), abs(lo - np.roll(c, 1))))
    tr[0] = h[0] - lo[0]
    return float(np.median(tr))


def run_symbol(sym, cfg, use_regime=True, min_days=4):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    if len(dates) < min_days:
        return {'sym': sym, 'error': f'insufficient_days({len(dates)})'}
    cost = cost_for_symbol(sym)
    trips = []
    atr_hist = []  # 20 日 ATR 中位滚动（已收盘）
    prev_close = None
    n_ok = 0
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        # regime 门控：当日 ATR 中位 vs 20 日滚动中位
        d_atr = day_atr_median(h, lo, c)
        quota_mult = 1.0
        if use_regime and len(atr_hist) >= 20:
            base = float(np.median(atr_hist[-20:]))
            if base > 0 and d_atr < 0.85 * base:
                quota_mult = 0.5  # 低波日降频
        cfg_i = cfg
        if quota_mult < 1.0:
            cfg_i = cfg.__class__(**{**cfg.__dict__,
                                      'max_b': max(1, int(cfg.max_b * quota_mult)),
                                      'max_s': max(1, int(cfg.max_s * quota_mult))})
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        # ⚠️ detect_signals_general 用函数参数 max_b/max_s（默认12），须显式传 cfg 配额
        sigs = detect_signals_general(data, pc, cfg_i, max_b=cfg_i.max_b, max_s=cfg_i.max_s)
        trips.extend(simulate_day(sigs, prices, make_config(), cost))
        atr_hist.append(d_atr)
        n_ok += 1
        prev_close = c[-1]
    return {'sym': sym, 'days': n_ok, 'trips': trips}


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default=','.join(SYMBOLS))
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--thr', default='0.45,0.50,0.55,0.60')
    ap.add_argument('--gap', default='6,8')
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]
    results = []
    for thr_s in a.thr.split(','):
        thr = float(thr_s)
        for gap_s in a.gap.split(','):
            gap = int(gap_s)
            for regime in (True, False):
                cfg = GeneralConfig(buy_threshold=thr, sell_threshold=thr,
                                    signal_gap=gap, b_downtrend_reversal=True)
                pool_trips, pool_days = [], 0
                per = {}
                for sym in syms:
                    r = run_symbol(sym, cfg, use_regime=regime)
                    if 'error' in r:
                        per[sym] = {'error': r['error']}
                        continue
                    s = summarize(r['trips'])
                    per[sym] = {'days': r['days'], **s}
                    pool_trips.extend(r['trips']); pool_days += r['days']
                ps = summarize(pool_trips)
                ps['days'] = pool_days
                g1 = 'PASS' if (ps['n'] >= 20 and ps['wr'] >= 55.0) else 'FAIL'
                tag = f'thr={thr:.2f} gap={gap} regime={regime}'
                print(f'[{tag}] n={ps["n"]} WR={ps["wr"]}% net={ps["total_ret"]}% days={pool_days} → G1:{g1}')
                results.append({'thr': thr, 'gap': gap, 'regime': regime, 'pool': ps,
                                'g1': g1, 'per_symbol': per})
    best = [r for r in results if r['g1'] == 'PASS']
    best = sorted(best, key=lambda r: r['pool']['total_ret'], reverse=True)
    print('\n=== 最优（按净ret排序）===')
    for r in best[:5]:
        print(f"thr={r['thr']} gap={r['gap']} regime={r['regime']} WR={r['pool']['wr']}% net={r['pool']['total_ret']}%")
    if not best:
        top = sorted(results, key=lambda r: r['pool']['total_ret'], reverse=True)[:5]
        print('无 PASS 组合；净ret top5:')
        for r in top:
            print(f"thr={r['thr']} gap={r['gap']} regime={r['regime']} WR={r['pool']['wr']}% net={r['pool']['total_ret']}%")
    fn = f'tune_general_v5_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump({'date': a.out_suffix, 'results': results}, f, ensure_ascii=False, indent=2)
    print(f'JSON -> {os.path.join(OUT, fn)}')


if __name__ == '__main__':
    main()
