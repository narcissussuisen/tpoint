#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""ra_zero_signal_grid.py — R-A：零信号标的联合网格归因寻优（0805 迭代）

目标（计划 self_iteration_plan_v2 P1/P2）：161129/513310/600570 全天有波动零信号（08-04 实测），
定位卡死闸门并给出最小松绑组合。
网格：atr_min_pct {0.10,0.15,0.25} × TP_MHD_THRESHOLD {0.08,0.10,0.15} × mpr {关,[60]}
      = 27 组合 × 3 标的 × F盘全历史（生产同源信号 + simulate_day trail 0.4/0.6 + 生产出口径成本）
指标（用户七项）：净胜率(主)/盈亏比/总收益率/最大回撤/夏普/日均信号数/全集口径不降(对照当前生产组合)
产出：output/ra_zero_signal_<date>.json
"""
import os, sys, json, datetime, itertools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import monitor as M
import factor_optimizer as FO
from exit_manager import aggregate_metrics

SYMS = ['161129.SZ', '513310.SH', '600570.SH']   # 08-04 零推送标的
ATR_G = [0.10, 0.15, 0.25]
MHD_G = [0.08, 0.10, 0.15]
MPR_G = [None, [60]]
CUR = {'atr': 0.25, 'mhd': 0.15, 'mpr': [60]}

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))


def full_metrics(trips, n_days, n_signals):
    m = aggregate_metrics(trips)
    return {'n_trips': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
            'total_ret': m.get('total_ret_pct', 0), 'max_dd': m.get('max_drawdown_pct'),
            'sharpe': m.get('sharpe'), 'sig_per_day': round(n_signals / max(n_days, 1), 2)}


def run_cell(sym, name, days, atr_v, mhd_v, mpr_v):
    if sym in M.PER_SYMBOL_CFG:
        M.PER_SYMBOL_CFG[sym]['atr_min_pct'] = atr_v
        M.PER_SYMBOL_CFG[sym]['mpr_enable'] = 'B' if mpr_v else None
        M.PER_SYMBOL_CFG[sym]['mpr_periods'] = mpr_v
    os.environ['TP_MHD_THRESHOLD'] = str(mhd_v)
    n_sig = 0
    sig_days = []
    for d, data, g in days:
        data['sym'] = sym
    sig_days = FO.day_signals(sym, name, days, atr_v)   # 内部读 PER_SYMBOL_CFG/mpr + env MHD
    # day_signals 内部只传 atr；mpr 覆盖在上面已写入 PER_SYMBOL_CFG，detect_for 经 per_symbol_mpr 读取
    n_sig = sum(len(s) for _, _, s in sig_days)
    trips = FO.eval_config(sig_days, *FO.CUR_TRAIL)
    return full_metrics(trips, len(days), n_sig)


def main():
    date = datetime.date.today().strftime('%Y-%m-%d')
    rep = {'date': date, 'grid': {'atr': ATR_G, 'mhd': MHD_G, 'mpr': ['off', [60]]},
           'current': CUR, 'symbols': {}}
    for sym in SYMS:
        name = wl[sym]
        days = FO.sym_days(sym)
        cells = {}
        for atr_v, mhd_v, mpr_v in itertools.product(ATR_G, MHD_G, MPR_G):
            key = f'atr{atr_v}+mhd{mhd_v}+mpr{"60" if mpr_v else "off"}'
            try:
                cells[key] = run_cell(sym, name, days, atr_v, mhd_v, mpr_v)
            except Exception as e:
                cells[key] = {'error': str(e)}
            print(f'[{sym}] {key} -> sig/d={cells[key].get("sig_per_day")} wr={cells[key].get("win_rate")}', flush=True)
        cur_key = f'atr{CUR["atr"]}+mhd{CUR["mhd"]}+mpr60'
        base = cells.get(cur_key, {})
        # 候选：日均信号 1~12 且净胜率 ≥ 基线+1pp（薄样本 161129/513310 +2pp）
        gate = 2.0 if len(days) < 80 else 1.0
        ok = []
        for k, m in cells.items():
            if 'error' in m or k == cur_key:
                continue
            if 1.0 <= m['sig_per_day'] <= 12 and m['win_rate'] >= base.get('win_rate', 0) + gate and m['n_trips'] >= 30:
                ok.append((k, m))
        ok.sort(key=lambda x: -x[1]['win_rate'])
        rep['symbols'][sym] = {'name': name, 'n_days': len(days), 'baseline': base,
                               'cells': cells, 'top_candidates': ok[:3]}
        print(f'== {sym} {name}: 基线 sig/d={base.get("sig_per_day")} wr={base.get("win_rate")}% | 达标候选 {len(ok)} 个')
    out = os.path.join(ROOT, 'output', f'ra_zero_signal_{date}.json')
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'[ok] {out}')


if __name__ == '__main__':
    main()
