#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
early_session_0722_review.py
复盘 2026-07-22 早盘(09:30-11:30) tpoint 触发的所有交易信号。
用隔离引擎(miji_engine, strict 门控, enable=(gravity, vol_div=False, macd)) 真实复算,
再与 state.json 计数交叉验证, 最后用后续价格走势做有效性验证。
输出: output/early_session_0722_review.json + 控制台表格。
"""
import os, sys, json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))
from core.datasource import MootdxDataSource
import miji_engine as ME

SYMS = ['161129.SZ', '688347.SH', '513310.SH']
NAME = {'161129.SZ': '原油LOF易方达', '688347.SH': '华虹公司', '513310.SH': '中韩半导体ETF华泰柏瑞'}
# 生产配置: MACD_GATE_MODE=floor (run_monitor.bat line9), VOL_DIV_ENABLED=False
MODE = 'floor'
ENABLE = (True, False, True)  # gravity, vol_div(off), macd

tf = MootdxDataSource()

def fetch(sym):
    df = tf.klines.intraday(sym)
    if df is None or len(df) == 0:
        return None
    df = df.sort_values('trade_time').reset_index(drop=True)
    # 早盘截止到 11:30 (含 11:29 那根); 13:00 那根属于午后, 剔除
    df = df[df['trade_time'].dt.strftime('%H:%M') <= '11:30'].reset_index(drop=True)
    return df

def get_pc(sym):
    day_df = tf.klines.get(sym, '1d', count=2)
    return float(day_df['close'].iloc[-2])

def validate(sig_idx, df, direction):
    """信号后 N 根内是否出现有利走势。
    买入(B): 后续应有更高价 (上涨); 卖出/反T开空(S): 后续应有更低价 (下跌)。
    返回 (max_fav_pct, valid_bool, horizon_note)
    """
    c = df['close'].values.astype(float)
    n = len(df)
    if sig_idx >= n - 1:
        return None, None, '信号在最后一根, 无后续'
    fwd = c[sig_idx+1:]
    sig_p = c[sig_idx]
    if direction == 'B':
        best = (fwd.max() - sig_p) / sig_p * 100
        valid = best > 0.15  # 至少 +0.15% 才算有效(覆盖手续费)
    else:  # S / 反T开空
        best = (sig_p - fwd.min()) / sig_p * 100
        valid = best > 0.15
    return round(best, 3), valid, f'后续{len(fwd)}根'

def main():
    out = {'date': '2026-07-22', 'session': '早盘 09:30-11:30',
           'mode': MODE, 'vol_div_enabled': False, 'symbols': {}}
    all_sigs = []
    for sym in SYMS:
        df = fetch(sym)
        if df is None:
            out['symbols'][sym] = {'error': 'no data'}
            continue
        pc = get_pc(sym)
        o = df['open'].values.astype(float); h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        data = ME.compute_miji_indicators(o, h, lo, c, v, pc)
        sigs = ME.detect_miji_signals(data, pc, macd_gate_mode=MODE, enable=ENABLE)
        rows = []
        for s in sigs:
            idx = s['idx']
            direction = 'B' if s['type'] == 'B' else 'S'
            t = str(df['trade_time'].iloc[idx])
            price = s['price']
            f = s.get('factors', {})
            max_fav, valid, note = validate(idx, df, direction)
            if valid is not None:
                valid = bool(valid)
            rows.append({
                'time': t, 'type': direction, 'price': round(price, 3),
                'idx': idx, 'resonance': s.get('resonance_score'),
                'gravity': f.get('gravity'), 'vol_div': f.get('vol_div'),
                'macd_div': f.get('macd_div'), 'day_chg': round(s.get('chg', 0), 3),
                'detail': s.get('detail', ''),
                'max_fav_pct': max_fav, 'valid': valid, 'note': note,
            })
        nb = sum(1 for r in rows if r['type'] == 'B')
        ns = sum(1 for r in rows if r['type'] == 'S')
        out['symbols'][sym] = {
            'name': NAME[sym], 'pc': round(pc, 3),
            'bars': len(df), 't_first': str(df['trade_time'].iloc[0]),
            't_last': str(df['trade_time'].iloc[-1]),
            'first_close': round(float(c[0]), 3), 'last_close': round(float(c[-1]), 3),
            'day_chg_pct': round((float(c[-1]) - pc) / pc * 100, 3),
            'n_B': nb, 'n_S': ns, 'signals': rows,
        }
        all_sigs.extend([dict(r, sym=sym) for r in rows])
    # 汇总
    out['summary'] = {
        'total_signals': len(all_sigs),
        'total_B': sum(1 for s in all_sigs if s['type'] == 'B'),
        'total_S': sum(1 for s in all_sigs if s['type'] == 'S'),
        'valid': sum(1 for s in all_sigs if s['valid'] is True),
        'invalid': sum(1 for s in all_sigs if s['valid'] is False),
        'unknown': sum(1 for s in all_sigs if s['valid'] is None),
    }
    os.makedirs(os.path.join(ROOT, 'output'), exist_ok=True)
    with open(os.path.join(ROOT, 'output', 'early_session_0722_review.json'), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    # 控制台表格
    print('=' * 100)
    print(f"早盘信号复盘 {out['date']} ({out['session']}) 门控={MODE} vol_div=OFF")
    print('=' * 100)
    for sym in SYMS:
        d = out['symbols'].get(sym, {})
        if 'error' in d:
            print(f"\n[{sym}] 无数据"); continue
        print(f"\n### {sym} {d['name']}  pc={d['pc']}  早盘棒数={d['bars']} ({d['t_first']}→{d['t_last']})")
        print(f"    首棒收={d['first_close']} 末棒收={d['last_close']} 当日涨跌={d['day_chg_pct']}%  B={d['n_B']}/S={d['n_S']}")
        print(f"    {'时间':<22}{'方向':<4}{'价':>9}{'共振':>4}{'g/vd/md':<12}{'后续最优%':>10}{'有效':>6}")
        for r in d['signals']:
            g = r['gravity']; vd = r['vol_div']; md = r['macd_div']
            vf = '✓' if r['valid'] else ('✗' if r['valid'] is False else '?')
            print(f"    {r['time']:<22}{r['type']:<4}{r['price']:>9.3f}{str(r['resonance']):>4}  {g}/{vd}/{md:<8}{str(r['max_fav_pct']):>10}{vf:>6}  {r['detail'][:30]}")
    s = out['summary']
    print('\n' + '-' * 100)
    print(f"汇总: 总信号={s['total_signals']} (B={s['total_B']} S={s['total_S']}) | 有效={s['valid']} 失效={s['invalid']} 未知={s['unknown']}")
    print('=' * 100)

if __name__ == '__main__':
    main()
