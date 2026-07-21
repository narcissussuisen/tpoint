#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exec_compare_0721.py — 2026-07-21 两标的交易执行复盘 + strict vs floor 算法对比
  1) 实时执行: 从 data/state.json 读 live 实际信号/持仓
  2) 算法重放: 同一隔离引擎对当日全日已收盘1m棒, 分别 strict(生产) / floor(拟flip) 跑信号
  3) 准确率: 因果前向收益 fwd@6/12/24 (信号棒收盘 -> 之后k棒收盘)
  4) 盈亏: 用生产 EXIT_CFG(trailing 0.4/0.6, 硬/时间止损关) 做单仓正向T配对模拟
  5) 输出 output/exec_compare_2026_07_21.json 供 build_exec_html.py 渲染

因果约束: 仅当日已收盘棒; pc 取前一日1d收盘; 无未来函数.
"""
import os, sys, json
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

from core.datasource import MootdxDataSource
from core.exit_manager import make_config, simulate_day, aggregate_metrics
import miji_engine as ME

DATE = '2026-07-21'
SYMS = [('161129.SZ', '原油LOF易方达'), ('688347.SH', '华虹宏力')]
MODES = ('strict', 'floor')
FWD_K = (6, 12, 24)

# 生产出场配置(来自 monitor.py:79)
EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)


def fwd_ret(c, idx, k):
    j = idx + k
    if j >= len(c) or c[idx] <= 0:
        return None
    return (c[j] - c[idx]) / c[idx] * 100.0


def load_state():
    p = os.path.join(ROOT, 'data', 'state.json')
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    st = load_state()
    ds = MootdxDataSource()
    out = {'date': DATE, 'exit_cfg': 'trailing only (0.4/0.6), hard/time stop OFF',
           'symbols': {}, 'live': {}}

    for sym, label in SYMS:
        print('=' * 70)
        print(f'处理 {sym} {label} ...')
        df = ds.intraday(sym)
        if df is None or len(df) == 0:
            print(f'  ❌ 取不到 {sym} 行情'); continue
        df = df.sort_values('trade_time').reset_index(drop=True)
        n = len(df)
        t_last = str(df['trade_time'].iloc[-1])
        times = [str(df['trade_time'].iloc[i]) for i in range(n)]
        o = df['open'].values.astype(float); h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        try:
            day_df = ds.get(sym, '1d', count=2)
            pc = float(day_df['close'].iloc[-2])
            prev_close = float(day_df['close'].iloc[-2])
        except Exception as e:
            print(f'  ❌ 前收失败 {e}'); continue
        data = ME.compute_miji_indicators(o, h, lo, c, v, pc)
        data['n'] = n

        # 行情概要
        day_open = o[0]; day_high = h.max(); day_low = lo.min(); day_close = c[-1]
        day_chg = (day_close / prev_close - 1) * 100
        intraday_range = (day_high / day_low - 1) * 100
        total_vol = v.sum()
        med_vol = np.median(v)
        vol_ratio = total_vol / med_vol if med_vol > 0 else 0
        # 最大偏离 VWAP
        vwap = data['vwap']
        dev = (c - vwap) / vwap * 100
        max_os = dev.min(); max_ob = dev.max()

        sym_block = {
            'label': label, 'n_bars': n, 't_last': t_last,
            'ohlc': {'open': round(day_open, 3), 'high': round(day_high, 3),
                     'low': round(day_low, 3), 'close': round(day_close, 3),
                     'prev_close': round(prev_close, 3),
                     'day_chg_pct': round(day_chg, 2),
                     'intraday_range_pct': round(intraday_range, 2),
                     'vol_ratio': round(vol_ratio, 1),
                     'max_os_dev_pct': round(float(max_os), 2),
                     'max_ob_dev_pct': round(float(max_ob), 2)},
            'modes': {},
        }

        for mode in MODES:
            sigs = ME.detect_miji_signals(data, pc, macd_gate_mode=mode, enable=(True, True, True))
            sig_list = []
            for s in sigs:
                idx = s['idx']
                bs = 'B' if s['type'] == 'B' else 'S'
                sig_list.append({
                    'idx': idx, 'time': times[idx], 'dir': bs,
                    'price': round(float(s['price']), 3),
                    'resonance': s.get('resonance_score'),
                    'detail': s.get('detail', ''),
                    'fwd6': fwd_ret(c, idx, 6), 'fwd12': fwd_ret(c, idx, 12),
                    'fwd24': fwd_ret(c, idx, 24),
                })
            # 准确率
            b = [r for r in sig_list if r['dir'] == 'B']
            s = [r for r in sig_list if r['dir'] == 'S']
            def acc(sigs, want_up):
                vals = [r['fwd12'] for r in sigs if r['fwd12'] is not None]
                if not vals: return None
                return round(sum(1 for x in vals if (x > 0) == want_up) / len(vals) * 100, 1)
            def meanf(sigs):
                vals = [r['fwd12'] for r in sigs if r['fwd12'] is not None]
                return round(sum(vals)/len(vals), 2) if vals else None
            # 盈亏(round-trip 配对)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'n': n}
            trips = simulate_day(sigs, prices, EXIT_CFG)
            agg = aggregate_metrics(trips)
            # 灵敏度: 首信号时间 / 频率(信号/小时)
            if sig_list:
                first_t = sig_list[0]['time']
                hours = (n / 60.0) or 1
                freq = round(len(sig_list) / hours, 2)
            else:
                first_t = None; freq = 0.0
            sym_block['modes'][mode] = {
                'n_sig': len(sig_list), 'nB': len(b), 'nS': len(s),
                'first_time': first_t, 'freq_per_h': freq,
                'B_acc12': acc(b, True), 'S_acc12': acc(s, False),
                'B_mean_fwd12': meanf(b), 'S_mean_fwd12': meanf(s),
                'pnl': agg, 'signals': sig_list,
            }
            print(f'  [{mode}] 信号 {len(sig_list)} (B{len(b)}/S{len(s)}) | '
                  f'B准确率@12m={acc(b,True)} S准确率@12m={acc(s,False)} | '
                  f'配对 {agg["total"]} 笔 总收益 {agg["total_ret"]}% 胜率 {agg["win_rate"]}%')

        # ---- live 实时执行 ----
        b_key = f'_b_count_{sym}_{DATE.replace("-","")}'
        s_key = f'_s_count_{sym}_{DATE.replace("-","")}'
        live_b = st.get(b_key); live_s = st.get(s_key)
        pos = st.get(f'pos_{sym}')
        live_block = {
            'b_count': live_b if live_b is not None else 0,
            's_count': live_s if live_s is not None else 0,
            'scanned': (live_b is not None or live_s is not None or pos is not None
                        or any(k.startswith(f'bar_{sym}') for k in st.keys())),
            'position': pos,
        }
        out['live'][sym] = live_block
        out['symbols'][sym] = sym_block

    with open(os.path.join(ROOT, 'output', 'exec_compare_2026_07_21.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('=' * 70)
    print('JSON -> output/exec_compare_2026_07_21.json')


if __name__ == '__main__':
    main()
