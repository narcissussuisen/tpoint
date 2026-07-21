#!/usr/bin/env python3
"""测试 'macd-required' 门控的 OOS 表现 (T1.4)。

门控: B 需 macd_div==1, S 需 macd_div==-1 (纳入 macd-only + both, 排除 gravity-only)。
依据: OOS 归因显示 gravity-only OOS=-2.09%(负), macd-only=+4.82%, both=+5.08%。
同口径: 子集400, 逐日修正, 跨时段 OOS 66/34, skill24。
对比冻结 RESONANCE=2 (both-only): 逐股均值 test=-0.35%, 信号加权=+1.77%, frac_pos=55.5%。
目标: 逐股均值 OOS > 0。
"""
import sys, os, glob, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _paths import KEYFACTOR_DATA_DIR, KEYFACTOR_1M_DIR

import kf_utils as K
import miji_engine as ME
from run_study import _segment_days, HORIZONS

DATA = KEYFACTOR_DATA_DIR
SUBSET = os.path.join(DATA, '_subset400')
OUT_JSON = os.path.join(DATA, 'macd_required_oos.json')


def detect_macdreq_daily(df):
    """逐日检测 + require_macd=True (macd 必须投票, 排除 gravity-only)。idx 重映射回整段。"""
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
        data = ME.compute_miji_indicators(o_d, h_d, lo_d, c_d, v_d, pc, has_vol=has_vol)
        # require_macd=True: 门控=macd 投票 (min_resonance 被绕过)
        sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=2,
                                      b_trend_filter=False, allow_reverse=True,
                                      require_macd=True)
        for s in sigs:
            s2 = dict(s)
            s2['idx'] = gs + s['idx']
            all_sigs.append(s2)
        if ge > gs:
            prev_close = float(c[ge - 1])
    return all_sigs


def sk24(sigs):
    v = [(s['fwd24'] if s['type'] == 'B' else -s['fwd24'])
         for s in sigs if s.get('fwd24') is not None]
    return (float(np.mean(v)), len(v)) if v else (None, 0)


def main():
    files = sorted(glob.glob(os.path.join(SUBSET, '*_1m.csv')))
    print(f"=== macd-required 门控 OOS 测试 (子集{len(files)}只, 逐日修正) ===", flush=True)
    per = []
    skipped = 0
    for k, fp in enumerate(files):
        try:
            df = K.load_1m(fp)
        except Exception:
            skipped += 1
            continue
        N = len(df)
        if N < 200:
            continue
        try:
            sigs = detect_macdreq_daily(df)
        except Exception as e:
            skipped += 1
            if skipped <= 10:
                print(f"  [skip] {os.path.basename(fp)}: {type(e).__name__}: {e}", flush=True)
            continue
        sigs = K.fwd_rets(df, sigs)
        split = int(0.66 * N)
        if split < 50 or (N - split) < 50:
            continue
        tr = [s for s in sigs if s['idx'] < split and (s['idx'] + 24) <= split]
        te = [s for s in sigs if s['idx'] >= split]
        tr24 = sk24(tr)
        te24 = sk24(te)
        per.append({'sym': os.path.basename(fp).replace('_1m.csv', ''),
                    'n_train': tr24[1],
                    'skill24_train': round(tr24[0], 4) if tr24[0] is not None else None,
                    'n_test': te24[1],
                    'skill24_test': round(te24[0], 4) if te24[0] is not None else None})
        if (k + 1) % 50 == 0:
            print(f"  ...{k+1}/{len(files)} 只", flush=True)

    te_skills = [p['skill24_test'] for p in per if p['skill24_test'] is not None]
    tr_skills = [p['skill24_train'] for p in per if p['skill24_train'] is not None]
    npos = sum(1 for x in te_skills if x > 0)
    w = [p['n_test'] for p in per if p['skill24_test'] is not None]
    xs = [p['skill24_test'] for p in per if p['skill24_test'] is not None]
    sw = sum(w)
    pooled = sum(x * wv for x, wv in zip(xs, w)) / sw if sw > 0 else None

    print(f"\n  股数={len(per)}  跳过={skipped}")
    print(f"  train skill24(逐股均值) = {float(np.mean(tr_skills)):+.4f}%")
    print(f"  test  skill24(逐股均值) = {float(np.mean(te_skills)):+.4f}%  "
          f"median={float(np.median(te_skills)):+.4f}%")
    print(f"  test  信号加权 = {float(pooled):+.4f}%  "
          f"frac_pos = {npos/len(te_skills)*100:.1f}%  (n_pos={npos}/{len(te_skills)})")
    print(f"  test  分布: <0: {sum(1 for x in te_skills if x<0)}  "
          f"<-2%: {sum(1 for x in te_skills if x<-0.02)}  "
          f">+2%: {sum(1 for x in te_skills if x>0.02)}")
    print(f"\n  >>> 对比冻结 RESONANCE=2 (both-only): 逐股均值 test=-0.3500%, "
          f"信号加权=+1.7698%, frac_pos=55.5% <<<")

    res = {'config': 'macd-required (B需macd==1 / S需macd==-1, 排除gravity-only)',
           'n_stocks': len(per),
           'mean_skill24_train': round(float(np.mean(tr_skills)), 4) if tr_skills else None,
           'mean_skill24_test': round(float(np.mean(te_skills)), 4) if te_skills else None,
           'median_skill24_test': round(float(np.median(te_skills)), 4) if te_skills else None,
           'signal_weighted_test': round(float(pooled), 4) if pooled is not None else None,
           'frac_test_positive': round(npos / len(te_skills), 3) if te_skills else None,
           'n_neg': int(sum(1 for x in te_skills if x < 0)),
           'n_neg2': int(sum(1 for x in te_skills if x < -0.02)),
           'n_pos2': int(sum(1 for x in te_skills if x > 0.02))}
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n  落地: {OUT_JSON}")


if __name__ == '__main__':
    main()
