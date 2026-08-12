"""验证当前 recalc 引擎(replay_symbol gates='prod')是否与生产 detect_for 同源校准。
应与 replay_0811_diag.py 直接 detect_for 结果一致: 161129=0, 300757=4。
"""
import sys, os
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = BASE + r'\core'
sys.path.insert(0, CORE)
sys.path.insert(0, BASE + r'\venv\Lib\site-packages')
os.chdir(CORE)

import pandas as pd
from miji_alpha import compute_miji_indicators
import daily_signal_review as dr
import monitor
monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None

SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}
DATE = '2026-08-11'; FROOT = r'F:\keyfactor_data\1m'

print('=== replay_symbol(gates=prod) 重算 08-11 ===')
for sym, name in SYMS.items():
    df = pd.read_csv(f'{FROOT}/{sym}_1m.csv')
    df = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
    if len(df) < 10:
        print(f'{name}: 数据不足'); continue
    prev = df[df['trade_date'] < DATE]
    pc = float(prev['close'].iloc[-1]) if len(prev) else {'161129.SZ':1.7,'513310.SH':4.899,'300757.SZ':488.18}[sym]
    o=df['open'].values.astype(float); h=df['high'].values.astype(float)
    lo=df['low'].values.astype(float); c=df['close'].values.astype(float); v=df['volume'].values.astype(float)
    data = compute_miji_indicators(o,h,lo,c,v,pc); data['df'] = df
    monitor.STATE[sym] = {'PC': pc, 'WARM': None}
    rows, stats = dr.replay_symbol(sym, name, data, pc, gates='prod')
    print(f'\n{name}({sym}) pc={pc:.3f} recalc信号数={len(rows)}')
    for r in rows:
        print(f"   {r['type']:>2} {r['time']} px={r['price']:.3f} band={r.get('band','')} valid={r.get('valid')}")
print('\n>>> 期望: 161129=0, 513310=0, 300757=4(含X)。若 161129>0 说明 recalc 仍未接 atr 门控(校准未生效)。')
