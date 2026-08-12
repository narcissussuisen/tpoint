"""独立重放 2026-08-11：用生产同源 monitor.detect_for 重放 F 盘 tickflow 1m 数据，
判定 08-11 当日生产引擎本应产出多少信号（无首扫抑制、空仓起步）。
打桩 emit 防止污染真实 signal.txt / 飞书。
"""
import sys, os, json
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = BASE + r'\core'
sys.path.insert(0, CORE)
sys.path.insert(0, BASE + r'\venv\Lib\site-packages')
os.chdir(CORE)

import pandas as pd
from datetime import datetime
from miji_alpha import compute_miji_indicators
import monitor

# 打桩：防止副作用
monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None
monitor._append_signal_txt = lambda *a, **k: None
monitor.push_batch = lambda *a, **k: None

SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}
DATE = '2026-08-11'
FROOT = r'F:\keyfactor_data\1m'

try:
    with open(f'{BASE}/data/monitor_config.json') as f:
        cfg_all = json.load(f)
except Exception as e:
    cfg_all = {}
    print('cfg load fail', e)

total_b = total_s = 0
for sym, name in SYMS.items():
    csv = f'{FROOT}/{sym}_1m.csv'
    if not os.path.exists(csv):
        print(f'{name}: 无 {csv}'); continue
    df = pd.read_csv(csv)
    df = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
    if len(df) < 10:
        print(f'{name}: 08-11 数据不足 {len(df)}'); continue
    prev = df[df['trade_date'] < DATE]
    if len(prev):
        pc = float(prev['close'].iloc[-1])
    else:
        pc_map = {'161129.SZ': 1.7, '513310.SH': 4.899, '300757.SZ': 488.18}
        pc = pc_map[sym]
    o = df['open'].values.astype(float); h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    data = compute_miji_indicators(o, h, lo, c, v, pc)
    data['df'] = df
    monitor.STATE[sym] = {'PC': pc, 'WARM': None}
    cfg = cfg_all.get(sym, {})
    atr_p = cfg.get('atr_min_pct'); mpr_e = cfg.get('mpr_enable'); mpr_p = cfg.get('mpr_periods')
    st = {}  # 无已处理标记 => 等价于无首扫抑制、空仓起步
    sigs = monitor.detect_for(sym, name, data, st,
                              mpr_enable=mpr_e, mpr_periods=mpr_p, atr_min_pct=atr_p)
    times = pd.to_datetime(df['trade_time']).dt.strftime('%H:%M:%S').values
    nb = sum(1 for s in sigs if s[0] == 'B')
    ns = sum(1 for s in sigs if s[0] == 'S')
    total_b += nb; total_s += ns
    print(f'\n=== {name}({sym}) pc={pc:.3f} bars={len(df)} atr_min={atr_p} mpr={mpr_e}/{mpr_p} ===')
    print(f'  生产引擎 detect_for 应产出: B={nb} S={ns} 合计={len(sigs)}')
    for s in sigs:
        baridx = None
        # s 结构: (typ, price, chg, std, reason, rsi, temp, volr, name, tag, exit_reason, day_chg, trade_time, size_pct)
        tt = s[12] if len(s) > 12 else '?'
        print(f'   {s[0]} @ {tt} px={s[1]:.3f} chg={s[2]:.2f}% reason={s[4]} size={s[-1]}')

print(f'\n>>> 08-11 生产引擎(无首扫抑制)本应产出: B={total_b} S={total_s}')
print('>>> 若 B+S>0 而 monitor 实发0 => 实盘漏检/漏推(bug或首扫抑制); 若=0 => 当日确无信号, reconcile recalc 算错')
