# -*- coding: utf-8 -*-
"""
select_clean_symbols.py -- 从 1m 完整性扫描结果中筛出「真实可靠干净」的回测标的

输入 : check_1m_integrity.py 产出的 per_symbol 完整性 JSON
输出 :
  - clean_symbols.txt      : 全部通过清洁度门槛的标的（一行一个，供参考）
  - clean_basket.txt       : 按「历史天数」取前 BASKET_N 的回测篮子（v4_param_search --symbols-file 用）
  - clean_summary.json     : 清洁度统计 + 篮子构成

清洁度门槛（对应回测可用性，逐条对应 integrity 字段）:
  authenticity == AUTHENTIC_TICK      : close 几乎全部落在 tick 价位网格(off_grid_pct<1%) —— 真实微观结构核心信号
  ts_daybreaks_broken_pct <= 10       : 跨日时间戳连续（未被「序列化/无隔夜断点」污染）
  parse_errors == 0                   : 无空值/损坏行
  ohlc_bad == 0                        : OHLC 价格逻辑自洽
  dup_ts == 0                          : 无重复 timestamp
  limit_violations/bars <= 0.1%       : 涨跌停越界率极低（容忍除权缺口等极少数真实情形）
  bars_per_day_max <= 241             : 允许真实 09:30 开盘 bar（241/日，非溢出）
  n_days >= MIN_DAYS                  : 历史足够（默认 30 完整日）
  （注: out_of_session 因真实数据含 09:30 bar 恒为 ~145，属 checker 网格口径产物，不计入硬门槛）

用法:
  python select_clean_symbols.py --in output/kline_integrity_full_2026-08-20.json \
        --out-dir output --basket 40 --min-days 30
"""
import argparse, json, os, collections


def board_of(sym):
    s = sym.replace(".", "")
    if s.startswith(("688", "300", "301")):
        return "STAR/CHINEXT"
    if s.startswith(("51", "15", "16")):
        return "ETF/LOF"
    if s.startswith(("11", "12")):
        return "BOND"
    return "MAIN"


def is_clean(r, min_days):
    # 注意：本数据集真实 1m 含 09:30 开盘 bar（241 根/日），故 out_of_session/bars_per_day_max
    # 是 checker 网格口径产物（真实数据恒为 ~145 / 241），不计入清洁度硬门槛，避免误杀真实数据。
    if r.get("authenticity") != "AUTHENTIC_TICK":
        return False, "authenticity=%s" % r.get("authenticity")
    if r.get("ts_daybreaks_broken_pct", 0) > 10:
        return False, "ts_broken=%.1f%%" % r.get("ts_daybreaks_broken_pct", 0)
    if r.get("parse_errors", 0):
        return False, "parse_errors=%d" % r.get("parse_errors", 0)
    if r.get("ohlc_bad", 0):
        return False, "ohlc_bad=%d" % r.get("ohlc_bad", 0)
    if r.get("dup_ts", 0):
        return False, "dup_ts=%d" % r.get("dup_ts", 0)
    bars = max(r.get("bars", 1), 1)
    if r.get("limit_violations", 0) / bars > 0.001:
        return False, "limit_viol_rate=%.3f%%" % (100.0 * r.get("limit_violations", 0) / bars)
    if r.get("bars_per_day_max", 999) > 241:
        return False, "overfull_day=%d" % r.get("bars_per_day_max", 0)
    if r.get("n_days", 0) < min_days:
        return False, "n_days=%d<min" % r.get("n_days", 0)
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="完整性扫描 JSON")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--basket", type=int, default=40, help="回测篮子取历史前 N")
    ap.add_argument("--min-days", type=int, default=30)
    a = ap.parse_args()

    data = json.load(open(a.inp, encoding="utf-8"))
    per = data.get("per_symbol", [])
    root = a.out_dir or os.path.dirname(os.path.abspath(a.inp))
    os.makedirs(root, exist_ok=True)

    clean = []
    reasons = collections.Counter()
    for r in per:
        ok, why = is_clean(r, a.min_days)
        if ok:
            clean.append(r)
        else:
            reasons[why.split("=")[0]] += 1

    clean.sort(key=lambda r: (r.get("n_days", 0), r.get("bars", 0)), reverse=True)
    basket = clean[:a.basket]

    all_path = os.path.join(root, "clean_symbols.txt")
    bsk_path = os.path.join(root, "clean_basket.txt")
    sum_path = os.path.join(root, "clean_summary.json")
    with open(all_path, "w", encoding="utf-8") as f:
        for r in clean:
            f.write(r["sym"] + "\n")
    with open(bsk_path, "w", encoding="utf-8") as f:
        for r in basket:
            f.write(r["sym"] + "\n")

    board_cnt = collections.Counter(board_of(r["sym"]) for r in clean)
    bsk_board = collections.Counter(board_of(r["sym"]) for r in basket)
    summary = dict(
        source=a.inp,
        min_days=a.min_days,
        total_scanned=len(per),
        n_clean=len(clean),
        n_basket=len(basket),
        clean_by_board=dict(board_cnt),
        basket_by_board=dict(bsk_board),
        reject_reasons=dict(reasons),
        basket_days_stats=dict(
            min_days=min((r.get("n_days", 0) for r in basket), default=0),
            max_days=max((r.get("n_days", 0) for r in basket), default=0),
            min_offgrid=min((r.get("off_grid_pct", 99) for r in basket), default=0),
            max_offgrid=max((r.get("off_grid_pct", 0) for r in basket), default=0),
        ),
        basket_symbols=[r["sym"] for r in basket],
    )
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("=" * 64)
    print("清洁度筛选  source=%s" % a.inp)
    print("  扫描总数=%d  清洁=%d (%.1f%%)  篮子=%d" % (
        len(per), len(clean), 100.0 * len(clean) / max(len(per), 1), len(basket)))
    print("  清洁按板块: %s" % dict(board_cnt))
    print("  篮子按板块: %s" % dict(bsk_board))
    print("  被拒原因(top): %s" % dict(reasons.most_common(6)))
    print("  篮子历史天数: min=%d max=%d | off-grid: min=%.2f%% max=%.2f%%" % (
        summary["basket_days_stats"]["min_days"], summary["basket_days_stats"]["max_days"],
        summary["basket_days_stats"]["min_offgrid"], summary["basket_days_stats"]["max_offgrid"]))
    print("  篮子标的: %s" % ", ".join(r["sym"] for r in basket))
    print("-" * 64)
    print("  clean_symbols.txt -> %s" % all_path)
    print("  clean_basket.txt  -> %s" % bsk_path)
    print("  clean_summary.json-> %s" % sum_path)
    return summary


if __name__ == "__main__":
    main()
