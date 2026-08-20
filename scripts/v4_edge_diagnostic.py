# -*- coding: utf-8 -*-
"""
v4 方向性 edge 诊断（Craft 模式，用户理论复核用）
==================================================
目标：检验 v4 均值回复信号的方向性 edge 是否随「信号强度」单调上升。
若强信号(极端读数)准确率显著高于弱信号，则证明 v4 有真实 edge，
上一轮"无 edge"结论源于目标函数把强弱信号平均稀释 —— 正确用法是"只交易强信号"。

检验维度：
  1) 强度分层：weak / medium / strong（按 |composite| vs medium_band/strong_band）
  2) |score| 阈值扫描：0.50/0.60/0.70/0.80/0.90/1.00 —— 直接给出"越严越准但越稀"曲线
  3) 组件极端分层：B 信号在 rsi<=35(真超卖) vs rsi>35；S 信号在 rsi>=65(真超买) vs rsi<65
  4) 多前瞻窗口：H∈{3,5,8,15,30} 看 reversion 的微观尺度
逐标的聚合 + 池加权，B/S 方向准确性分别统计（B 正确=fr>0, S 正确=fr<0）。

用法：
  python scripts/v4_edge_diagnostic.py --symbols-file output/clean_basket.txt --out output/v4_edge_diag_2026-08-20.json
"""
import os, sys, csv, json, glob, argparse, collections, datetime
sys.path.insert(0, r"C:/Users/YZP/WorkBuddy/Claw/tpoint/core")
import numpy as np
from composite_scorer import CompositeConfig, detect_signals_v4
from indicators import compute_indicators

DATA = r"F:/keyfactor_data/1m"
HORIZONS = [3, 5, 8, 15, 30]
SCORE_THR = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]


def load_day(sym, date):
    p = f"{DATA}/{sym}_1m.csv"
    if not os.path.exists(p):
        return None
    rows = [r for r in csv.DictReader(open(p, encoding="utf-8-sig")) if r["trade_date"] == date]
    if len(rows) < 200:
        return None
    rows.sort(key=lambda x: int(x["timestamp"]))
    c = np.array([float(x["close"]) for x in rows])
    o = np.array([float(x["open"]) for x in rows])
    h = np.array([float(x["high"]) for x in rows])
    lo = np.array([float(x["low"]) for x in rows])
    v = np.array([float(x["volume"]) for x in rows])
    return o, h, lo, c, v


def sym_days(sym):
    p = f"{DATA}/{sym}_1m.csv"
    if not os.path.exists(p):
        return []
    days = collections.defaultdict(int)
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        days[r["trade_date"]] += 1
    return sorted(d for d, n in days.items() if n >= 200)


def acc_dir(pairs, side):
    sel = [(b, fr) for (b, fr) in pairs if b == side]
    if not sel:
        return None, 0
    ok = sum(1 for b, fr in sel if (b and fr > 0) or (not b and fr < 0))
    return 100.0 * ok / len(sel), len(sel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", default=r"C:/Users/YZP/WorkBuddy/Claw/tpoint/output/clean_basket.txt")
    ap.add_argument("--out", default=r"C:/Users/YZP/WorkBuddy/Claw/tpoint/output/v4_edge_diag_2026-08-20.json")
    ap.add_argument("--last-n", type=int, default=0, help="0=全部可用日")
    a = ap.parse_args()

    syms = [s.strip() for s in open(a.symbols_file, encoding="utf-8") if s.strip()]
    cfg = CompositeConfig()
    cfg.trend_b_allowed = (-1, 0, 1)   # 松弛门控，避免 B 侧被生产闸门收缩（纯测信号质量）
    cfg.trend_s_allowed = (-1, 0, 1)

    # 收集每条信号的 (type, score, strength, rsi, band, rsi_extreme, fr_by_H)
    sigs = []  # each: dict
    print(f"[load] {len(syms)} 清洁标的, 松弛门控, 全历史日", flush=True)
    for si, sym in enumerate(syms, 1):
        print(f"[sym {si}] {sym} ...", flush=True)
        try:
            days = sym_days(sym)
            if a.last_n:
                days = days[-a.last_n:]
            for d in days:
                ld = load_day(sym, d)
                if ld is None:
                    continue
                o, h, lo, c, v = ld
                pc = c[0]
                data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
                ss = detect_signals_v4(data, pc, cfg)
                n = len(c)
                for s in ss:
                    i = s["idx"]
                    frs = {}
                    for H in HORIZONS:
                        j = min(i + H, n - 1)
                        frs[H] = (c[j] - c[i]) / c[i] if c[i] > 0 else 0.0
                    sigs.append({
                        "type": s["type"], "score": s["score"], "strength": s["strength"],
                        "band": s["strength_band"], "rsi": s["rsi"], "comps": s["components"],
                        "frs": frs,
                    })
        except Exception as e:
            import traceback
            print(f"[ERR] {sym}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            raise
        if si % 8 == 0:
            print(f"  ... {si}/{len(syms)} syms, {len(sigs)} sigs so far", flush=True)

    print(f"[done load] total sigs={len(sigs)}", flush=True)

    def pairs_for(filt, H):
        out = []
        for s in sigs:
            if filt(s):
                out.append((s["type"] == "B", s["frs"][H]))
        return out

    report = {"n_total": len(sigs), "symbols": syms, "horizons": HORIZONS,
              "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    # ---- 1) 强度分层 (按默认 band) ----
    band_rows = {}
    for H in HORIZONS:
        for band in ["weak", "medium", "strong"]:
            pairs = pairs_for(lambda s, b=band: s["band"] == b, H)
            ab, nb = acc_dir(pairs, True)
            ass, ns = acc_dir(pairs, False)
            allp, nall = acc_dir(pairs, None)
            band_rows.setdefault(band, {})[H] = {
                "acc_all": round(allp, 2) if allp is not None else None, "n": nall,
                "acc_b": round(ab, 2) if ab is not None else None, "n_b": nb,
                "acc_s": round(ass, 2) if ass is not None else None, "n_s": ns}
    report["by_strength_band"] = band_rows

    # ---- 2) |score| 阈值扫描（越严越准 vs 越稀） ----
    thr_rows = {}
    for H in HORIZONS:
        for thr in SCORE_THR:
            pairs = pairs_for(lambda s, t=thr: abs(s["score"]) >= t, H)
            ab, nb = acc_dir(pairs, True)
            ass, ns = acc_dir(pairs, False)
            allp, nall = acc_dir(pairs, None)
            thr_rows.setdefault(str(thr), {})[H] = {
                "acc_all": round(allp, 2) if allp is not None else None, "n": nall,
                "acc_b": round(ab, 2) if ab is not None else None, "n_b": nb,
                "acc_s": round(ass, 2) if ass is not None else None, "n_s": ns}
    report["by_score_threshold"] = thr_rows

    # ---- 3) 组件极端分层（真超卖/超买） ----
    comp_rows = {}
    for H in HORIZONS:
        # B: rsi<=35 真超卖
        b_os = pairs_for(lambda s: s["type"] == "B" and s["rsi"] <= 35, H)
        b_no = pairs_for(lambda s: s["type"] == "B" and s["rsi"] > 35, H)
        # S: rsi>=65 真超买
        s_ob = pairs_for(lambda s: s["type"] == "S" and s["rsi"] >= 65, H)
        s_no = pairs_for(lambda s: s["type"] == "S" and s["rsi"] < 65, H)
        comp_rows[H] = {
            "B_rsi<=35": {"acc": round(acc_dir(b_os, True)[0], 2) if b_os else None, "n": len(b_os)},
            "B_rsi>35": {"acc": round(acc_dir(b_no, True)[0], 2) if b_no else None, "n": len(b_no)},
            "S_rsi>=65": {"acc": round(acc_dir(s_ob, False)[0], 2) if s_ob else None, "n": len(s_ob)},
            "S_rsi<65": {"acc": round(acc_dir(s_no, False)[0], 2) if s_no else None, "n": len(s_no)},
        }
    report["by_rsi_extreme"] = comp_rows

    # ---- 4) 汇总打印 ----
    print("\n===== 强度分层 (|score| band) | H=8 =====")
    for band in ["weak", "medium", "strong"]:
        r = band_rows[band][8]
        print(f"  {band:7s} acc_all={r['acc_all']}% n={r['n']:5d} | B={r['acc_b']}%({r['n_b']}) S={r['acc_s']}%({r['n_s']})")
    print("\n===== |score| 阈值扫描 | H=8 (越严越准 vs 越稀) =====")
    for thr in SCORE_THR:
        r = thr_rows[str(thr)][8]
        print(f"  |score|>={thr:.2f} acc_all={r['acc_all']}% n={r['n']:5d} | B={r['acc_b']}% S={r['acc_s']}%")
    print("\n===== RSI 极端分层 | H=8 =====")
    r = comp_rows[8]
    print(f"  B rsi<=35(真超卖): acc={r['B_rsi<=35']['acc']}% n={r['B_rsi<=35']['n']}")
    print(f"  B rsi>35 (非超卖): acc={r['B_rsi>35']['acc']}% n={r['B_rsi>35']['n']}")
    print(f"  S rsi>=65(真超买): acc={r['S_rsi>=65']['acc']}% n={r['S_rsi>=65']['n']}")
    print(f"  S rsi<65 (非超买): acc={r['S_rsi<65']['acc']}% n={r['S_rsi<65']['n']}")

    json.dump(report, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nJSON -> {a.out}")


if __name__ == "__main__":
    main()
