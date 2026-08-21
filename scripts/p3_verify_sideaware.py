# -*- coding: utf-8 -*-
"""
p3_verify_sideaware.py —— P3.3 验收：faithful 双向解耦回测
镜像 monitor.py 运行时逻辑：
  - 正T(多仓) 出场 = PROD EXIT_CFG（与生产一致：trail0.4/0.6 + FIXSTOP1.5，关硬/时间止损）
  - 反T(空仓) 出场 = EXIT_CFG_SHORT（DEFAULT 类：硬止损atr1.5 + 时间止损90 + trail0.4/0.6，FIXSTOP off）
    —— 保留反T 正期望，不被正T 紧出场污染。

验收口径（用户 q-0 决策「按期望值启用/DET对齐」）：
  1) 反T 池级净收益 >= 0  → 反T 信号系统成立并启用
  2) 双向合计净 >= 正T净    → 反T 是增量正贡献，不拖累正T

数据: F:/keyfactor_data/1m_clean（tickflow 真实 1m）
用法: python p3_verify_sideaware.py [--last N] [--syms a,b,c]
"""
import sys, os, argparse, json
import numpy as np

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data
from simulate_bidirectional import simulate_bidirectional

DATA_DIR = r'F:/keyfactor_data/1m_clean'
SYMBOLS = ['603039.SH', '688111.SH', '300058.SZ', '600570.SH', '161129.SZ', '513310.SH']
NAME = {'603039.SH': '泛微网络', '688111.SH': '金山办公', '300058.SZ': '蓝色光标',
        '600570.SH': '恒生电子', '161129.SZ': '原油LOF易方达', '513310.SH': '中概互联网ETF'}

# 与生产 monitor.py 完全一致的两套出场配置
PROD_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                       use_fixed_stop=True, fixed_stop_pct=1.5)   # 正T 用
SHORT_CFG = make_config()                                         # 反T 用（DEFAULT 类，保留正期望）


def load_days(path):
    import csv
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = (np.array([float(x['open']) for x in rs]),
                   np.array([float(x['high']) for x in rs]),
                   np.array([float(x['low']) for x in rs]),
                   np.array([float(x['close']) for x in rs]),
                   np.array([float(x['volume']) for x in rs]))
    return days


def run_symbol(sym, last_n=None, min_days=5):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    if last_n:
        dates = dates[-last_n:]
    cost = cost_for_symbol(sym)
    long_all, short_all = [], []
    prev_close = None
    n_days_ok = 0
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        import pandas as pd
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        # 解耦：正T 用 PROD，反T 用 SHORT
        long_all.extend(simulate_day(sigs, prices, config=PROD_CFG, cost=cost))
        short_all.extend(simulate_bidirectional(sigs, prices, config=SHORT_CFG, cost=cost))
        n_days_ok += 1
        prev_close = c[-1]
    if n_days_ok < min_days:
        return {'sym': sym, 'error': f'insufficient_days({n_days_ok})'}
    return {'sym': sym, 'days': n_days_ok, 'first': dates[0], 'last': dates[-1],
            'long': long_all, 'short': short_all}


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg_trip=0.0, win=0, loss=0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg_trip=round(sum(rets) / n, 3), win=wins, loss=n - wins)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--last', type=int, default=None)
    ap.add_argument('--syms', default=','.join(SYMBOLS))
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]

    results = {}
    for sym in syms:
        r = run_symbol(sym, last_n=a.last)
        if 'error' in r:
            print(f'[{sym}] SKIP: {r["error"]}')
            results[sym] = {'sym': sym, 'name': NAME.get(sym, sym), 'error': r['error']}
            continue
        la = summarize(r['long']); sa = summarize(r['short'])
        combined = round(la['total_ret'] + sa['total_ret'], 2)
        results[sym] = {'sym': sym, 'name': NAME.get(sym, sym), 'days': r['days'],
                        'first': r['first'], 'last': r['last'],
                        'long': la, 'short': sa, 'combined_ret': combined}
        print(f'[{sym}] {NAME.get(sym,sym)} days={r["days"]} '
              f'| 正T n={la["n"]} WR={la["wr"]}% net={la["total_ret"]}% '
              f'| 反T n={sa["n"]} WR={sa["wr"]}% net={sa["total_ret"]}% '
              f'| 双向合计净={combined}%')

    # 池级
    ok = [r for r in results.values() if 'error' not in r]
    def _pool(key):
        n = sum(r[key]['n'] for r in ok)
        win = sum(r[key]['win'] for r in ok)
        ret = round(sum(r[key]['total_ret'] for r in ok), 2)
        return dict(n=n, wr=round(100.0 * win / n, 1) if n else 0.0, total_ret=ret)
    pl = _pool('long'); ps = _pool('short')
    pdual = round(pl['total_ret'] + ps['total_ret'], 2)
    print(f'\n=== 池级 ===')
    print(f'正T: n={pl["n"]} WR={pl["wr"]}% net={pl["total_ret"]}%')
    print(f'反T: n={ps["n"]} WR={ps["wr"]}% net={ps["total_ret"]}%')
    print(f'双向合计净 = {pdual}%')
    print(f'\n验收#1 反T净>=0 : {"PASS" if ps["total_ret"] >= 0 else "FAIL"} ({ps["total_ret"]}%)')
    print(f'验收#2 双向>=正T: {"PASS" if pdual >= pl["total_ret"] else "FAIL"} '
          f'({pdual}% vs {pl["total_ret"]}%)')

    out = {'engine': 'general', 'mode': 'side-aware(PROD_LONG+DEFAULT_SHORT)',
           'acceptance': {'rev_net_ge_0': ps['total_ret'] >= 0,
                          'dual_ge_long': pdual >= pl['total_ret']},
           'pool': {'long': pl, 'short': ps, 'dual_total_ret': pdual},
           'symbols': results}
    with open(os.path.join(ROOT, 'output', 'p3_sideaware_verify.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('\nJSON -> output/p3_sideaware_verify.json')


if __name__ == '__main__':
    main()
