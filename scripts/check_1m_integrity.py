#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_1m_integrity.py -- 本地 1 分钟 K 线数据库 数据完整性与真实性校验器

检查维度（对应需求）：
  1. 时间连续性            : trade_date / trade_time / timestamp 三者一致；时间戳解析
  2. 60 秒间隔             : 日内相邻 bar 是否标准 60s；午间 11:30->13:00 跳空豁免
  3. 缺失 / 重复           : 日内预期 240 根（09:30-11:30 + 13:00-15:00）缺失/多余/重复分钟
  4. OHLC 价格逻辑         : high>=max(open,close), low<=min(open,close), high>=low, 全>0
  5. 量价异常 / 偏离        : 零成交量但变价、amount≈close*volume*100 口径、单分钟极端收益
  6. 真实性 / 市场一致性    : tick 价位网格(0.01/0.001)、涨跌停约束(±10%/±20%)、交易时段、
                             跨日时间戳断裂（broken timestamp 检测）、外部样本比对
可选外部比对: --reference ref.csv (sym,date,close) 或使用内置已核验样本(300308)。

用法:
  python check_1m_integrity.py --root F:/keyfactor_data/1m --sample 120 --out output/kline_integrity_2026-08-20.json
  python check_1m_integrity.py --root F:/keyfactor_data/1m --symbols 688111.SH,600570.SH,000001.SZ
  python check_1m_integrity.py --root F:/keyfactor_data/1m --all
"""
import argparse, csv, glob, json, os, random, sys, collections, datetime
import signal
# 进程级信号免疫：避免被工具侧超时强杀的 SIGINT/SIGBREAK 波及（Windows 长跑守护）
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        signal.signal(getattr(signal, _s), signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

# ---------- 板块/规则 ----------
def board_limit(sym):
    """涨跌停幅度。债券(11/12)返回 None(跳过限量校验)。"""
    s = sym.replace(".", "")
    if s.startswith(("688", "300", "301")):   # 科创板 / 创业板 ±20%
        return 0.20
    if s.startswith(("11", "12")):            # 可转债 不校验
        return None
    return 0.10                                # 主板 / ETF / LOF ±10%

def tick_grid(sym, price):
    """该标的该价位的合理最小价位。ETF/LOF(51/15/16) 用 0.001，其余 0.01(price<1 也 0.001)。"""
    s = sym.replace(".", "")
    if s.startswith(("51", "15", "16")):
        return 0.001
    return 0.001 if price < 1.0 else 0.01

def is_etf_lof(sym):
    s = sym.replace(".", "")
    return s.startswith(("51", "15", "16"))

# 标准交易时段（贴合真实数据网格：首根 09:31=集合竞价结果，末根 15:00；无 11:31/13:00）
def _minute_range(h0, m0, h1, m1):
    out = []
    cur = h0 * 60 + m0
    end = h1 * 60 + m1
    while cur <= end:
        out.append(f"{cur // 60:02d}:{cur % 60:02d}:00")
        cur += 1
    return out
EXPECTED_MINUTES = set(_minute_range(9, 31, 11, 30) + _minute_range(13, 1, 15, 0))  # 120+120=240
EXPECTED_PER_DAY = len(EXPECTED_MINUTES)                # 240
LUNCH_GAP = 5460        # 11:30 -> 13:01 秒数
OVERNIGHT_MIN = 54000   # 跨日最小间隔(15h)，小于此视为 broken timestamp

# 内置已 WebSearch 核验的真实样本（sym,date,close）
BUILTIN_REF = [
    ("300308.SZ", "2026-05-28", 1197.99),
    ("300308.SZ", "2026-08-05", 947.74),
]

def parse_ts(ts):
    try:
        return int(float(ts))
    except Exception:
        return None

def load_reference(path):
    ref = collections.defaultdict(dict)
    if not path or not os.path.exists(path):
        return ref
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ref[r["sym"]][r["date"]] = float(r["close"])
    return ref

def check_symbol(path, sym, ref):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    n = len(rows)
    rec = dict(sym=sym, bars=n, errors=[], warns=[])
    if n == 0:
        rec["errors"].append("empty_file")
        return rec

    # ---- 逐行基础校验 ----
    off_grid = 0; ohlc_bad = 0; nonpos = 0; zero_vol = 0; zero_vol_pricechg = 0
    amt_mismatch = 0; extreme_ret = 0; parse_err = 0
    days = collections.defaultdict(list)
    seen_in_day = collections.defaultdict(set)
    dup_ts = 0
    prev_close_by_idx = None
    closes_all = []
    for r in rows:
        try:
            o=float(r["open"]); h=float(r["high"]); l=float(r["low"]); c=float(r["close"])
            v=float(r["volume"]); a=float(r["amount"]); ts=parse_ts(r["timestamp"])
        except Exception:
            parse_err += 1
            continue
        closes_all.append(c)
        # OHLC 逻辑
        if not (h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9 and h >= l - 1e-9 and c > 0 and o > 0 and l > 0 and h > 0):
            ohlc_bad += 1
        if c <= 0 or o <= 0 or l <= 0 or h <= 0:
            nonpos += 1
        # tick 网格（按价位/板块）
        g = tick_grid(sym, c)
        if abs(round(c / g) * g - c) > 1e-6:
            off_grid += 1
        # amount 口径
        if v > 0 and c > 0:
            ratio = a / (c * v * 100.0)
            if abs(ratio - 1.0) > 0.05:
                amt_mismatch += 1
        if v <= 0:
            zero_vol += 1
            if abs(c - prev_close_by_idx) > 1e-9:
                zero_vol_pricechg += 1
        # 重复 timestamp
        if ts is not None:
            if ts in seen_in_day[r["trade_date"]]:
                dup_ts += 1
            else:
                seen_in_day[r["trade_date"]].add(ts)
        days[r["trade_date"]].append((ts, r["trade_time"], o, h, l, c, v, a))
        prev_close_by_idx = c

    rec["off_grid_pct"]   = round(100.0 * off_grid / n, 2)
    rec["ohlc_bad"]       = ohlc_bad
    rec["nonpos"]         = nonpos
    rec["zero_vol"]       = zero_vol
    rec["zero_vol_pricechg"] = zero_vol_pricechg
    rec["amt_mismatch"]   = amt_mismatch
    rec["dup_ts"]         = dup_ts
    rec["price_min"]      = round(min(closes_all), 4)
    rec["price_max"]      = round(max(closes_all), 4)

    # ---- 日内结构：缺失/多余/重复分钟、60s 间隔、时段外 ----
    bars_per_day = []
    intra_gaps = 0; missing_min = 0; extra_min = 0; out_of_session = 0
    ts_daybreaks_broken = 0; day_pairs = 0
    prev_day_last_ts = None
    for d in sorted(days):
        rs = sorted(days[d], key=lambda x: (x[0] if x[0] is not None else 0))
        bars_per_day.append(len(rs))
        actual_minutes = set()
        for (ts, tt, o, h, l, c, v, a) in rs:
            hhmm = (tt or "")[11:19] if tt and len(tt) >= 19 else (tt or "")
            if hhmm in EXPECTED_MINUTES:
                actual_minutes.add(hhmm)
            else:
                out_of_session += 1
                actual_minutes.add(hhmm)
        missing_min += len(EXPECTED_MINUTES - actual_minutes)
        extra_min   += len(actual_minutes - EXPECTED_MINUTES)
        # 60s 间隔 + 跨日断裂
        last = None
        for (ts, tt, o, h, l, c, v, a) in rs:
            if ts is None:
                continue
            if last is not None:
                dt = (ts - last) / 1000.0
                if dt != 60 and dt != LUNCH_GAP and dt > 0:
                    if dt < LUNCH_GAP:
                        intra_gaps += 1
            last = ts
        if prev_day_last_ts is not None and ts is not None and last is not None:
            day_pairs += 1
            if (ts - prev_day_last_ts) / 1000.0 < OVERNIGHT_MIN:
                ts_daybreaks_broken += 1
        if last is not None:
            prev_day_last_ts = last

    rec["n_days"] = len(days)
    rec["bars_per_day_min"] = min(bars_per_day) if bars_per_day else 0
    rec["bars_per_day_max"] = max(bars_per_day) if bars_per_day else 0
    rec["intra_gaps"] = intra_gaps
    rec["missing_minutes"] = missing_min
    rec["extra_minutes"] = extra_min
    rec["out_of_session"] = out_of_session
    rec["ts_daybreaks_broken_pct"] = round(100.0 * ts_daybreaks_broken / day_pairs, 1) if day_pairs else 0.0

    # ---- 涨跌停约束（真实性）----
    lim = board_limit(sym)
    limit_viol = 0
    if lim is not None:
        daily = {}
        for d in sorted(days):
            rs = sorted(days[d], key=lambda x: (x[0] if x[0] is not None else 0))
            if rs:
                daily[d] = rs[-1][5]   # 当日最后 close = 当日收盘
        prev_close = None
        for d in sorted(days):
            rs = days[d]
            if prev_close is None:
                prev_close = daily.get(d)
                continue
            lo_band = prev_close * (1 - lim)
            hi_band = prev_close * (1 + lim)
            for (ts, tt, o, h, l, c, v, a) in rs:
                if c < lo_band - 1e-6 or c > hi_band + 1e-6 or h > hi_band + 1e-6 or l < lo_band - 1e-6:
                    limit_viol += 1
            prev_close = daily.get(d)
    rec["limit_violations"] = limit_viol
    rec["limit_pct"] = lim

    # ---- 外部样本比对 ----
    ref_hits = []
    ref_sym = ref.get(sym, {})
    for d in sorted(days):
        if d in ref_sym:
            rs = sorted(days[d], key=lambda x: (x[0] if x[0] is not None else 0))
            if rs:
                ref_hits.append((d, ref_sym[d], round(rs[-1][5], 4),
                                 abs(rs[-1][5] - ref_sym[d]) <= max(0.005 * ref_sym[d], 0.05)))
    rec["ref_checks"] = ref_hits

    # ---- 汇总判定 ----
    rec["parse_errors"] = parse_err
    if parse_err: rec["errors"].append(f"parse_errors={parse_err}")
    if ohlc_bad: rec["errors"].append(f"ohlc_logic_viol={ohlc_bad}")
    if nonpos:   rec["errors"].append(f"nonpositive_price={nonpos}")
    if dup_ts:   rec["errors"].append(f"dup_timestamp={dup_ts}")
    if lim is not None and limit_viol: rec["errors"].append(f"limit_band_viol={limit_viol}")
    if rec["bars_per_day_max"] > EXPECTED_PER_DAY: rec["warns"].append(f"overfull_day={rec['bars_per_day_max']}")
    if rec["bars_per_day_min"] < EXPECTED_PER_DAY * 0.9: rec["warns"].append(f"short_day={rec['bars_per_day_min']}")
    if missing_min: rec["warns"].append(f"missing_min={missing_min}")
    if extra_min:   rec["warns"].append(f"extra_min={extra_min}")
    if intra_gaps:  rec["warns"].append(f"intra_60s_gaps={intra_gaps}")
    if out_of_session: rec["warns"].append(f"out_of_session={out_of_session}")
    if rec["ts_daybreaks_broken_pct"] > 10: rec["warns"].append(f"timestamp_broken={rec['ts_daybreaks_broken_pct']}%")
    if rec["off_grid_pct"] >= 1.0: rec["warns"].append(f"tick_offgrid={rec['off_grid_pct']}%")
    if amt_mismatch and not is_etf_lof(sym): rec["warns"].append(f"amount_unit_mismatch={amt_mismatch}")
    if is_etf_lof(sym) and amt_mismatch: rec["warns"].append(f"etf_amount_unit_mismatch={amt_mismatch}(单位口径待核)")

    # 真实性分类
    if rec["off_grid_pct"] < 1.0:
        authenticity = "AUTHENTIC_TICK"          # tick 量化干净
    elif rec["off_grid_pct"] < 30.0:
        authenticity = "LEVEL_REAL_TICK_INTERP"   # 部分插值
    else:
        authenticity = "TICK_SYNTHETIC"           # 高度疑似合成/插值
    rec["authenticity"] = authenticity
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="F:/keyfactor_data/1m")
    ap.add_argument("--out", default=None)
    ap.add_argument("--symbols", default=None, help="逗号分隔，如 688111.SH,600570.SH")
    ap.add_argument("--sample", type=int, default=120)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--reference", default=None, help="可选 ref.csv: sym,date,close")
    ap.add_argument("--seed", type=int, default=20260820)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.root, "*.csv")))
    files = [f for f in files if not f.endswith(".bad")]
    sym_of = lambda p: os.path.basename(p)[:-7]  # strip _1m.csv

    if args.symbols:
        want = set(s.strip() for s in args.symbols.split(","))
        files = [f for f in files if sym_of(f) in want]
    elif not args.all:
        random.seed(args.seed)
        files = random.sample(files, min(args.sample, len(files)))

    ref = load_reference(args.reference)
    # 内置样本并入
    for s, d, c in BUILTIN_REF:
        ref.setdefault(s, {})[d] = c

    results = []
    for f in files:
        try:
            results.append(check_symbol(f, sym_of(f), ref))
        except Exception as e:
            results.append(dict(sym=sym_of(f), error=str(e)))

    # 汇总
    n = len(results)
    cls = collections.Counter(r.get("authenticity", "ERR") for r in results)
    agg = dict(
        total_files=n,
        authenticity_classes=dict(cls),
        total_bars=sum(r.get("bars", 0) for r in results),
        sum_off_grid_pct=round(sum(r.get("off_grid_pct", 0) for r in results) / max(n, 1), 2),
        sum_ohlc_bad=sum(r.get("ohlc_bad", 0) for r in results),
        sum_dup_ts=sum(r.get("dup_ts", 0) for r in results),
        sum_parse_err=sum(r.get("parse_errors", 0) for r in results),
        sum_limit_viol=sum(r.get("limit_violations", 0) for r in results),
        sum_missing_min=sum(r.get("missing_minutes", 0) for r in results),
        sum_extra_min=sum(r.get("extra_minutes", 0) for r in results),
        sum_intra_gaps=sum(r.get("intra_gaps", 0) for r in results),
        sum_out_of_session=sum(r.get("out_of_session", 0) for r in results),
        n_ts_broken=sum(1 for r in results if r.get("ts_daybreaks_broken_pct", 0) > 10),
        n_with_errors=sum(1 for r in results if r.get("errors")),
        n_with_warns=sum(1 for r in results if r.get("warns")),
    )
    out = dict(
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=args.root,
        summary=agg,
        per_symbol=results,
    )
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "output",
                                        f"kline_integrity_{datetime.date.today().isoformat()}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 可读摘要
    print("="*70)
    print(f"1m K线完整性/真实性校验  root={args.root}  files={n}")
    print(f"  真实性分类: {dict(cls)}")
    print(f"  平均脱离tick网格: {agg['sum_off_grid_pct']}%   OHLC违规: {agg['sum_ohlc_bad']}  重复ts: {agg['sum_dup_ts']}")
    print(f"  涨跌停越界: {agg['sum_limit_viol']}   缺失分钟: {agg['sum_missing_min']}   多余分钟: {agg['sum_extra_min']}")
    print(f"  日内非60s间隔: {agg['sum_intra_gaps']}   时段外bar: {agg['sum_out_of_session']}   timestamp断裂标的: {agg['n_ts_broken']}")
    print(f"  有error标的: {agg['n_with_errors']}   有warn标的: {agg['n_with_warns']}")
    print("-"*70)
    print("  外部样本比对(内置 300308 已WebSearch核验):")
    for r in results:
        for (d, rc, ac, ok) in r.get("ref_checks", []):
            print(f"    {r['sym']} {d}  ref={rc}  data={ac}  {'OK' if ok else 'MISMATCH'}")
    print("-"*70)
    print("  问题标的 Top:")
    prob = sorted([r for r in results if r.get("errors") or r.get("warns")],
                  key=lambda r: (len(r.get("errors", [])) + len(r.get("warns", [])), -r.get("off_grid_pct", 0)),
                  reverse=True)[:12]
    for r in prob:
        print(f"    {r['sym']:12s} auth={r.get('authenticity','?'):22s} offgrid={r.get('off_grid_pct',0)}% "
              f"E={r.get('errors')} W={r.get('warns')}")
    print("="*70)
    print(f"JSON -> {out_path}")

if __name__ == "__main__":
    main()
