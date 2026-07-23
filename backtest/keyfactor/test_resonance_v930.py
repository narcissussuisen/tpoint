#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v9.3.0 三因子共振模式快速评估

对比三种 MACD_GATE_MODE 在同一批 1m CSV 上的信号数量与质量：
  - strict  (生产历史默认)
  - floor   (生产当前默认)
  - resonance (v9.3.0 新增：>=RESONANCE_THRESHOLD 个同向因子)

resonance 模式下自动启用 vol_div（量价背离），其余模式保持 vol_div 关闭以
匹配当前生产配置。早盘 i<LOCAL_W 均降级 gravity-only。

输出:
  output/resonance_v930_report.json
  output/resonance_v930_report.csv
"""
import sys
import os
import glob
import json
import argparse
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import KEYFACTOR_1M_DIR
import kf_utils as K
import miji_engine as ME


OUT_JSON = os.path.join('output', 'resonance_v930_report.json')
OUT_CSV = os.path.join('output', 'resonance_v930_report.csv')
HORIZONS = [6, 12, 24]


class Patch:
    """临时 monkeypatch miji_engine 模块常量, 退出即还原。"""
    def __init__(self, **kv):
        self.kv = kv
        self.saved = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = getattr(ME, k, None)
            setattr(ME, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(ME, k, v)


def _segment_days(df):
    """按 trade_date 分日（同 run_study）。"""
    if 'trade_date' not in df.columns:
        return [(0, len(df))]
    dates = df['trade_date'].tolist()
    groups = []
    prev = None
    start = 0
    for i, d in enumerate(dates):
        if prev is None:
            prev = d
        if d != prev:
            groups.append((start, i))
            start = i
            prev = d
    groups.append((start, len(df)))
    return groups


def detect_daily(df, macd_gate_mode='strict', vol_div_enabled=False, min_resonance=2):
    """逐日检测，返回信号列表（idx 映射到整段）。"""
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    groups = _segment_days(df)
    prev_close = None
    all_sigs = []
    for gs, ge in groups:
        pc = float(prev_close) if (prev_close is not None and prev_close > 0) else float(c[gs])
        o_d = o[gs:ge]
        h_d = h[gs:ge]
        lo_d = lo[gs:ge]
        c_d = c[gs:ge]
        v_d = v[gs:ge] if v is not None else None
        has_vol = v_d is not None and float(np.sum(v_d)) > 0
        with Patch(MACD_GATE_MODE=macd_gate_mode, VOL_DIV_ENABLED=vol_div_enabled):
            data = ME.compute_miji_indicators(o_d, h_d, lo_d, c_d, v_d, pc, has_vol=has_vol)
            sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_resonance,
                                          b_trend_filter=False, allow_reverse=True,
                                          macd_gate_mode=macd_gate_mode)
        for s in sigs:
            s2 = dict(s)
            s2['idx'] = gs + s['idx']
            all_sigs.append(s2)
        prev_close = float(c[ge - 1])
    return all_sigs


def skill_stats(rows):
    out = {}
    for hh in HORIZONS:
        vals = [r[f'fwd{hh}'] if r['type'] == 'B' else -r[f'fwd{hh}']
                for r in rows if r.get(f'fwd{hh}') is not None]
        out[hh] = (float(np.mean(vals)), len(vals)) if vals else (0.0, 0)
    return out


def score_distribution(rows):
    """统计共振分数分布。"""
    b_scores = [r['resonance_score'] for r in rows if r['type'] == 'B']
    s_scores = [r['resonance_score'] for r in rows if r['type'] == 'S']
    dist = {'B': defaultdict(int), 'S': defaultdict(int)}
    for s in b_scores:
        dist['B'][s] += 1
    for s in s_scores:
        dist['S'][s] += 1
    return {
        'B': dict(sorted(dist['B'].items())),
        'S': dict(sorted(dist['S'].items())),
    }


def factor_breakdown(rows):
    """统计信号由哪些因子组合触发。"""
    combos = defaultdict(lambda: {'B': 0, 'S': 0})
    for r in rows:
        parts = []
        if r['g'] == 1:
            parts.append('g')
        if r['vd'] == 1:
            parts.append('vd')
        if r['md'] == 1:
            parts.append('md')
        key = '+'.join(parts) if parts else 'none'
        combos[key][r['type']] += 1
    return dict(sorted(combos.items()))


def evaluate_symbol(df, sym):
    """对单只票评估三种模式。"""
    results = {}
    for mode, vol_on in [('strict', False), ('floor', False), ('resonance', True)]:
        sigs = detect_daily(df, macd_gate_mode=mode, vol_div_enabled=vol_on,
                            min_resonance=ME.RESONANCE_THRESHOLD)
        sigs = K.fwd_rets(df, sigs)
        rows = K.build_attr_rows(sigs, df)
        sk = skill_stats(rows)
        results[mode] = {
            'n_signals': len(sigs),
            'n_B': sum(1 for s in sigs if s['type'] == 'B'),
            'n_S': sum(1 for s in sigs if s['type'] == 'S'),
            'skill': {f'skill{h}': round(sk[h][0], 4) for h in HORIZONS},
            'n_evaluated': {f'n{h}': sk[h][1] for h in HORIZONS},
            'score_dist': score_distribution(rows),
            'factor_breakdown': factor_breakdown(rows),
            'rows': rows,
        }
    return results


def aggregate(per_stock):
    """跨票聚合。"""
    agg = {}
    for mode in ('strict', 'floor', 'resonance'):
        nsig = sum(p[mode]['n_signals'] for p in per_stock.values())
        nB = sum(p[mode]['n_B'] for p in per_stock.values())
        nS = sum(p[mode]['n_S'] for p in per_stock.values())
        # skill 按信号数加权
        skill = {}
        for h in HORIZONS:
            vals = []
            weights = []
            for p in per_stock.values():
                n = p[mode]['n_evaluated'][f'n{h}']
                if n > 0:
                    vals.append(p[mode]['skill'][f'skill{h}'])
                    weights.append(n)
            skill[f'skill{h}'] = round(float(np.average(vals, weights=weights)), 4) if vals else 0.0
            skill[f'n{h}'] = sum(weights)
        # 合并分数分布
        dist = {'B': defaultdict(int), 'S': defaultdict(int)}
        for p in per_stock.values():
            for t in ('B', 'S'):
                for k, v in p[mode]['score_dist'][t].items():
                    dist[t][k] += v
        # 合并因子组合
        combos = defaultdict(lambda: {'B': 0, 'S': 0})
        for p in per_stock.values():
            for k, v in p[mode]['factor_breakdown'].items():
                # v 是 {'B': n, 'S': m}
                for t in ('B', 'S'):
                    combos[k][t] += v.get(t, 0)
        agg[mode] = {
            'n_signals': nsig,
            'n_B': nB,
            'n_S': nS,
            'skill': skill,
            'score_dist': {t: dict(sorted(dist[t].items())) for t in ('B', 'S')},
            'factor_breakdown': dict(sorted(combos.items())),
        }
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--indir', default=KEYFACTOR_1M_DIR)
    ap.add_argument('--max-symbols', type=int, default=50,
                    help='为控制耗时，默认只跑前 N 只票；设 0 跑全部')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.indir, '*_1m.csv')))
    if not files:
        print(f'⚠️ {args.indir} 无 1m 文件')
        return
    if args.max_symbols > 0:
        files = files[:args.max_symbols]
    print(f'=== v9.3.0 共振评估: {len(files)} 只票 ===')

    per_stock = {}
    for fp in files:
        sym = os.path.basename(fp).replace('_1m.csv', '')
        try:
            df = K.load_1m(fp)
        except Exception as e:
            print(f'  skip {sym}: {e}')
            continue
        if len(df) < 100:
            continue
        print(f'  处理 {sym} ...', flush=True)
        per_stock[sym] = evaluate_symbol(df, sym)

    agg = aggregate(per_stock)

    # 打印摘要
    print('\n=== 跨票聚合摘要 ===')
    for mode in ('strict', 'floor', 'resonance'):
        a = agg[mode]
        print(f"\n[{mode}]")
        print(f"  总信号数: {a['n_signals']} (B={a['n_B']}, S={a['n_S']})")
        print(f"  skill: 6={a['skill']['skill6']:+.4f}% 12={a['skill']['skill12']:+.4f}% 24={a['skill']['skill24']:+.4f}%")
        print(f"  共振分数分布: B={a['score_dist']['B']} S={a['score_dist']['S']}")
        print(f"  因子组合(top): {dict(list(a['factor_breakdown'].items())[:5])}")

    # 保存聚合报告
    report = {
        'config': {
            'RESONANCE_THRESHOLD': ME.RESONANCE_THRESHOLD,
            'VWAP_DEV_BUY': ME.VWAP_DEV_BUY,
            'VWAP_DEV_SELL': ME.VWAP_DEV_SELL,
            'VOL_EXPAND_RATIO': ME.VOL_EXPAND_RATIO,
            'VOL_SHRINK_RATIO': ME.VOL_SHRINK_RATIO,
            'SIGNAL_GAP': ME.SIGNAL_GAP,
            'LOCAL_W': ME.LOCAL_W,
            'n_symbols': len(per_stock),
            'data_dir': args.indir,
        },
        'aggregate': agg,
        'per_stock': {
            sym: {mode: {k: v for k, v in res[mode].items() if k != 'rows'}
                  for mode in res}
            for sym, res in per_stock.items()
        },
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 保存逐票 CSV
    rows = []
    for sym, res in per_stock.items():
        for mode in ('strict', 'floor', 'resonance'):
            d = res[mode]
            rows.append({
                'symbol': sym,
                'mode': mode,
                'n_signals': d['n_signals'],
                'n_B': d['n_B'],
                'n_S': d['n_S'],
                'skill6': d['skill']['skill6'],
                'skill12': d['skill']['skill12'],
                'skill24': d['skill']['skill24'],
            })
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding='utf-8-sig')
    print(f'\n  落地: {OUT_JSON}')
    print(f'  落地: {OUT_CSV}')


if __name__ == '__main__':
    main()
