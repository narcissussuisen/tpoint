# -*- coding: utf-8 -*-
"""双引擎对比实测：同一份 161129 07-24 数据，回测引擎 vs 生产引擎"""
import os, sys, json
os.environ['MACD_GATE_MODE'] = 'floor'
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
sys.path.insert(0, os.path.join(BASE, 'core'))
sys.path.insert(0, BASE)

import numpy as np
import pandas as pd
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    from core.miji_alpha import compute_miji_indicators as alpha_compute, detect_miji_signals
    from core.exit_manager import simulate_day, aggregate_metrics, make_config, cost_for_symbol
    import monitor as M

df = pd.read_csv(r'F:/keyfactor_data/1m/161129.SZ_1m.csv', encoding='utf-8-sig')
df['trade_date'] = df['trade_date'].astype(str)
sub = df[df['trade_date'] == '2026-07-24'].reset_index(drop=True)
pc = float(sub['close'].iloc[0])
o = sub['open'].values.astype(float); h = sub['high'].values.astype(float)
lo = sub['low'].values.astype(float); c = sub['close'].values.astype(float)
v = sub['volume'].values.astype(float)

print('='*80)
print('数据: 161129.SZ 2026-07-24  ', len(sub), '根 1m  |  PC(首bar收盘近似)=', pc)
print('实际前收应从日K取：此处用首bar收盘近似（与 signal_chart 一致）')
print('='*80)

# ============ A. 回测引擎 detect_miji_signals（floor 门控，per-symbol 参数） ============
# 生产 per-symbol 参数
_mpr_e, _mpr_p = M.per_symbol_mpr('161129.SZ')
_atr_p = M.per_symbol_atr('161129.SZ')
print(f'\n[per-symbol] 161129.SZ: mpr_enable={_mpr_e} mpr_periods={_mpr_p} atr_min_pct={_atr_p}')

data_a = alpha_compute(o, h, lo, c, v, pc)
sigs_a = detect_miji_signals(data_a, pc, macd_min_hist_diff=0.15,
                             atr_min_pct=_atr_p, mpr_enable=_mpr_e, mpr_periods=_mpr_p)
print(f'\n[回测引擎 detect_miji_signals] 信号 {len(sigs_a)} 个:')
for s in sigs_a:
    print(f"  {s['type']} @idx{s['idx']:>3} price={s['price']:.3f} chg={s['chg']:+.2f}%  {s['detail']}")

# 回测配对 simulate_day
cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                  trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
cost = cost_for_symbol('161129.SZ')
prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data_a['atr'],
          'trend': data_a.get('trend'), 'n': data_a['n']}
trips = simulate_day(sigs_a, prices, cfg, cost=cost)
print(f'\n[回测配对 simulate_day] round-trip {len(trips)} 笔:')
for t in trips:
    print(f"  {t['entry_idx']:>3}->{t['exit_idx']:>3} {t['exit_reason']:>5} 入{t['entry_price']:.3f} 出{t['exit_price']:.3f} 净{t['ret_pct']:+.3f}%")
print('\n[回测指标]', json.dumps(aggregate_metrics(trips), ensure_ascii=False))

# ============ B. 生产引擎 monitor.detect_for（含出场管理状态机） ============
M.STATE['161129.SZ'] = {'PC': pc}
data_b = alpha_compute(o, h, lo, c, v, pc)
data_b['df'] = sub
st = {}
sigs_b = M.detect_for('161129.SZ', '原油LOF', data_b, st,
                      mpr_enable=_mpr_e, mpr_periods=_mpr_p, atr_min_pct=_atr_p)
print(f'\n[生产引擎 monitor.detect_for] 信号 {len(sigs_b)} 个:')
for s in sigs_b:
    print(f"  {s[0]:>1} @{s[12]} price={float(s[1]):.3f} chg={s[2]:+.2f}% tag={s[9]} exit={s[10]} sz={s[13]}成")

# ============ C. 逐信号对齐 ============
print('\n' + '='*80)
print('逐信号对比（时间对齐）')
print('='*80)
bt = {}
for s in sigs_b:
    t = str(s[12])[11:16]
    bt[t] = {'type': s[0], 'price': float(s[1]), 'exit': s[10], 'tag': s[9]}
for s in sigs_a:
    t = sub['trade_time'].iloc[s['idx']][11:16]
    b = bt.get(t, '—')
    mark = '✅' if (b != '—' and b['type'] == s['type']) else ('⚠️' if b != '—' else '❌缺失')
    print(f"  {t} 回测={s['type']}@ {s['price']:.3f} vs 生产={b if b!='—' else '无'} {mark}")
for t in sorted(set(bt.keys()) - {sub['trade_time'].iloc[s['idx']][11:16] for s in sigs_a}):
    print(f"  {t} 回测=— vs 生产={bt[t]} ❌仅生产有")
