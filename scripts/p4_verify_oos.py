# -*- coding: utf-8 -*-
"""
p4_verify_oos.py —— P4 regime 门控 OOS 验证
方法（防 data snooping）：
  - 每标的交易日按时间排序，前 60% 为 train、后 40% 为 test（test = 未参与任何参数选择的 OOS 窗口）。
  - regime 参数固定（lookback=40, thresh=0.5），不在 train 上寻优（避免过拟合光学）。
  - 比较 正T(long) 净 WR / 净收益：gate=off(baseline) vs gate=on。
  - 决策（fail-safe）：train 方向正确(ON 净≥OFF) 且 test 净(ON) ≥ test 净(OFF) → 启用；否则默认关。
数据: F:/keyfactor_data/1m_clean
用法: python p4_verify_oos.py
"""
import sys, os, json, csv
import numpy as np

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GeneralConfig
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
SYMBOLS = ['603039.SH', '688111.SH', '300058.SZ', '600570.SH', '161129.SZ', '513310.SH']
NAME = {'603039.SH': '泛微网络', '688111.SH': '金山办公', '300058.SZ': '蓝色光标',
        '600570.SH': '恒生电子', '161129.SZ': '原油LOF易方达', '513310.SH': '中概互联网ETF'}

PROD_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                       use_fixed_stop=True, fixed_stop_pct=1.5)
CFG_OFF = GeneralConfig()                       # regime_gate 默认 False
CFG_ON = GeneralConfig(regime_gate=True, regime_lookback=40, regime_downtrend_suppress=True,
                        regime_downtrend_thresh=0.5)


def load_days(path):
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


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg=0.0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg=round(sum(rets) / n, 3))


def run_window(days_list, cfg, cost):
    trips = []
    prev_close = None
    for d in days_list:
        o, h, lo, c, v = days_list[d]
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
                  'n': len(c), 'date': d, 'pc': pc, 'sym': ''}
        sigs = detect_signals_general(data, pc, cfg)
        trips.extend(simulate_day(sigs, prices, config=PROD_CFG, cost=cost))
        prev_close = c[-1]
    return trips


def main():
    results = {}
    pool = {'train': {'off': None, 'on': None}, 'test': {'off': None, 'on': None}}
    for sym in SYMBOLS:
        path = f'{DATA_DIR}/{sym}_1m.csv'
        if not os.path.exists(path):
            print(f'[{sym}] SKIP no_data'); continue
        all_days = load_days(path)
        dates = sorted(all_days.keys())
        if len(dates) < 40:
            print(f'[{sym}] {NAME[sym]} days={len(dates)} <40 不做 OOS 切分（样本不足）')
            # 仍给全样本对比
            cost = cost_for_symbol(sym)
            off = summarize(run_window({d: all_days[d] for d in dates}, CFG_OFF, cost))
            on = summarize(run_window({d: all_days[d] for d in dates}, CFG_ON, cost))
            results[sym] = {'name': NAME[sym], 'days': len(dates), 'oos': False,
                            'off': off, 'on': on}
            print(f'    全样本 OFF n={off["n"]} WR={off["wr"]}% net={off["total_ret"]}% | '
                  f'ON n={on["n"]} WR={on["wr"]}% net={on["total_ret"]}%')
            continue
        cut = int(len(dates) * 0.6)
        train_d, test_d = dates[:cut], dates[cut:]
        cost = cost_for_symbol(sym)
        off_tr = summarize(run_window({d: all_days[d] for d in train_d}, CFG_OFF, cost))
        on_tr = summarize(run_window({d: all_days[d] for d in train_d}, CFG_ON, cost))
        off_te = summarize(run_window({d: all_days[d] for d in test_d}, CFG_OFF, cost))
        on_te = summarize(run_window({d: all_days[d] for d in test_d}, CFG_ON, cost))
        results[sym] = {'name': NAME[sym], 'days': len(dates), 'oos': True,
                        'train_off': off_tr, 'train_on': on_tr,
                        'test_off': off_te, 'test_on': on_te}
        for k, ro, rn in (('train', off_tr, on_tr), ('test', off_te, on_te)):
            pool[k]['off'] = _acc(pool[k].get('off'), ro)
            pool[k]['on'] = _acc(pool[k].get('on'), rn)
        print(f'[{sym}] {NAME[sym]} days={len(dates)} (train={len(train_d)}/test={len(test_d)})')
        print(f'  TRAIN OFF n={off_tr["n"]} WR={off_tr["wr"]}% net={off_tr["total_ret"]}% | '
              f'ON n={on_tr["n"]} WR={on_tr["wr"]}% net={on_tr["total_ret"]}%')
        print(f'  TEST  OFF n={off_te["n"]} WR={off_te["wr"]}% net={off_te["total_ret"]}% | '
              f'ON n={on_te["n"]} WR={on_te["wr"]}% net={on_te["total_ret"]}%')

    # 池级 OOS 决策
    def _wr(r): return r['wr'] if r else 0
    def _net(r): return r['total_ret'] if r else 0
    tr_off, tr_on = pool['train']['off'], pool['train']['on']
    te_off, te_on = pool['test']['off'], pool['test']['on']
    train_dir = _net(tr_on) - _net(tr_off)
    test_delta = _net(te_on) - _net(te_off)
    enable = (train_dir >= 0) and (test_delta >= 0)
    print('\n=== 池级 OOS 决策 ===')
    print(f'TRAIN  net OFF={_net(tr_off)}% ON={_net(tr_on)}% (Δ={train_dir:+.2f})')
    print(f'TEST   net OFF={_net(te_off)}% ON={_net(te_on)}% (Δ={test_delta:+.2f})  [OOS 窗口]')
    print(f'决策: {"启用 regime_gate=True" if enable else "fail-safe 保持 regime_gate=False（默认关）"}')
    out = {'pool': {'train_off': tr_off, 'train_on': tr_on, 'test_off': te_off, 'test_on': te_on,
                    'train_dir': round(train_dir, 2), 'test_delta': round(test_delta, 2),
                    'enable': enable}, 'symbols': results}
    with open(os.path.join(ROOT, 'output', 'p4_oos_verify.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('\nJSON -> output/p4_oos_verify.json')


def _acc(a, r):
    if a is None:
        return dict(n=r['n'], wr=r['wr'], total_ret=r['total_ret'])
    n = a['n'] + r['n']
    return dict(n=n, wr=round(100.0 * (a['wr']/100*a['n'] + r['wr']/100*r['n']) / n, 1) if n else 0.0,
                total_ret=round(a['total_ret'] + r['total_ret'], 2))


if __name__ == '__main__':
    main()
