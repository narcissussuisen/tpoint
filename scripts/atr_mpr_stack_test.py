"""ATR×mpr 叠加验证（阶段A遗留收尾）：
watchlist 5 只 × {baseline, mpr_b60, atr025, atr025+mpr_b60} 固定全集口径对比。

目的：确认 ATR 门控与 mpr_b60 叠加是否过度过滤（两者都滤 B 入场）。
若叠加后胜率 ≥ 单用且样本保留可接受 → 一起上；否则只上更优者。
"""
import os, sys, json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

from backtest_screener import backtest_symbol

WATCHLIST = ['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH']
F = 'F:/keyfactor_data/1m'
MHD = 0.15  # 生产 P0 阈值

COMBOS = [
    ('baseline',       {'atr_min_pct': None, 'mpr_enable': False}),
    ('mpr_b60',        {'atr_min_pct': None, 'mpr_enable': 'B', 'mpr_periods': (60,)}),
    ('atr025',         {'atr_min_pct': 0.25, 'mpr_enable': False}),
    ('atr025_mpr_b60', {'atr_min_pct': 0.25, 'mpr_enable': 'B', 'mpr_periods': (60,)}),
]


def main():
    out = {'combos': {}}
    for cname, kw in COMBOS:
        per_sym = {}
        for sym in WATCHLIST:
            p = os.path.join(F, sym + '_1m.csv')
            r = backtest_symbol(p, macd_min_hist_diff=MHD, **kw)
            m = r['metrics']
            per_sym[sym] = {
                'total': m['total'], 'win_rate': round(m['win_rate'], 1),
                'pl_ratio': round(m['pl_ratio'], 2), 'days': r['days'],
            }
        n = [v['total'] for v in per_sym.values()]
        wrs = [v['win_rate'] for v in per_sym.values()]
        out['combos'][cname] = {
            'per_symbol': per_sym,
            'pool_total': sum(n),
            'med_win': round(sorted(wrs)[len(wrs) // 2], 1) if wrs else 0,
        }
        print(f"\n== {cname} ==")
        for sym, v in per_sym.items():
            print(f"  {sym}: total={v['total']} win={v['win_rate']}% pl={v['pl_ratio']}")
        print(f"  池合计: total={out['combos'][cname]['pool_total']} 中位胜率={out['combos'][cname]['med_win']}%")

    # 对比表
    base = out['combos']['baseline']
    print("\n=== 对比（vs baseline）===")
    print(f"{'combo':<16}{'Δtotal':>8}{'Δmed_win':>10}  {'保留率':>8}")
    for cname in ['mpr_b60', 'atr025', 'atr025_mpr_b60']:
        c = out['combos'][cname]
        dt = c['pool_total'] - base['pool_total']
        dw = c['med_win'] - base['med_win']
        keep = c['pool_total'] / base['pool_total'] * 100 if base['pool_total'] else 0
        print(f"{cname:<16}{dt:>+8}{dw:>+9.1f}pp  {keep:>7.0f}%")

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data',
                           'atr_mpr_stack_test.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n输出: data/atr_mpr_stack_test.json")


if __name__ == '__main__':
    main()
