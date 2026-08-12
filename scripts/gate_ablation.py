#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""gate_ablation.py — 零信号标的闸门消融探针（2026-08-05 闭环升级）

问题：某标的当天有显著波动却零推送时，factor_optimizer 调 atr/trail 数值无意义，
需要先回答「哪道闸门卡死了全部信号」。本探针对指定交易日重放，逐个放开
per-symbol 闸门（atr_min_pct / mpr / vol_confirm），统计各组合下的信号量，
定位主卡死闸门并量化放开后的信号收益空间。

CLI：python scripts/gate_ablation.py --date 2026-08-05 --syms 161129.SZ,513310.SH
输出：output/gate_ablation_<date>.json + stdout 摘要行
"""
import os, sys, json, argparse, itertools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import monitor as M
import daily_signal_review as R
from backtest_screener import load_1m_csv, group_by_day, day_prev_close

F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')

# 消融组合：基线 + 单闸门放开 + 全放开
ABLATIONS = [
    ('baseline(生产配置)', {}),
    ('atr关', {'atr_min_pct': None}),
    ('mpr关', {'mpr_enable': None}),
    ('vol_confirm关', {'vol_confirm': False}),
    ('atr+mpr关', {'atr_min_pct': None, 'mpr_enable': None}),
    ('全放开', {'atr_min_pct': None, 'mpr_enable': None, 'vol_confirm': False}),
]


def _quiet(*_a, **_k):
    pass


def replay_with(sym, name, data, pc, overrides):
    """按 overrides 计算真实闸门入参后重放，返回信号 op 列表。

    [2026-08-11 P0 缺陷修复 —— 本探针此前结论无效]
    原实现只改 `M.PER_SYMBOL_CFG` 然后调 `M.detect_for(sym, name, data, {})`。但
    detect_for 的闸门（mpr_enable / mpr_periods / atr_min_pct）**只认函数入参**，
    内部不回退读 PER_SYMBOL_CFG —— 于是 6 个消融臂拿到的入参完全相同（全 None，
    即闸门全关），信号量必然恒等。08-05 由此得出的
        「161129 全闸门放开信号量不变(4=复算量) → 闸门无卡死，实盘零推送=生产侧抑制」
    是**同义反复/自证**，不构成证据；该错误结论已错误指导 R1 方向 6 天。
    另：vol_confirm 臂对应的过滤逻辑当时根本不在 detect_for 内，臂本身是空操作。

    现修复为：① 闸门作为真实入参传入 detect_for；② vol_confirm 臂改为调
    core/v10_confirm 做真实后置过滤（与实盘同一实现）；③ baseline 臂取生产配置真值。
    """
    cfg = M.PER_SYMBOL_CFG.get(sym) or {}
    mpr_e, mpr_p = cfg.get('mpr_enable'), cfg.get('mpr_periods')
    atr = cfg.get('atr_min_pct')
    vc = bool(cfg.get('vol_confirm'))

    if 'atr_min_pct' in overrides:
        atr = overrides['atr_min_pct']
    if 'mpr_enable' in overrides:
        mpr_e = overrides['mpr_enable']
    if 'vol_confirm' in overrides:
        vc = bool(overrides['vol_confirm'])

    M.STATE[sym] = {'PC': pc}
    sigs = M.detect_for(sym, name, data, {},
                        mpr_enable=mpr_e, mpr_periods=mpr_p, atr_min_pct=atr)
    if vc:
        try:
            import v10_confirm as _vc
            sigs, _sup = _vc.filter_signals(sym, sigs, data.get('df'), log=_quiet)
        except Exception as e:
            print(f'  [warning] {sym} vol_confirm 臂 v10_confirm 异常(按不过滤计): {e}')
    return [s[0] for s in sigs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--syms', required=True)
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    wl = json.load(open(WATCHLIST, encoding='utf-8'))

    report = {'date': a.date, 'symbols': {}}
    for sym in a.syms.split(','):
        name = wl.get(sym, sym)
        full = load_1m_csv(os.path.join(F_DATA, f'{sym}_1m.csv'))
        day_df, pc = None, None
        for d, g in group_by_day(full):
            if str(d) == a.date:
                day_df = g.reset_index(drop=True)
                pc = day_prev_close(full, d)
        if day_df is None or pc is None:
            report['symbols'][sym] = {'error': f'{a.date} 无1m数据'}
            print(f'[{sym}] {a.date} 无1m数据，跳过')
            continue
        data = R.build_data(day_df, pc)
        cells = {}
        for label, ov in ABLATIONS:
            ops = replay_with(sym, name, data, pc, ov)
            cells[label] = {'n': len(ops), 'B': ops.count('B'), 'S': ops.count('S'),
                            'X': sum(1 for o in ops if o not in ('B', 'S'))}
        # 定位主卡死闸门：单闸门放开后信号增量最大者
        base_n = cells['baseline(生产配置)']['n']
        gains = {lb: c['n'] - base_n for lb, c in cells.items() if lb != 'baseline(生产配置)'}
        main_gate = max(gains, key=gains.get) if gains else None
        report['symbols'][sym] = {'name': name, 'baseline_signals': base_n,
                                  'cells': cells, 'main_block_gate': main_gate,
                                  'max_gain': gains.get(main_gate, 0)}
        print(f'[{sym}] baseline={base_n} ' +
              ' '.join(f'{lb}:{c["n"]}' for lb, c in cells.items()) +
              f' → 主卡死={main_gate}(+{gains.get(main_gate, 0)})')

    out = a.out or os.path.join(ROOT, 'output', f'gate_ablation_{a.date}.json')
    json.dump(report, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'[ok] {out}')


if __name__ == '__main__':
    main()
