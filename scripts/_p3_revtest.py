# -*- coding: utf-8 -*-
"""P3 临时探针：反T(short) 在不同出场配置下的 WR/net，判断 37.4% 是否由出场配置导致。"""
import os, csv, sys
import numpy as np
ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import simulate_day, make_config, cost_for_symbol
from simulate_bidirectional import simulate_bidirectional
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
SYMS = ['688111.SH', '161129.SZ', '513310.SH']

def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs]); h = np.array([float(x['high']) for x in rs])
        lo = np.array([float(x['low']) for x in rs]); c = np.array([float(x['close']) for x in rs])
        v = np.array([float(x['volume']) for x in rs])
        days[d] = (o, h, lo, c, v)
    return days

def main():
    # 生产对齐配置（镜像 P2.2 EXIT_CFG：关硬止损/时间止损，仅移动止损 + FIXSTOP）
    prod_cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                           trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                           use_fixed_stop=True, fixed_stop_pct=1.5)
    # 回测默认配置
    def_cfg = make_config()
    for label, cfg in [('DEFAULT(回测默认:硬止损+时间止损)', def_cfg), ('PROD(生产对齐:仅trail+FIXSTOP)', prod_cfg)]:
        tot_n = tot_win = tot_ret = 0
        for sym in SYMS:
            path = f'{DATA_DIR}/{sym}_1m.csv'
            if not os.path.exists(path):
                continue
            days = load_days(path); cost = cost_for_symbol(sym)
            for d in sorted(days.keys()):
                o, h, lo, c, v = days[d]
                if len(c) < 20:
                    continue
                import pandas as pd
                df = pd.DataFrame({'symbol': sym, 'name': sym, 'timestamp': [f'{d} 09:31:00'] * len(c),
                                   'trade_date': [d] * len(c), 'trade_time': [f'{d} 09:31:00'] * len(c),
                                   'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v})
                pc = float(c[0])
                data = build_data(df, pc)
                sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
                trips = simulate_bidirectional(sigs, data, config=cfg, cost=cost)
                for t in trips:
                    tot_n += 1
                    if t['ret_pct'] > 0:
                        tot_win += 1
                    tot_ret += t['ret_pct']
        wr = round(100.0 * tot_win / tot_n, 1) if tot_n else 0.0
        print(f'[{label}] 反T trips={tot_n} WR={wr}% net={round(tot_ret,2)}%')

if __name__ == '__main__':
    main()
