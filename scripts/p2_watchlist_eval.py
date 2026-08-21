# -*- coding: utf-8 -*-
"""
p2_watchlist_eval.py -- P2.1 watchlist-5 prod-target subset exact evaluation.
Runs the 5 monitor symbols under FIXSTOP = off / 1.5 / 2.0 / 2.5 with the live
exit (use_stop=False + trail0.4/0.6) and reports exact combined metrics:
net WR / P/L ratio / avg win / avg loss / avg trip / worst / exit reasons.
The roadmap Phase-1 "net WR>=55%" target refers to this 5-symbol prod
reconciliation subset, so this is the core acceptance evidence for P2.
"""
import os, sys, json
import numpy as np
ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from p2_diagnose import run_symbol_trips

WATCHLIST = ['161129.SZ', '300058.SZ', '513310.SH', '600570.SH', '688111.SH']
GAP = 8
OUT = r'F:/WorkBuddyItem/automation-2026-08-03-09-39-31'


def combined(trips):
    if not trips:
        return None
    rets = np.array([float(t['ret_pct']) for t in trips])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    aw = float(wins.mean()) if len(wins) else 0.0
    al = float(losses.mean()) if len(losses) else 0.0
    pl = round(aw / abs(al), 3) if al else None
    by_reason = {}
    for t in trips:
        by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1
    return dict(
        n=len(rets),
        wr=round(100.0 * len(wins) / len(rets), 1),
        pl_ratio=pl,
        avg_win=round(aw, 4), avg_loss=round(al, 4),
        avg_trip=round(float(rets.mean()), 4),
        total_ret=round(float(rets.sum()), 2),
        worst=round(float(rets.min()), 2),
        by_reason=by_reason,
    )


def main():
    configs = [('off', None), ('1.2', 1.2), ('1.3', 1.3), ('1.5', 1.5), ('2.0', 2.0)]
    rows = {}
    for label, fs in configs:
        all_t = []
        per = {}
        for sym in WATCHLIST:
            t = run_symbol_trips(sym, GAP, fs)
            if t:
                per[sym] = combined(t)
                all_t.extend(t)
        rows[label] = dict(combined=combined(all_t), per=per)

    print("\n=== P2.1 watchlist-5 exact combined (gap=%d, live no-atr+trail) ===" % GAP)
    hdr = "%-8s%6s%8s%8s%9s%10s%10s%9s" % ('config', 'n', 'netWR%', 'P/L', 'avgWin%', 'avgLoss%', 'avgTrip%', 'worst%')
    print(hdr)
    for label, fs in configs:
        c = rows[label]['combined']
        if c:
            print("%-8s%6d%8.1f%8s%9.4f%10.4f%10.4f%9.2f" % (
                label, c['n'], c['wr'], str(c['pl_ratio']),
                c['avg_win'], c['avg_loss'], c['avg_trip'], c['worst']))

    print("\n--- per-symbol P/L / WR (1.5pct config) ---")
    for sym in WATCHLIST:
        p = rows['1.5']['per'].get(sym)
        if p:
            print("  %-12s n=%-5d WR=%-5.1f%%  P/L=%s  worst=%s%%" % (
                sym, p['n'], p['wr'], str(p['pl_ratio']), str(p['worst'])))

    print("\n--- exit-reason mix per config (watchlist combined) ---")
    for label, fs in configs:
        c = rows[label]['combined']
        if c:
            br = c['by_reason']
            s = "  ".join("%s=%d(%.0f%%)" % (k, v, 100.0 * v / c['n']) for k, v in
                          sorted(br.items(), key=lambda kv: -kv[1]))
            print("  %-6s: %s" % (label, s))

    out = dict(meta=dict(gap=GAP, watchlist=WATCHLIST, exit='live(no-atr+trail0.4/0.6)'),
               configs={label: rows[label] for label, _ in configs})
    fn = os.path.join(OUT, 'p2_watchlist_eval.json')
    json.dump(out, open(fn, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print("\nJSON -> %s" % fn)


if __name__ == '__main__':
    main()
