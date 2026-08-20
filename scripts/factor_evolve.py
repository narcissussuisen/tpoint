"""scripts/factor_evolve.py — 因子演化 CLI（2026-08-18 Phase 3c）

新的「出手」入口：对候选门控做**池级** IS/OOS 评估，产出晋升/淘汰判定，落盘
output/research/factor_evolution_<date>.json。与旧 auto_tune.py（per-symbol 参数网格）
并存：本入口是战略转向后的主路径，auto_tune 仅保留作过渡对照。

CLI：python scripts/factor_evolve.py [--syms 161129.SZ,513310.SH]
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from evolution import evolve, CANDIDATES, OVERFIT_CANDIDATES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default=None, help='逗号分隔标的（默认 watchlist 全部）')
    a = ap.parse_args()
    syms = a.syms.split(',') if a.syms else None

    results, summary = evolve(syms=syms, candidates=CANDIDATES)
    overfit = [__import__('evolution').evaluate_gate(g, syms) for g in OVERFIT_CANDIDATES]

    out = {
        'date': datetime.date.today().strftime('%Y-%m-%d'),
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'summary': summary,
        'results': results,
        'overfit_counterexamples': overfit,
        'principle': '池级整体优化，不对单一标的调参；目标=池级 total_ret + 净夏普 + 逐年稳健；OOS 时间序切分。',
    }
    os.makedirs(os.path.join(ROOT, 'output', 'research'), exist_ok=True)
    p = os.path.join(ROOT, 'output', 'research', f"factor_evolution_{out['date']}.json")
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"[ok] {p}")
    print(f"候选 {summary['n_candidates']} / 晋升 {summary['n_promote']} / 淘汰 {summary['n_candidates'] - summary['n_promote']}")
    for r in results:
        print(f"  {r['gate']:20s} → {r['verdict']}  (OOS Δret {r['d_ret_oos_pp']:+6.2f}pp, Δwr {r['d_wr_oos_pp']:+5.1f}pp)")


if __name__ == '__main__':
    main()
