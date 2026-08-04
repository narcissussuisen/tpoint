#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""two_stage_trail_review.py — trail 参数两段式复核（2026-08-04 晚，用户授权）

纪律（计划铁律）：先在 tune_pool_40 调参池寻优 → 再在 watchlist 5 只验证，全集口径不降才通过。
- Stage1（调参）：data/tune_pool_40.json 40 只 × F盘全历史，trail {0.3,0.4,0.5}×{0.5,0.6,0.8} 网格，
  信号=生产同源复算（各池标的无 per-symbol 配置 → 默认口径），评价=全池聚合净胜率+标的中位。
- Stage2（验证）：胜出组合在 watchlist 5 只复核，对比当前 0.4/0.6。
- 通过标准：①Stage1 全池净胜率 ≥ 基线（不降）②Stage2 watchlist 池级 ≥ 基线+1pp ③无单只劣化>2pp
产出：output/two_stage_trail_<date>.json + 控制台结论。
CLI：python scripts/two_stage_trail_review.py
"""
import os, sys, json, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import factor_optimizer as FO
from exit_manager import aggregate_metrics

TRAIL_ACT = [0.3, 0.4, 0.5]
TRAIL_PCT = [0.5, 0.6, 0.8]
CUR = (0.4, 0.6)
C_PROD_MEDIAN = 56.2   # watchlist 池级中位基线（计划口径）


def eval_sym(sym, name, days):
    """单标的 9 格 trail 结果 {cell: metrics}（信号用默认口径算一次）。"""
    for d, data, g in days:
        data['sym'] = sym
    sig_days = FO.day_signals(sym, name, days, FO.CUR_ATR)
    res = {}
    for ta in TRAIL_ACT:
        for tp in TRAIL_PCT:
            res[f'{ta}/{tp}'] = FO.metrics_of(FO.eval_config(sig_days, ta, tp))
    return res


def pool_stats(per_cell_trips):
    """全池聚合：整体净胜率 + 标的胜率中位 + 总收益合计。"""
    agg = {}
    for cell, sym_trips in per_cell_trips.items():
        all_t = [t for ts in sym_trips.values() for t in ts]
        wrs = []
        for s, ts in sym_trips.items():
            if ts:
                wrs.append(sum(1 for t in ts if t['ret_pct'] > 0) / len(ts) * 100)
        m = aggregate_metrics(all_t)
        wrs.sort()
        agg[cell] = {'n': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
                     'total_ret': m.get('total_ret_pct', 0),
                     'sym_median_wr': round(wrs[len(wrs) // 2], 1) if wrs else None}
    return agg


def main():
    date = datetime.date.today().strftime('%Y-%m-%d')
    pool = json.load(open(os.path.join(ROOT, 'data', 'tune_pool_40.json'), encoding='utf-8'))['pool']
    wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))

    # ---------- Stage 1：40 只调参池 ----------
    s1_trips = {}   # cell -> {sym: trips}
    n_loaded = 0
    for p in pool:
        sym = p['symbol']
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            print(f'[stage1] {sym} 数据加载失败: {e}')
            continue
        if len(days) < 30:
            continue
        n_loaded += 1
        for d, data, g in days:
            data['sym'] = sym
        sig_days = FO.day_signals(sym, sym, days, FO.CUR_ATR)
        for ta in TRAIL_ACT:
            for tp in TRAIL_PCT:
                cell = f'{ta}/{tp}'
                s1_trips.setdefault(cell, {})[sym] = FO.eval_config(sig_days, ta, tp)
        print(f'[stage1] {sym} done ({len(days)}d)', flush=True)
    s1 = pool_stats(s1_trips)
    base_cell = f'{CUR[0]}/{CUR[1]}'
    base_wr = s1[base_cell]['win_rate']
    # 胜出 = 全池净胜率最高 且 ≥ 基线（不降）且样本充足
    cands = [(c, m) for c, m in s1.items() if c != base_cell and m['n'] >= 400 and m['win_rate'] >= base_wr]
    cands.sort(key=lambda x: -x[1]['win_rate'])
    best_cell, best_m = (cands[0] if cands else (None, None))

    # ---------- Stage 2：watchlist 验证 ----------
    s2 = {}
    for sym, name in wl.items():
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            s2[sym] = {'error': str(e)}
            continue
        for d, data, g in days:
            data['sym'] = sym
        sig_days = FO.day_signals(sym, name, days, FO.CUR_ATR)
        cur_m = FO.metrics_of(FO.eval_config(sig_days, *CUR))
        row = {'current': cur_m}
        if best_cell:
            ta, tp = map(float, best_cell.split('/'))
            row['candidate'] = FO.metrics_of(FO.eval_config(sig_days, ta, tp))
        s2[sym] = row
        print(f'[stage2] {sym} cur={cur_m["win_rate"]}% '
              f'cand={row.get("candidate", {}).get("win_rate")}%', flush=True)

    # ---------- 判定 ----------
    verdict = {'stage1_pool_n': n_loaded, 'stage1_base_wr': base_wr,
               'best_cell': best_cell, 'best_stage1': best_m}
    if best_cell is None:
        verdict['result'] = 'FAIL：调参池无任何组合不劣于基线 → 维持 0.4/0.6'
    else:
        cur_pool_wr = [v['current']['win_rate'] for v in s2.values() if 'current' in v and v['current']['n'] > 0]
        cand_pool_wr = [v['candidate']['win_rate'] for v in s2.values() if 'candidate' in v and v['candidate']['n'] > 0]
        cur_pool = sum(cur_pool_wr) / len(cur_pool_wr)
        cand_pool = sum(cand_pool_wr) / len(cand_pool_wr)
        degrades = [s for s, v in s2.items()
                    if 'candidate' in v and v['candidate']['win_rate'] < v['current']['win_rate'] - 2]
        ok = (cand_pool >= cur_pool + 1.0) and not degrades
        verdict.update({'stage2_cur_pool_wr': round(cur_pool, 1), 'stage2_cand_pool_wr': round(cand_pool, 1),
                        'degrade_syms': degrades,
                        'result': ('PASS：可灰度（建议先 1 只 3 日）' if ok
                                   else f'FAIL：watchlist 验证未过（池级 {round(cand_pool - cur_pool, 1):+}pp <+1pp 或单只劣化>2pp）')})
    out = {'date': date, 'stage1': s1, 'stage2': s2, 'verdict': verdict}
    path = os.path.join(ROOT, 'output', f'two_stage_trail_{date}.json')
    json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('\n===== 判定 =====')
    print(json.dumps(verdict, ensure_ascii=False, indent=1))
    print(f'[ok] {path}')


if __name__ == '__main__':
    main()
