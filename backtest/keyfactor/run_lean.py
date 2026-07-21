#!/usr/bin/env python3
"""
快跑测量器 (复用 run_study 的内部函数, 跳过昂贵 27 格 VWAP 扫描):
  - baseline + 消融(gravity/vol_div/macd_div) 在固定 min_res 下
  - RESONANCE 扫描 (1/2/3)
  目的: 单次引擎改动后, ~3 分钟前台跑完得到对比数据, 不依赖会死的后台 shell。
  输出: keyfactor_lean.json (不覆盖 canonical 产物)
用法: venv/Scripts/python.exe backtest/keyfactor/run_lean.py
"""
import os, json, glob, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

import run_study as RS

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
INDIR = os.path.join(DATA, '1m')
OUT = os.path.join(DATA, 'keyfactor_lean.json')
MIN_RES = 2  # 选中配置: RESONANCE=2.0

def main():
    files = sorted(glob.glob(os.path.join(INDIR, '*_1m.csv')))
    if not files:
        print(f"⚠️ {INDIR} 无 1m 文件"); return
    cache = RS.load_cache(INDIR)

    base_rows, n_base = RS.attr_for_config(cache, (True, True, True), MIN_RES)
    base_skill = RS.skill_stats(base_rows)
    marginal = RS.factor_marginal(base_rows)

    abl = {}
    for name, en in [('all', (True, True, True)),
                     ('no_gravity', (False, True, True)),
                     ('no_vol_div', (True, False, True)),
                     ('no_macd_div', (True, True, False))]:
        r, n = RS.attr_for_config(cache, en, MIN_RES)
        sk = RS.skill_stats(r)
        abl[name] = {'n': n, 'skill': {h: sk[h][0] for h in RS.HORIZONS}}
        print(f"  [消融] {name:12s} n={n:5d} skill24={sk[24][0]:+.4f}%")

    sweep = []
    for mr in (1, 2, 3):
        r, n = RS.attr_for_config(cache, (True, True, True), mr)
        sk = RS.skill_stats(r)
        sweep.append({'RESONANCE': mr, 'n': n,
                      'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    print(f"  [共振] " + ", ".join(f"res={s['RESONANCE']}:{s['skill24']*100:+.3f}%" for s in sweep))

    imp = {}
    for fac, key in [('gravity', 'no_gravity'), ('vol_div', 'no_vol_div'), ('macd_div', 'no_macd_div')]:
        drop = (abl['all']['skill'][24] or 0) - (abl[key]['skill'][24] or 0)
        imp[fac] = round(drop, 5)
    ranking = sorted(imp.items(), key=lambda x: -abs(x[1]))

    out = {
        'n_symbols': len(cache),
        'min_resonance': MIN_RES,
        'baseline_skill': {h: base_skill[h][0] for h in RS.HORIZONS},
        'ablation': abl,
        'factor_importance_drop_skill24': imp,
        'factor_ranking': [k for k, _ in ranking],
        'marginal_24': marginal,
        'resonance_sweep': sweep,
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  baseline skill24 = {base_skill[24][0]*100:+.4f}%  (res={MIN_RES})")
    print(f"  落地: {OUT}")

if __name__ == '__main__':
    main()
