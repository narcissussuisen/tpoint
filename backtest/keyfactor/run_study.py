#!/usr/bin/env python3
"""
Phase 3-5 编排器 (autoresearch 轻量 harness 形态A):
  输入: 一批 1m CSV (默认 keyfactor_data/1m/, 可用 --indir 指向 seed 目录做验证)
  流程:
    Phase 2 标准化: load_1m (会话感知留待, 前向收益按 bar 计)
    Phase 3 归因: 全开配置跑引擎 -> 每信号 factors + 前向收益 -> 因子边际/方向正确率
    Phase 4 消融: 关 gravity / vol_div / macd_div 各一次 -> 指标(符号调整后 skill) 下降幅度 = 因子重要性
    Phase 5 阈值扫描: VWAP_DEV(0.6/0.8/1.0) × VOL_EXPAND/SHRINK × RESONANCE(1/2/3) -> 找灵敏/鲁棒参数
  输出: keyfactor_results.csv (逐配置) + keyfactor_summary.json (排名/结论)
  飞书里程碑在调用方(download 完成后跑此脚本, 50/75/100% 由外层推)。

  skill 定义 (符号调整后前向收益): B信号 fwd>0 为好, S信号 fwd<0 为好
    skill = fwd if type=='B' else -fwd ; 越高=信号质量越好
"""
import sys, os, glob, json, argparse
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kf_utils as K
import miji_engine as ME

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'keyfactor_data')
DEF_IN = os.path.join(DATA, '1m')
OUT_CSV = os.path.join(DATA, 'keyfactor_results.csv')
OUT_JSON = os.path.join(DATA, 'keyfactor_summary.json')
HORIZONS = [6, 12, 24]

class Patch:
    """临时 monkeypatch miji_engine 模块常量, 退出即还原。"""
    def __init__(self, **kv):
        self.kv = kv; self.saved = {}
    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = getattr(ME, k, None)
            setattr(ME, k, v)
        return self
    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(ME, k, v)

def precompute(files):
    """一次算指标(阈值无关), 缓存 data/pc/df。Phase5 阈值扫描只重跑检测。"""
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
    return cache

def attr_for_config(cache, enable, min_res, **monkey):
    with Patch(**monkey):
        rows = []
        nsig = 0
        for item in cache:
            df = item['df']; data = item['data']; pc = item['pc']
            sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_res,
                                          b_trend_filter=False, allow_reverse=True, enable=enable)
            sigs = K.fwd_rets(df, sigs)
            rows.extend(K.build_attr_rows(sigs, df))
            nsig += len(sigs)
    return rows, nsig

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

def factor_marginal(rows):
    """每个因子(在其方向)的边际: 参与 vs 不参与的 24根 skill 差。"""
    res = {}
    for fac in ('g', 'vd', 'md'):
        on24, off24 = [], []
        for r in rows:
            f = r['fwd24']
            if f is None:
                continue
            sk = f if r['type'] == 'B' else -f
            if r[fac] == 1:
                on24.append(sk)
            else:
                off24.append(sk)
        res[fac] = {
            'n_on': len(on24), 'n_off': len(off24),
            'skill24_on': float(np.mean(on24)) if on24 else None,
            'skill24_off': float(np.mean(off24)) if off24 else None,
        }
    return res

def load_cache(indir):
    """glob 1m 文件 + 预计算指标 (供 run_all / 外部编排调用)。"""
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    if not files:
        return []
    print(f"=== 加载 {len(files)} 只 1m (indir={indir}) ===")
    cache = precompute(files)
    print(f"  指标预计算完成 (阈值无关, 缓存 {len(cache)} 只)")
    return cache

def phase3(cache):
    """Phase 3 基线 + 归因。返回 (base_rows, n_base, base_skill, marginal)。"""
    base_rows, n_base = attr_for_config(cache, (True, True, True), 2)
    base_skill = skill_stats(base_rows)
    marginal = factor_marginal(base_rows)
    print(f"  [Phase3] baseline 信号数={n_base}  skill(6/12/24)= "
          f"{base_skill[6][0]:+.4f}/{base_skill[12][0]:+.4f}/{base_skill[24][0]:+.4f}%")
    return base_rows, n_base, base_skill, marginal

def phase4(cache):
    """Phase 4 消融: 关 gravity/vol_div/macd_div。返回 abl dict。"""
    abl = {}
    for name, en in [('all', (True, True, True)),
                     ('no_gravity', (False, True, True)),
                     ('no_vol_div', (True, False, True)),
                     ('no_macd_div', (True, True, False))]:
        r, n = attr_for_config(cache, en, 2)
        sk = skill_stats(r)
        abl[name] = {'n': n, 'skill': {h: sk[h][0] for h in HORIZONS}}
        print(f"  [Phase4] 消融 {name:12s} n={n:5d} skill24={sk[24][0]:+.4f}%")
    return abl

def phase5(cache):
    """Phase 5 阈值扫描: VWAP_DEV×VOL_EXPAND×VOL_SHRINK + RESONANCE。返回 sweep_df。"""
    sweep = []
    for dev in (0.6, 0.8, 1.0):
        for ve in (1.1, 1.2, 1.3):
            for vs in (0.7, 0.8, 0.9):
                r, n = attr_for_config(cache, (True, True, True), 2,
                                       VWAP_DEV_BUY=dev, VWAP_DEV_SELL=dev,
                                       VOL_EXPAND_RATIO=ve, VOL_SHRINK_RATIO=vs)
                sk = skill_stats(r)
                sweep.append({'VWAP_DEV': dev, 'VOL_EXPAND': ve, 'VOL_SHRINK': vs,
                             'n': n,
                             'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    for mr in (1, 2, 3):
        r, n = attr_for_config(cache, (True, True, True), mr)
        sk = skill_stats(r)
        sweep.append({'RESONANCE': mr, 'n': n,
                      'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    print(f"  [Phase5] 阈值扫描 {len(sweep)} 组完成")
    return pd.DataFrame(sweep)

def write_outputs(cache, base_rows, n_base, base_skill, marginal, abl, sweep_df, seedtest=False):
    sweep_df.to_csv(OUT_CSV, index=False, encoding='utf-8-sig')
    imp = {}
    for fac, key in [('gravity', 'no_gravity'), ('vol_div', 'no_vol_div'), ('macd_div', 'no_macd_div')]:
        drop = (abl['all']['skill'][24] or 0) - (abl[key]['skill'][24] or 0)
        imp[fac] = round(drop, 5)
    ranking = sorted(imp.items(), key=lambda x: -abs(x[1]))
    summary = {
        'n_symbols': len(cache),
        'n_signals_baseline': n_base,
        'baseline_skill': {h: base_skill[h][0] for h in HORIZONS},
        'ablation': abl,
        'factor_importance_drop_skill24': imp,
        'factor_ranking': [k for k, _ in ranking],
        'marginal_24': marginal,
        'threshold_sweep_best': {
            'vwap_best': sweep_df[sweep_df['VWAP_DEV'].notna()].loc[
                sweep_df[sweep_df['VWAP_DEV'].notna()]['skill24'].idxmax()].to_dict()
                if sweep_df['VWAP_DEV'].notna().any() else None,
            'resonance_best': sweep_df[sweep_df['RESONANCE'].notna()].loc[
                sweep_df[sweep_df['RESONANCE'].notna()]['skill24'].idxmax()].to_dict()
                if sweep_df['RESONANCE'].notna().any() else None,
        },
        'seedtest': seedtest,
    }
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n=== 因子重要性 (关掉后 skill24 变化, 单位%) ===")
    for k, v in ranking:
        print(f"  {k:12s} drop={v:+.5f}")
    print(f"\n  落地: {OUT_CSV}")
    print(f"  落地: {OUT_JSON}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--indir', default=DEF_IN)
    ap.add_argument('--seedtest', action='store_true', help='指向 backtest_data 的 7 seed 做验证')
    args = ap.parse_args()
    indir = os.path.join(HERE, '..', '..', 'backtest', 'backtest_data') if args.seedtest else args.indir
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    if not files:
        print(f"⚠️ {indir} 无 1m 文件")
        return
    cache = load_cache(indir)
    base_rows, n_base, base_skill, marginal = phase3(cache)
    abl = phase4(cache)
    sweep_df = phase5(cache)
    write_outputs(cache, base_rows, n_base, base_skill, marginal, abl, sweep_df, seedtest=args.seedtest)

if __name__ == '__main__':
    main()
