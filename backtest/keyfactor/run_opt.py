#!/usr/bin/env python3
"""
P2-P5 统一优化测量器. 复用 kf_utils (load_1m/fwd_rets/build_attr_rows) 与 miji_engine.
通过环境变量选择 variant, 输出到独立 JSON (不覆盖 canonical 产物 keyfactor_summary.json).

Env 控制:
  OPT_MODE = lean | vwap | holdout
  MIN_RES       (默认 2)
  VWAP_DEV      (同时设 BUY/SELL; 默认引擎值)
  VOL_EXPAND    (默认引擎值)
  VOL_SHRINK    (默认引擎值)
  BTREND=1      开启 b_trend_filter (买臂下跌趋势不接飞刀)
  VOLSWAP=1     运行时反转 vol_div 买卖符号 (不改引擎文件)
  HOLDOUT_FRAC=0.66  前 66% 标的作训练, 后 34% 作样本外测试
  OUT=path       输出 json

lean:   baseline + 消融(gravity/vol_div/macd_div) + RESONANCE 扫描(1/2/3)
vwap:   27 组 VWAP_DEV×VOL_EXPAND×VOL_SHRINK 网格扫描
holdout: 按标的切分, 仅在测试标的上跑 baseline, 报 skill24 (样本外)
"""
import os, json, glob, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

import kf_utils as K
import miji_engine as ME

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = KEYFACTOR_DATA_DIR
INDIR = os.path.join(DATA, '1m')
HORIZONS = [6, 12, 24]


def skill_stats(rows):
    out = {}
    for hh in HORIZONS:
        vals = []
        for r in rows:
            f = r[f'fwd{hh}']
            if f is None:
                continue
            vals.append(f if r['type'] == 'B' else -f)
        out[hh] = (float(np.mean(vals)), len(vals)) if vals else (None, 0)
    return out


def detect_config(cache, enable, min_res, b_trend, vol_swap, novol=False):
    saved_fn = None
    if vol_swap:
        orig = ME.volume_divergence_signal
        def wrapped(h, lo, c, v, i, w=ME.DIVERGENCE_W, vol_w=ME.VOL_COMPARE_W):
            f, d = orig(h, lo, c, v, i, w, vol_w)
            return (-f, d) if f != 0 else (0, d)
        ME.volume_divergence_signal = wrapped
        saved_fn = orig
    rows = []
    nsig = 0
    en = (enable[0], (False if novol else enable[1]), enable[2])
    for item in cache:
        sigs = ME.detect_miji_signals(item['data'], item['pc'], start_idx=2,
                                       min_resonance=min_res, b_trend_filter=b_trend,
                                       allow_reverse=True, enable=en)
        sigs = K.fwd_rets(item['df'], sigs)
        rows.extend(K.build_attr_rows(sigs, item['df']))
        nsig += len(sigs)
    if saved_fn is not None:
        ME.volume_divergence_signal = saved_fn
    return rows, nsig


def patch_constants():
    saved = {}
    if os.environ.get('VWAP_DEV'):
        v = float(os.environ['VWAP_DEV'])
        saved['VWAP_DEV_BUY'] = ME.VWAP_DEV_BUY
        saved['VWAP_DEV_SELL'] = ME.VWAP_DEV_SELL
        ME.VWAP_DEV_BUY = v
        ME.VWAP_DEV_SELL = v
    if os.environ.get('VOL_EXPAND'):
        saved['VOL_EXPAND_RATIO'] = ME.VOL_EXPAND_RATIO
        ME.VOL_EXPAND_RATIO = float(os.environ['VOL_EXPAND'])
    if os.environ.get('VOL_SHRINK'):
        saved['VOL_SHRINK_RATIO'] = ME.VOL_SHRINK_RATIO
        ME.VOL_SHRINK_RATIO = float(os.environ['VOL_SHRINK'])
    return saved


def restore_constants(saved):
    for k, v in saved.items():
        setattr(ME, k, v)


def build_cache(indir):
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    cache = []
    for fp in files:
        df = K.load_1m(fp)
        o = df['open'].values.astype(float)
        h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float)
        c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        has_vol = float(np.sum(v)) > 0
        pc = float(c[0]) if len(c) > 0 else 0.0
        data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
        cache.append({'df': df, 'data': data, 'pc': pc})
    return cache, len(files)


def run_lean(cache, min_res, b_trend, vol_swap, out, novol=False):
    saved = patch_constants()
    base_rows, n_base = detect_config(cache, (True, True, True), min_res, b_trend, vol_swap, novol)
    base_skill = skill_stats(base_rows)
    abl = {}
    for name, en in [('all', (True, True, True)),
                     ('no_gravity', (False, True, True)),
                     ('no_vol_div', (True, False, True)),
                     ('no_macd_div', (True, True, False))]:
        r, n = detect_config(cache, en, min_res, b_trend, vol_swap, novol)
        sk = skill_stats(r)
        abl[name] = {'n': n, 'skill': {h: sk[h][0] for h in HORIZONS}}
    sweep = []
    for mr in (1, 2, 3):
        r, n = detect_config(cache, (True, True, True), mr, b_trend, vol_swap)
        sk = skill_stats(r)
        sweep.append({'RESONANCE': mr, 'n': n,
                      'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    imp = {}
    for fac, key in [('gravity', 'no_gravity'), ('vol_div', 'no_vol_div'), ('macd_div', 'no_macd_div')]:
        drop = (abl['all']['skill'][24] or 0) - (abl[key]['skill'][24] or 0)
        imp[fac] = round(drop, 5)
    ranking = sorted(imp.items(), key=lambda x: -abs(x[1]))
    outd = {
        'n_symbols': len(cache), 'min_resonance': min_res,
        'b_trend_filter': b_trend, 'vol_swap': vol_swap,
        'baseline_skill': {h: base_skill[h][0] for h in HORIZONS},
        'ablation': abl, 'factor_importance_drop_skill24': imp,
        'factor_ranking': [k for k, _ in ranking], 'resonance_sweep': sweep,
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(outd, f, ensure_ascii=False, indent=2)
    print(f"[lean] baseline skill24={base_skill[24][0]*100:+.4f}% n={n_base} "
          f"vol_div_drop={imp['vol_div']:+.4f} macd_div_drop={imp['macd_div']:+.4f} -> {out}")
    restore_constants(saved)


def run_vwap(cache, min_res, out):
    sweep = []
    for dev in (0.6, 0.8, 1.0):
        for ve in (1.1, 1.2, 1.3):
            for vs in (0.7, 0.8, 0.9):
                # 手动 patch
                sb = {'VWAP_DEV_BUY': ME.VWAP_DEV_BUY, 'VWAP_DEV_SELL': ME.VWAP_DEV_SELL,
                       'VOL_EXPAND_RATIO': ME.VOL_EXPAND_RATIO, 'VOL_SHRINK_RATIO': ME.VOL_SHRINK_RATIO}
                ME.VWAP_DEV_BUY = dev; ME.VWAP_DEV_SELL = dev
                ME.VOL_EXPAND_RATIO = ve; ME.VOL_SHRINK_RATIO = vs
                rows, n = detect_config(cache, (True, True, True), min_res, False, False)
                sk = skill_stats(rows)
                sweep.append({'VWAP_DEV': dev, 'VOL_EXPAND': ve, 'VOL_SHRINK': vs, 'n': n,
                              'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
                for k, v in sb.items():
                    setattr(ME, k, v)
    best = max(sweep, key=lambda s: s['skill24'])
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'n_symbols': len(cache), 'min_resonance': min_res,
                   'sweep': sweep, 'best': best}, f, ensure_ascii=False, indent=2)
    print(f"[vwap] best dev={best['VWAP_DEV']} ve={best['VOL_EXPAND']} vs={best['VOL_SHRINK']} "
          f"skill24={best['skill24']*100:+.4f}% n={best['n']} -> {out}")


def run_holdout(cache, min_res, frac, out, novol=False):
    saved = patch_constants()
    k = int(len(cache) * frac)
    test_cache = cache[k:]
    base_rows, n_base = detect_config(test_cache, (True, True, True), min_res, False, False, novol)
    sk = skill_stats(base_rows)
    outd = {
        'n_symbols_total': len(cache), 'n_symbols_test': len(test_cache),
        'holdout_frac': frac, 'min_resonance': min_res,
        'test_skill': {h: sk[h][0] for h in HORIZONS}, 'test_n_signals': n_base,
    }
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(outd, f, ensure_ascii=False, indent=2)
    print(f"[holdout] test skill24={sk[24][0]*100:+.4f}% n_test={n_base} (of {len(cache)}) -> {out}")
    restore_constants(saved)


def main():
    mode = os.environ.get('OPT_MODE', 'lean')
    min_res = int(os.environ.get('MIN_RES', '2'))
    out = os.environ.get('OUT', os.path.join(DATA, 'keyfactor_opt.json'))
    cache, nf = build_cache(INDIR)
    print(f"=== 加载 {nf} 只 1m ===")
    if mode == 'lean':
        run_lean(cache, min_res, os.environ.get('BTREND') == '1',
                 os.environ.get('VOLSWAP') == '1', out,
                 novol=os.environ.get('NOVOL') == '1')
    elif mode == 'vwap':
        run_vwap(cache, min_res, out)
    elif mode == 'holdout':
        frac = float(os.environ.get('HOLDOUT_FRAC', '0.66'))
        run_holdout(cache, min_res, frac, out, novol=os.environ.get('NOVOL') == '1')
    else:
        print(f"未知 OPT_MODE={mode}")


if __name__ == '__main__':
    main()
