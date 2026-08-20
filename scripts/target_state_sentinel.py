#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
target_state_sentinel.py —— 目标态数据质量哨兵（T5~T8）

对 output/general_signals_<date>.json 做自检，判定:
  T5 信号计数: 每标的 B/S ∈ [1, 12]
  T6 评分健全: avg_score ∈ [0,1] 且无 NaN；逐信号 score 有限
  T7 时间完整: n_bars ≥ 200（全日 240 根基准）
  T8 无后视自检: 信号 idx 严格递增且 ∈ [0, n_bars-1]；type ∈ {B,S}

用法: python scripts/target_state_sentinel.py [YYYY-MM-DD ...]
退出码: 0=全过, 1=有 FAIL
"""
import os, sys, json, math

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')

MAX_SIGNALS = 12   # 对齐 MAX_B_DAILY / MAX_S_DAILY
MIN_BARS = 200


def check_symbol(sym, s):
    res = {'sym': sym, 'checks': {}}
    # T5
    n_b, n_s = s.get('n_b', 0), s.get('n_s', 0)
    t5 = (1 <= n_b <= MAX_SIGNALS) and (1 <= n_s <= MAX_SIGNALS)
    res['checks']['T5_counts'] = {'pass': t5, 'detail': f"B={n_b} S={n_s} (需 ∈[1,{MAX_SIGNALS}])"}
    # T6
    avg = s.get('avg_score')
    t6 = avg is not None and math.isfinite(float(avg)) and 0.0 <= float(avg) <= 1.0
    bad = [sig.get('score') for sig in s.get('signals', [])
           if not (isinstance(sig.get('score'), (int, float)) and math.isfinite(float(sig.get('score'))))]
    if bad:
        t6 = False
    res['checks']['T6_scores'] = {'pass': t6, 'detail': f"avg_score={avg} 异常信号数={len(bad)}"}
    # T7
    n_bars = s.get('n_bars', 0)
    t7 = n_bars >= MIN_BARS
    res['checks']['T7_bars'] = {'pass': t7, 'detail': f"n_bars={n_bars} (需 ≥{MIN_BARS})"}
    # T8
    sigs = s.get('signals', [])
    idxs = [sig.get('idx', -1) for sig in sigs]
    ordered = all(b > a for a, b in zip(idxs, idxs[1:]))
    inrange = all(0 <= i < n_bars for i in idxs)
    valid_type = all(sig.get('type') in ('B', 'S') for sig in sigs)
    t8 = ordered and inrange and valid_type
    res['checks']['T8_no_lookahead'] = {
        'pass': t8,
        'detail': f"n_sig={len(sigs)} 严格递增={ordered} idx在范围={inrange} type合法={valid_type}"}
    res['all_pass'] = all(c['pass'] for c in res['checks'].values())
    return res


def main():
    dates = sys.argv[1:] or [__import__('datetime').date.today().strftime('%Y-%m-%d')]
    overall = True
    for d in dates:
        path = os.path.join(OUT, f'general_signals_{d}.json')
        print(f"\n===== 数据质量哨兵 [{d}] =====")
        if not os.path.exists(path):
            print(f"  ❌ 信号文件缺失: {path}")
            overall = False
            continue
        data = json.load(open(path, encoding='utf-8'))
        syms = data.get('symbols', {})
        if not syms:
            print("  ❌ symbols 为空")
            overall = False
            continue
        ok = True
        for sym, s in syms.items():
            r = check_symbol(sym, s)
            for k, c in r['checks'].items():
                print(f"  {sym} {k}: {'✅' if c['pass'] else '❌'} {c['detail']}")
            ok = ok and r['all_pass']
        overall = overall and ok
        print(f"  >>> [{d}] {'✅ 哨兵全部 PASS' if ok else '❌ 哨兵存在 FAIL'}")
    print(f"\n{'='*60}\n哨兵总判定: {'✅ PASS' if overall else '❌ FAIL'}")
    sys.exit(0 if overall else 1)


if __name__ == '__main__':
    main()
