# -*- coding: utf-8 -*-
"""调试：为什么 F盘 161129 07-24 裸触发有信号但 detect_for=0"""
import os, sys
os.environ['MACD_GATE_MODE'] = 'floor'
sys.path.insert(0, os.path.join(os.getcwd(), 'core'))
sys.path.insert(0, os.getcwd())
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    import numpy as np, pandas as pd
    from miji_alpha import compute_miji_indicators
    import monitor as M

df = pd.read_csv('F:/keyfactor_data/1m/161129.SZ_1m.csv', encoding='utf-8-sig')
d = df[df['trade_date'] == '2026-07-24'].reset_index(drop=True)
c = d['close'].values.astype(float)
h = d['high'].values.astype(float)
lo = d['low'].values.astype(float)
o = d['open'].values.astype(float)
v = d['volume'].values.astype(float)
pc = float(d['close'].iloc[0])
data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
n = data['n']
trade_times = d['trade_time'].values

st = {}
b_count = 0; s_count = 0
pos = None
sig_cnt = 0
for i in range(n):
    # 出场段：检查当前持仓是否触发出场（复刻 monitor 逻辑核心）
    if pos is not None:
        atr_i = data['atr'][i]
        # hard stop（多仓）
        if pos['side'] == 'long':
            stop = pos['stop_price']
            if c[i] <= stop:
                pos = None
                continue
        else:
            stop = pos['stop_price']
            if c[i] >= stop:
                pos = None
                continue
        # trail
        if pos['side'] == 'long':
            pos['max_fav'] = max(pos['max_fav'], float(c[i]))
            fav = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
            if fav >= M.EXIT_CFG['trail_activate_pct']:
                tr = pos['max_fav'] * (1 + M.EXIT_CFG['trail_pct'] / 100.0)
                if c[i] >= tr and tr < pos['stop_price']:
                    pos = None
                    continue
        else:
            pos['max_fav'] = min(pos['max_fav'], float(c[i]))
            fav = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
            if fav >= M.EXIT_CFG['trail_activate_pct']:
                tr = pos['max_fav'] * (1 - M.EXIT_CFG['trail_pct'] / 100.0)
                if c[i] <= tr and tr > pos['stop_price']:
                    pos = None
                    continue
        # time stop
        if (i - pos['entry_idx']) >= M.EXIT_CFG['time_stop_bars']:
            pos = None
            continue

    tb, rb = M.check_b_trigger(data, i)
    ts, rs = M.check_s_trigger(data, i)
    if not (tb or ts):
        continue
    dev = (c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0
    if tb:
        s_pct = M.strength_size(dev, 'MACD' in (rb or ''))
        last_b = st.get('_cooldown_161129.SZ_B', -9999)
        if s_pct > 0 and (i - last_b) >= M.COLDOWN_BARS and b_count < M.MAX_B_DAILY:
            st['_cooldown_161129.SZ_B'] = i
            b_count += 1
            if pos is None:
                sig_cnt += 1
                pos = {'side': 'long', 'entry_price': float(c[i]), 'entry_idx': i, 'size_pct': s_pct,
                       'max_fav': float(c[i]), 'stop_price': M._compute_stop_price(float(c[i]), data['atr'], i, M.EXIT_CFG)}
                print('B@%d %s dev=%+.2f%% 建多 %d成 stop=%.3f' % (i, trade_times[i], dev, s_pct, pos['stop_price']))
            elif pos['side'] == 'long':
                add = min(s_pct, M.MAX_SIZE_PCT - pos['size_pct'])
                if add > 0:
                    pos['size_pct'] += add
                    pos['max_fav'] = max(pos['max_fav'], float(c[i]))
                    sig_cnt += 1
                    print('B@%d %s dev=%+.2f%% 加多 %d成' % (i, trade_times[i], dev, add))
            else:
                sz = min(s_pct, pos['size_pct'])
                if sz > 0:
                    sig_cnt += 1
                    pos['size_pct'] -= sz
                    print('B@%d %s dev=%+.2f%% 平空 %d成' % (i, trade_times[i], dev, sz))
                    if pos['size_pct'] <= 0:
                        pos = None
    if ts:
        s_pct = M.strength_size(dev, 'MACD' in (rs or ''))
        last_s = st.get('_cooldown_161129.SZ_S', -9999)
        if s_pct > 0 and (i - last_s) >= M.COLDOWN_BARS and s_count < M.MAX_S_DAILY:
            st['_cooldown_161129.SZ_S'] = i
            s_count += 1
            if pos is None:
                sig_cnt += 1
                pos = {'side': 'short', 'entry_price': float(c[i]), 'entry_idx': i, 'size_pct': s_pct,
                       'max_fav': float(c[i]), 'stop_price': M._compute_stop_price(float(c[i]), data['atr'], i, M.EXIT_CFG)}
                print('S@%d %s dev=%+.2f%% 建空 %d成 stop=%.3f' % (i, trade_times[i], dev, s_pct, pos['stop_price']))
            elif pos['side'] == 'short':
                add = min(s_pct, M.MAX_SIZE_PCT - pos['size_pct'])
                if add > 0:
                    pos['size_pct'] += add
                    pos['max_fav'] = min(pos['max_fav'], float(c[i]))
                    sig_cnt += 1
                    print('S@%d %s dev=%+.2f%% 加空 %d成' % (i, trade_times[i], dev, add))
            else:
                sz = min(s_pct, pos['size_pct'])
                if sz > 0:
                    sig_cnt += 1
                    pos['size_pct'] -= sz
                    print('S@%d %s dev=%+.2f%% 平多 %d成' % (i, trade_times[i], dev, sz))
                    if pos['size_pct'] <= 0:
                        pos = None
print('简化状态机信号数:', sig_cnt)
