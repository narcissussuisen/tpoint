#!/usr/bin/env python3
"""测试 core/indicators.py (live v2 均值回归) 的准确度，与 miji 3因子同口径对比。

同口径：子集400、逐日修正(按 trade_date 切分，每交易日独立
compute_indicators + detect_signals，VWAP 日内重置)、前向收益 skill
(B: fwd>0 好；S: -fwd 好)、跨时段OOS(前66%训练/后34%测试)。

用 detect_signals (即 monitor 实际调用的 check_b/s_trigger 的批量版，max_b/s=12/日)。
"""
import sys, os, glob, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))   # 让 import indicators 命中 core/
import kf_utils as K
import indicators as IND
from run_study import _segment_days, HORIZONS

DATA = os.path.join(HERE, '..', 'keyfactor_data')
SUBSET = os.path.join(DATA, '_subset400')
OUT_JSON = os.path.join(DATA, 'indicators_accuracy.json')


def detect_indicators_daily(df):
    """逐日：每交易日 compute_indicators + detect_signals，idx 重映射回整段 df。"""
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    n = len(c)
    if n == 0:
        return []
    groups = _segment_days(df)
    ordered = sorted(groups, key=lambda x: x[0])
    prev_close = None
    all_sigs = []
    for (gs, ge) in ordered:
        pc = float(prev_close) if (prev_close is not None and prev_close > 0) else float(c[gs])
        o_d = o[gs:ge]; h_d = h[gs:ge]; lo_d = lo[gs:ge]; c_d = c[gs:ge]
        v_d = v[gs:ge] if v is not None else None
        has_vol = v_d is not None and float(np.sum(v_d)) > 0
        data = IND.compute_indicators(o_d, h_d, lo_d, c_d, v_d, pc, has_vol=has_vol)
        sigs = IND.detect_signals(data, pc, start_idx=2)   # max_b/max_s 默认 12/日 (live 行为)
        for s in sigs:
            s2 = dict(s)
            s2['idx'] = gs + s['idx']
            all_sigs.append(s2)
        if ge > gs:
            prev_close = float(c[ge - 1])
    return all_sigs


def skill_stats(sigs):
    out = {}
    for hh in HORIZONS:
        vals = []
        for s in sigs:
            f = s.get(f'fwd{hh}')
            if f is None:
                continue
            vals.append(f if s['type'] == 'B' else -f)
        out[hh] = (float(np.mean(vals)), len(vals)) if vals else (0.0, 0)
    return out


def main():
    files = sorted(glob.glob(os.path.join(SUBSET, '*_1m.csv')))
    print(f"=== indicators.py 准确度测试 (子集{len(files)}只, 逐日修正, 同口径) ===", flush=True)
    all_sigs = []
    per = []
    skipped = 0
    for k, fp in enumerate(files):
        try:
            df = K.load_1m(fp)
        except Exception as e:
            skipped += 1
            continue
        N = len(df)
        if N < 200:
            continue
        try:
            sigs = detect_indicators_daily(df)
        except Exception as e:
            skipped += 1
            if skipped <= 10:
                print(f"  [skip] {os.path.basename(fp)}: {type(e).__name__}: {e}", flush=True)
            continue
        sigs = K.fwd_rets(df, sigs)
        all_sigs.extend(sigs)
        split = int(0.66 * N)
        if split >= 50 and (N - split) >= 50:
            tr = [s for s in sigs if s['idx'] < split and (s['idx'] + 24) <= split]
            te = [s for s in sigs if s['idx'] >= split]
            tr24 = skill_stats(tr)[24]
            te24 = skill_stats(te)[24]
            per.append({'sym': os.path.basename(fp).replace('_1m.csv', ''),
                        'n_train': tr24[1],
                        'skill24_train': round(tr24[0], 4) if tr24[1] else None,
                        'n_test': te24[1],
                        'skill24_test': round(te24[0], 4) if te24[1] else None})
        if (k + 1) % 50 == 0:
            print(f"  ...{k+1}/{len(files)} 只, 累计信号 {len(all_sigs)}", flush=True)

    bs = skill_stats(all_sigs)
    nB = sum(1 for s in all_sigs if s['type'] == 'B')
    nS = sum(1 for s in all_sigs if s['type'] == 'S')
    print(f"\n  信号数: {len(all_sigs)} (B={nB}, S={nS})  跳过={skipped}")
    for hh in HORIZONS:
        print(f"  skill{hh:>2}: {bs[hh][0]:+.4f}%  (n={bs[hh][1]})")

    sk_tr = [p['skill24_train'] for p in per if p['skill24_train'] is not None]
    sk_te = [p['skill24_test'] for p in per if p['skill24_test'] is not None]
    npos = sum(1 for x in sk_te if x > 0)
    w = [p['n_test'] for p in per if p['skill24_test'] is not None]
    xs = [p['skill24_test'] for p in per if p['skill24_test'] is not None]
    sw = sum(w)
    pooled = sum(x * wv for x, wv in zip(xs, w)) / sw if sw > 0 else None
    print(f"\n  跨时段OOS: train skill24={float(np.mean(sk_tr)):+.4f}%  "
          f"test skill24={float(np.mean(sk_te)):+.4f}%  "
          f"(frac_pos={npos/len(sk_te):.3f}, 信号加权test={float(pooled):+.4f}%, n_stocks={len(per)})")

    print(f"\n  >>> 对比 miji 3因子(同口径): 全样本 skill24=+2.9596%, OOS test=-0.3500%(信号加权+1.7698%, 55.5%正) <<<")

    res = {'n_symbols': len(files), 'n_signals': len(all_sigs), 'nB': nB, 'nS': nS,
           'baseline_skill': {str(h): bs[h][0] for h in HORIZONS},
           'oos': {'mean_skill24_train': round(float(np.mean(sk_tr)), 4) if sk_tr else None,
                   'mean_skill24_test': round(float(np.mean(sk_te)), 4) if sk_te else None,
                   'frac_test_positive': round(npos / len(sk_te), 3) if sk_te else None,
                   'signal_weighted_test': round(float(pooled), 4) if pooled is not None else None,
                   'n_stocks': len(per)}}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n  落地: {OUT_JSON}")


if __name__ == '__main__':
    main()
