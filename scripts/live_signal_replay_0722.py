#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_signal_replay_0722.py
直接调用生产 monitor.detect_for 复算 2026-07-22 早盘真实触发的信号。
用真实行情数据 + 干净 state, 复现生产门控(check_b_trigger/check_s_trigger + 仓位/冷却/去重),
得到与 state.json 计数一致的真实触发序列。
输出: output/live_signals_0722.json + 控制台表格。
"""
import os, sys, json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

import monitor as M

SYMS = ['161129.SZ', '688347.SH', '513310.SH']
NAME = {'161129.SZ': '原油LOF易方达', '688347.SH': '华虹公司', '513310.SH': '中韩半导体ETF华泰柏瑞'}

def main():
    # 强制 floor 门控与生产一致
    os.environ['MACD_GATE_MODE'] = 'floor'
    # 重新加载 monitor 以应用 env (已在 import 时读, 这里手动覆盖模块常量)
    M.MACD_GATE_MODE = 'floor'
    # 初始化 tf 数据源 (生产在 run() 内懒加载, 这里手动建)
    M.tf = M.TickFlow()
    try:
        _ = M.tf.client
    except Exception as e:
        print("tf init warn:", e)

    out = {'date': '2026-07-22', 'session': '早盘 09:30-11:30',
           'mode': 'floor (production)', 'symbols': {}}
    all_sig = []
    for sym in SYMS:
        name = NAME[sym]
        # refresh_daily 取 PC + WARM
        try:
            M.refresh_daily(sym)
        except Exception as e:
            print(f"[{sym}] refresh_daily err {e}")
        # compute 需要 tf 数据源; 生产懒加载, import 后 tf 可能为 None -> 触发初始化
        # 直接调用 compute 会内部用 tf
        try:
            data = M.compute(sym)
        except Exception as e:
            print(f"[{sym}] compute err {e}")
            out['symbols'][sym] = {'error': str(e)}
            continue
        if data is None:
            out['symbols'][sym] = {'error': 'compute returned None (tf unavailable)'}
            continue
        # 干净 state (模拟当日从零开始)
        st = {}
        sigs = M.detect_for(sym, name, data, st)
        rows = []
        c = data['c']
        tt = data['df']['trade_time'].values if data.get('df') is not None else None
        for s in sigs:
            # s 是 tuple: (op, price, chg, level, level_type, rsi, temp, vol_r, name, tag, exit_reason, day_chg, bar_trade_time, pos_pct)
            op = s[0]
            price = s[1]
            bar_tt = s[12] if len(s) > 12 else ''
            tag = s[9] if len(s) > 9 else ''
            exit_reason = s[10] if len(s) > 10 else ''
            pos_pct = s[13] if len(s) > 13 else None
            # 找 idx
            idx = -1
            if tt is not None:
                for k, t in enumerate(tt):
                    if str(t) == str(bar_tt):
                        idx = k; break
            # 后续验证
            if idx >= 0 and idx < len(c) - 1:
                fwd = c[idx+1:]
                if op in ('B',):
                    best = (fwd.max() - price) / price * 100
                    valid = best > 0.15
                elif op in ('S', 'EXIT', 'STOP', 'TRAIL', 'TIME'):
                    best = (price - fwd.min()) / price * 100
                    valid = best > 0.15
                else:
                    best = None; valid = None
            else:
                best = None; valid = None
            rows.append({
                'time': str(bar_tt), 'type': op, 'price': round(float(price), 3),
                'tag': tag, 'exit_reason': exit_reason, 'pos_pct': pos_pct,
                'idx': idx, 'max_fav_pct': round(float(best), 3) if best is not None else None,
                'valid': bool(valid) if valid is not None else None,
            })
        nb = sum(1 for r in rows if r['type'] == 'B')
        ns = sum(1 for r in rows if r['type'] in ('S', 'EXIT'))
        out['symbols'][sym] = {
            'name': name, 'pc': round(M.STATE[sym]['PC'], 3),
            'n_signals': len(rows), 'n_B': nb, 'n_S_EXIT': ns, 'signals': rows,
        }
        all_sig.extend([dict(r, sym=sym) for r in rows])

    # 与 state.json 计数交叉验证
    try:
        stj = json.load(open(os.path.join(ROOT, 'data', 'state.json'), encoding='utf-8'))
        today = '20260722'
        for sym in SYMS:
            b = stj.get(f'_b_count_{sym}_{today}', 0)
            s = stj.get(f'_s_count_{sym}_{today}', 0)
            rep = out['symbols'].get(sym, {})
            rep['state_json_B'] = b
            rep['state_json_S'] = s
            rep['match'] = (rep.get('n_B') == b and rep.get('n_S_EXIT') == s)
    except Exception as e:
        print("state.json cross-check err", e)

    os.makedirs(os.path.join(ROOT, 'output'), exist_ok=True)
    with open(os.path.join(ROOT, 'output', 'live_signals_0722.json'), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    # 控制台
    print('=' * 95)
    print(f"生产级信号复算 2026-07-22 早盘  门控=floor (与生产 run_monitor.bat 一致)")
    print('=' * 95)
    for sym in SYMS:
        d = out['symbols'].get(sym, {})
        if 'error' in d:
            print(f"\n[{sym}] ERROR {d['error']}"); continue
        mk = '✓' if d.get('match') else '✗(不一致)'
        print(f"\n### {sym} {d['name']}  pc={d['pc']}  B={d['n_B']}/S·EXIT={d['n_S_EXIT']}  "
              f"[state.json B={d.get('state_json_B')}/S={d.get('state_json_S')} {mk}]")
        print(f"    {'时间':<22}{'类型':<6}{'价':>9}  {'仓位':>4}  {'有效':>5}  后续最优%  触发条件")
        for r in d['signals']:
            vf = '✓' if r['valid'] else ('✗' if r['valid'] is False else '?')
            cond = (r['tag'] or '') + (' ' + r['exit_reason'] if r['exit_reason'] else '')
            print(f"    {r['time']:<22}{r['type']:<6}{r['price']:>9.3f}  {str(r['pos_pct']):>4}  {vf:>5}  {str(r['max_fav_pct']):>9}  {cond[:32]}")
    print('\n' + '=' * 95)

if __name__ == '__main__':
    main()
