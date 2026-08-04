# -*- coding: utf-8 -*-
"""决定性实验：同一标的同一日，三条管线对比
A. signal_chart 管线（miji_alpha + detect_for）— 分时图用
B. backtest_screener 回测管线（miji_alpha detect_miji_signals + simulate_day）— 回测报告用
C. 逐bar 因子快照对比（找出从哪个bar开始分叉）
"""
import os, sys, json
os.environ['MACD_GATE_MODE'] = 'floor'
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
sys.path.insert(0, os.path.join(BASE, 'core'))
sys.path.insert(0, BASE)

import numpy as np
import pandas as pd
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    from core.miji_alpha import compute_miji_indicators, detect_miji_signals, check_miji_trigger
    from core.exit_manager import simulate_day, make_config, cost_for_symbol
    import monitor as M

df = pd.read_csv(r'F:/keyfactor_data/1m/161129.SZ_1m.csv', encoding='utf-8-sig')
df['trade_date'] = df['trade_date'].astype(str)
sub = df[df['trade_date'] == '2026-07-24'].reset_index(drop=True)
pc = float(sub['close'].iloc[0])
o = sub['open'].values.astype(float); h = sub['high'].values.astype(float)
lo = sub['low'].values.astype(float); c = sub['close'].values.astype(float)
v = sub['volume'].values.astype(float)

_mpr_e, _mpr_p = M.per_symbol_mpr('161129.SZ')
_atr_p = M.per_symbol_atr('161129.SZ')

data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)

print('='*90)
print('A. signal_chart 管线（miji_alpha detect_for）vs B. 回测管线（detect_miji_signals）')
print('='*90)

# B 管线：detect_miji_signals + simulate_day
sigs_b = detect_miji_signals(data, pc, macd_min_hist_diff=0.15,
                             atr_min_pct=_atr_p, mpr_enable=_mpr_e, mpr_periods=_mpr_p)
cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                  trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
cost = cost_for_symbol('161129.SZ')
trips = simulate_day(sigs_b, {'o':o,'h':h,'lo':lo,'c':c,'atr':data['atr'],'trend':data.get('trend'),'n':data['n']}, cfg, cost=cost)
print(f'\n[B 回测管线] detect_miji_signals 信号 {len(sigs_b)}:')
for s in sigs_b:
    print(f"  {s['type']} idx{s['idx']:>3} {sub['trade_time'].iloc[s['idx']][11:16]} price={s['price']:.3f} {s['detail']}")
print(f'  simulate_day round-trip: {len(trips)} 笔')
for t in trips:
    print(f"    {t['entry_idx']}->{t['exit_idx']} {t['exit_reason']} 净{t['ret_pct']:+.3f}%")

# A 管线
M.STATE['161129.SZ'] = {'PC': pc}
data['df'] = sub
st = {}
sigs_a = M.detect_for('161129.SZ', '原油LOF', data, st,
                      mpr_enable=_mpr_e, mpr_periods=_mpr_p, atr_min_pct=_atr_p)
print(f'\n[A signal_chart 管线] detect_for 信号 {len(sigs_a)}:')
for s in sigs_a:
    print(f"  {s[0]} {s[12][11:16]} price={float(s[1]):.3f} exit={s[10]} sz={s[13]}成")

print()
print('='*90)
print('C. 逐bar因子快照对比（check_miji_trigger vs detect_miji_signals 内部）')
print('='*90)
# 关键：mpr 过滤是 B 侧唯一差异点吗？逐 bar 看 macd60_hist 方向
mp_hist = data['macd60_hist']
print('macd60_hist 符号分布: 负=%d 正=%d 零=%d (n=%d)' % (
    int((mp_hist < 0).sum()), int((mp_hist > 0).sum()), int((mp_hist == 0).sum()), len(mp_hist)))
# 找所有 m_factor=1 的bar（潜在B）及 mpr 拦截情况
print('\n潜在 B bar（m_factor==1，即 strict 基础放行）:')
for i in range(2, data['n']):
    g, gd = None, None
    if data['atr'][i] <= 0: continue
    # 用 check_miji_trigger 取因子
    b, s, bd, sd, snap = check_miji_trigger(data, i, min_hist_diff=0.15,
                                            atr_min_pct=_atr_p,
                                            mpr_enable=_mpr_e, mpr_periods=_mpr_p)
    if snap['macd_div'] == 1 or b:
        t = sub['trade_time'].iloc[i][11:16]
        mpv = mp_hist[i]
        mpr_ok = (mpv < 0)
        print(f"  {t} idx{i:>3} c={c[i]:.3f} g={snap['gravity']} m={snap['macd_div']} "
              f"macd60_hist={mpv:+.5f} mpr通过={'是' if mpr_ok else '否'} "
              f"trigger_B={'是' if b else '否'} {bd[:40] if bd else ''}")
