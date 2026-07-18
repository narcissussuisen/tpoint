#!/usr/bin/env python3
"""
管线自测: 在已有的 7 只 seed 1m CSV 上跑 v9.1.0 引擎 + 前向收益 + 归因聚合。
目的: 验证 kf_utils / miji_engine 逻辑正确, 且能产出因子归因表。
无需网络 (seed 已落地)。
"""
import sys, os, glob
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kf_utils as K

SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backtest_data')

def main():
    files = sorted(glob.glob(os.path.join(SEED_DIR, '*_1m.csv')))
    print(f"=== 管线自测: {len(files)} 只 seed ===")
    all_rows = []
    for fp in files:
        df = K.load_1m(fp)
        sigs = K.run_engine(df)
        sigs = K.fwd_rets(df, sigs)
        rows = K.build_attr_rows(sigs, df)
        all_rows.extend(rows)
        sm = K.summarize_signals(sigs)
        name = df['name'].iloc[0] if 'name' in df.columns else os.path.basename(fp)
        print(f"  {os.path.basename(fp):22s} {name:8s} B={sm['nB']:3d} S={sm['nS']:3d} "
              f"均分B={sm['mean_score_B']:.2f} S={sm['mean_score_S']:.2f} "
              f"attr行={len(rows)}")
    agg = K.aggregate(all_rows)
    print(f"\n=== 归因聚合 (全 {len(all_rows)} 信号) ===")
    for fac, rec in agg.items():
        print(f"\n  因子 {fac} (引力/量价/ macd 对应 g/vd/md):")
        print(f"    参与 n_on={rec['n_on']}  不参与 n_off={rec['n_off']}")
        for hh in K.HORIZONS:
            on = rec.get(f'mean_fwd{hh}_on')
            off = rec.get(f'mean_fwd{hh}_off')
            on_s = f"{on:+.3f}%" if on is not None else "  n/a"
            off_s = f"{off:+.3f}%" if off is not None else "  n/a"
            print(f"    前向{hh:2d}根: 参与={on_s:>9s}  不参与={off_s:>9s}")
    # 方向正确性: B 应正, S 应负
    B = [r for r in all_rows if r['type'] == 'B']
    S = [r for r in all_rows if r['type'] == 'S']
    import numpy as np
    def hit_rate(sub, hh):
        vals = [r[f'fwd{hh}'] for r in sub if r[f'fwd{hh}'] is not None]
        if not vals: return None
        # B: 正为好; S: 负为好
        good = sum(1 for v, t in zip(vals, [r['type'] for r in sub if r[f'fwd{hh}'] is not None]) if (v > 0) == (t == 'B'))
        return good / len(vals)
    print(f"\n=== 前向收益方向正确率 (B应正/S应负) ===")
    for hh in K.HORIZONS:
        rb = hit_rate(B, hh); rs = hit_rate(S, hh)
        print(f"    前向{hh:2d}根: B={rb*100:.1f}%  S={rs*100:.1f}%" if rb and rs else f"    前向{hh:2d}根: B={rb} S={rs}")

if __name__ == '__main__':
    main()
