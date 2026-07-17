#!/usr/bin/env python3
"""完整性审计: 只读扫描所有已落盘 1m CSV, 用 dl_core.verify_csv 校验,
汇总各类异常并生成 HTML 报告 (飞书偏好交付格式) + JSON (机读)。

只读: 不移动/不删除任何文件, 仅把校验结果写入共享缓存 (.integrity_store.json),
顺便为下载引擎预热缓存。

用法:
  python audit_integrity.py                       # 默认输出到 keyfactor_data/
  python audit_integrity.py --months 6 --html x.html --json y.json
  python audit_integrity.py --top 300            # 问题表最多列 300 行
"""
import os
import sys
import json
import glob
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pandas as pd
from dl_core import IntegrityStore, verify_csv, expected_days

DATA = os.path.join(HERE, "..", "keyfactor_data")
MANIFEST = os.path.join(DATA, "universe_ashare_full.csv")
ONED = os.path.join(DATA, "1m")
SHORT_MARKER = os.path.join(DATA, ".short_history.txt")
STORE_PATH = os.path.join(DATA, ".integrity_store.json")

DEFAULT_HTML = os.path.join(DATA, "integrity_audit.html")
DEFAULT_JSON = os.path.join(DATA, "integrity_audit.json")


def load_short_set():
    s = set()
    if os.path.exists(SHORT_MARKER):
        with open(SHORT_MARKER, "r", encoding="utf-8") as f:
            for line in f:
                x = line.strip()
                if x:
                    s.add(x)
    return s


def run_audit(manifest, months, html_path=DEFAULT_HTML, json_path=DEFAULT_JSON, top=300):
    t0 = datetime.datetime.now()
    store = IntegrityStore(STORE_PATH)
    short = load_short_set()
    man = pd.read_csv(manifest, dtype={"sym": str, "name": str})
    total_syms = len(man)

    landed = glob.glob(os.path.join(ONED, "*.csv"))
    n_landed = len(landed)

    good = 0
    by_error = {}            # error_tag -> 含该问题的文件数
    problem = []            # [{sym,bars,ndates,coverage,errors}]
    for fp in landed:
        sym = os.path.basename(fp).split("_1m.csv")[0]
        rep = store.check(sym, fp, months, short)   # 只读, 不移动文件
        if rep.ok:
            good += 1
        else:
            for e in rep.errors:
                tag = e.split(":")[0]
                by_error[tag] = by_error.get(tag, 0) + 1
            problem.append({"sym": sym, "bars": rep.bars, "ndates": rep.ndates,
                           "coverage": rep.coverage, "errors": rep.errors})

    # 缺失(未落盘)
    landed_syms = {os.path.basename(f).split("_1m.csv")[0] for f in landed}
    missing = [r["sym"] for _, r in man.iterrows() if r["sym"] not in landed_syms]
    if missing:
        by_error["missing"] = len(missing)

    n_bad = len(problem)
    pct_good = round(100 * good / n_landed, 1) if n_landed else 0

    # 排序: 跨度不完整(incomplete_days) 最需处理, 其次按 ndates 升序
    def _sev(p):
        inc = any(e.startswith("incomplete_days") for e in p["errors"])
        return (0 if inc else 1, p["ndates"])
    problem.sort(key=_sev)

    summary = {
        "generated_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": ONED,
        "months": months,
        "expected_trading_days": expected_days(months),
        "universe_total": total_syms,
        "landed": n_landed,
        "good": good,
        "problem": n_bad,
        "missing": len(missing),
        "short_marked": len(short),
        "pct_good": pct_good,
        "by_error": dict(sorted(by_error.items(), key=lambda kv: -kv[1])),
    }

    # ---- JSON ----
    out_json = dict(summary)
    out_json["problem_symbols"] = problem
    out_json["missing_symbols"] = missing
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(out_json, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("  JSON 写出失败:", e)

    # ---- HTML ----
    html = _build_html(summary, problem, missing, top)
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print("  HTML 写出失败:", e)

    # ---- stdout ----
    print(f"\n=== 完整性审计 ({summary['generated_at']}) ===")
    print(f"  universe={total_syms}  landed={n_landed}  good={good} "
          f"problem={n_bad}  missing={len(missing)}  short_marked={len(short)}")
    print(f"  完整率(已落盘口径)={pct_good}%")
    print(f"  异常分类: {summary['by_error']}")
    print(f"  报告: {html_path}")
    print(f"  JSON: {json_path}")
    store.flush()      # 批量化落盘, 让自迭代引擎复用缓存
    return summary


def _build_html(summary, problem, missing, top):
    cards = (
        f'<div class="card"><div class="num">{summary["universe_total"]}</div><div class="lbl">universe 总数</div></div>'
        f'<div class="card"><div class="num">{summary["landed"]}</div><div class="lbl">已落盘</div></div>'
        f'<div class="card ok"><div class="num">{summary["good"]}</div><div class="lbl">通过完整性</div></div>'
        f'<div class="card bad"><div class="num">{summary["problem"]}</div><div class="lbl">有问题文件</div></div>'
        f'<div class="card warn"><div class="num">{summary["missing"]}</div><div class="lbl">缺失(未落盘)</div></div>'
        f'<div class="card"><div class="num">{summary["short_marked"]}</div><div class="lbl">短历史已标记</div></div>'
    )
    err_rows = "".join(
        f'<tr><td><code>{k}</code></td><td class="c">{v}</td></tr>'
        for k, v in summary["by_error"].items()
    ) or '<tr><td colspan="2">无</td></tr>'

    shown = problem[:top]
    prob_rows = ""
    for p in shown:
        errs = " ".join(f'<span class="tag">{e}</span>' for e in p["errors"])
        sev = "sev1" if any(e.startswith("incomplete_days") for e in p["errors"]) else "sev2"
        prob_rows += (
            f'<tr class="{sev}"><td><code>{p["sym"]}</code></td>'
            f'<td class="c">{p["bars"]}</td><td class="c">{p["ndates"]}</td>'
            f'<td class="c">{p["coverage"]}</td><td>{errs}</td></tr>'
        )
    if len(problem) > top:
        prob_rows += f'<tr><td colspan="5" class="muted">… 仅显示前 {top} / 共 {len(problem)} 个问题文件</td></tr>'

    miss_txt = ", ".join(missing[:60]) if missing else "无"
    if len(missing) > 60:
        miss_txt += f" …(共 {len(missing)})"

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tpoint 数据完整性审计</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
  margin:0;background:#f5f6f8;color:#1f2329;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#8a9099;font-size:13px;margin-bottom:20px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #e6e8eb;border-radius:10px;
  padding:14px 18px;min-width:120px;flex:1}}
.card .num{{font-size:26px;font-weight:700}}
.card .lbl{{font-size:12px;color:#8a9099;margin-top:2px}}
.card.ok .num{{color:#1f9d55}} .card.bad .num{{color:#d4380d}}
.card.warn .num{{color:#d48806}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6e8eb;
  border-radius:10px;overflow:hidden;font-size:13px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #eef0f2}}
th{{background:#fafbfc;color:#5a6068;font-weight:600}}
td.c{{text-align:right;font-variant-numeric:tabular-nums}}
code{{background:#f0f2f5;padding:1px 5px;border-radius:4px;font-size:12px}}
.tag{{display:inline-block;background:#fff1f0;border:1px solid #ffccc7;color:#cf1322;
  border-radius:4px;padding:1px 6px;margin:1px 3px 1px 0;font-size:11px}}
tr.sev1{{background:#fff7e6}} tr.sev2{{background:#fff}}
.muted{{color:#a0a6ad;font-size:12px}}
.sec{{margin:22px 0 10px;font-size:15px;font-weight:600}}
</style></head>
<body>
<h1>tpoint · 1m 数据完整性审计</h1>
<div class="sub">生成时间 {summary['generated_at']} · 数据集 {summary['dataset']}<br>
窗口 {summary['months']} 月 · 跨度完整性阈值 ≥ {summary['expected_trading_days']} 个交易日 · 完整率(已落盘口径) {summary['pct_good']}%</div>
<div class="cards">{cards}</div>

<div class="sec">异常分类（按文件数）</div>
<table><thead><tr><th>异常类型</th><th class="c">文件数</th></tr></thead>
<tbody>{err_rows}</tbody></table>

<div class="sec">问题文件明细（incomplete_days 高亮，最需处理）</div>
<table><thead><tr><th>symbol</th><th class="c">bars</th><th class="c">交易日</th>
<th class="c">覆盖比</th><th>异常</th></tr></thead>
<tbody>{prob_rows}</tbody></table>

<div class="sec">缺失（未落盘）symbol</div>
<div class="muted">{miss_txt}</div>

<div class="sec">修复方式</div>
<div class="muted">运行 <code>python download_supervisor.py</code> 自迭代引擎：自动扫描缺口 → 重下载 → 完整性校验 → 隔离坏文件到 .bad → 循环直到全达标。</div>
</body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--html", default=DEFAULT_HTML)
    ap.add_argument("--json", default=DEFAULT_JSON)
    ap.add_argument("--top", type=int, default=300)
    a = ap.parse_args()
    run_audit(a.manifest, a.months, a.html, a.json, a.top)
