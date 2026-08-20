"""scripts/shadow_v3_review.py — 收盘复盘用：v3 影子信号 vs 生产信号 对比

用法:
  python scripts/shadow_v3_review.py [YYYY-MM-DD]

读取:
  data/shadow_v3_<date>.jsonl   （v3 影子旁路日志，格式见 core/shadow_v3.py）
  data/signal.txt               （生产实际推送信号，含 K:HH:MM 标签）

输出:
  - v3 信号总量 / 按 reason 分布 / 按标的分布
  - 与生产重叠（同 标的+分钟+方向 命中）与 v3 独有（生产漏抓）笔数
  - v3 独有信号按 reason 拆解（识别"真正增量" vs "非智能噪声"）

注意: signal.txt 跨日累积且只有 HH:MM 无日期，本脚本按"交易日交易时段内 标的+分钟+方向"
      做 best-effort 匹配（同日内分钟唯一，足够复盘用）。无 signal.txt 时仅报 v3 侧。
"""
import sys
import os
import json
import re
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATE = sys.argv[1] if len(sys.argv) > 1 else __import__('datetime').datetime.now().strftime('%Y-%m-%d')


def load_v3(date):
    path = os.path.join(ROOT, 'data', f'shadow_v3_{date}.jsonl')
    recs = []
    if not os.path.exists(path):
        return recs, False
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            recs.append(json.loads(ln))
    return recs, True


def load_prod(date):
    """best-effort 解析 signal.txt 的生产 B/S 信号 -> {(sym, hhmm, type)} 集合。"""
    path = os.path.join(ROOT, 'data', 'signal.txt')
    fired = set()
    if not os.path.exists(path):
        return fired, False
    txt = open(path, encoding='utf-8').read()
    # 匹配: 🟢/🔴 行含 标的名 + K:HH:MM
    sym_map = {}  # name -> sym（从 shadow 日志反查）
    for m in re.finditer(r'\[K:(\d{2}:\d{2})\]', txt):
        pass
    # 逐块: 每块第一行含 emoji + 标的名；找同块 K: 标签
    blocks = txt.split('\n\n')
    for b in blocks:
        lines = b.strip().split('\n')
        if not lines:
            continue
        head = lines[0]
        hhmm = re.search(r'\[K:(\d{2}:\d{2})\]', head)
        if not hhmm:
            # K 标签可能在第二行（write_signal_txt 把 k_tag 拼在第一行末尾）
            for l in lines:
                hhmm = re.search(r'\[K:(\d{2}:\d{2})\]', l)
                if hhmm:
                    break
        if not hhmm:
            continue
        if '🟢' in head:
            stype = 'B'
        elif '🔴' in head:
            stype = 'S'
        else:
            continue
        # 提取标的名（第一个中文字段，如 "华虹公司"）
        nm = re.search(r'([\u4e00-\u9fa5]{2,})', head)
        name = nm.group(1) if nm else None
        if name:
            fired.add((name, hhmm.group(1), stype))
    return fired, True


def main():
    v3_recs, v3_ok = load_v3(DATE)
    if not v3_ok:
        print(f"[shadow_v3_review] 未找到 data/shadow_v3_{DATE}.jsonl —— 今日 shadow 未运行或无信号。")
        return
    # 展开 v3 信号
    v3_signals = []  # (sym, name, hhmm, type, reason)
    for rec in v3_recs:
        for s in rec.get('v3', []):
            bar_ts = s.get('bar_ts', '')  # 'YYYY-MM-DD HH:MM:SS'
            hhmm = bar_ts[11:16] if len(bar_ts) >= 16 else ''
            v3_signals.append((rec['sym'], rec['name'], hhmm, s['type'], s.get('reason')))

    print(f"═══ v3 影子复盘 {DATE} ═══")
    print(f"v3 信号总数: {len(v3_signals)}")
    print(f"  按 reason: {dict(Counter(x[4] for x in v3_signals))}")
    by_sym = Counter(x[0] for x in v3_signals)
    print(f"  按标的: {dict(by_sym)}")

    # 与生产对比
    prod_fired, prod_ok = load_prod(DATE)
    if not prod_ok:
        print("\n[生产侧] 无 signal.txt，跳过重叠对比。")
        return
    # name -> sym 反查（生产日志用 name，v3 用 sym+name）
    name_to_sym = {x[1]: x[0] for x in v3_signals}
    prod_sym = set()
    for (nm, hhmm, st) in prod_fired:
        sym = name_to_sym.get(nm, nm)
        prod_sym.add((sym, hhmm, st))
    v3_set = set((x[0], x[2], x[3]) for x in v3_signals)

    overlap = v3_set & prod_sym
    v3_only = v3_set - prod_sym
    # v3_only 的 reason 拆解
    v3_only_reasons = Counter()
    for x in v3_signals:
        if (x[0], x[2], x[3]) in v3_only:
            v3_only_reasons[x[4]] += 1

    print(f"\n[生产侧] 推送信号(去重后): {len(prod_sym)}")
    print(f"[重叠] v3 与 生产同 标的+分钟+方向 命中: {len(overlap)}")
    print(f"[v3 独有] 生产未推送(漏抓/噪声): {len(v3_only)}")
    if v3_only_reasons:
        print(f"  v3 独有按 reason 拆解(= 真正待评估的增量信号):")
        for r, c in v3_only_reasons.most_common():
            print(f"    {r}: {c}")

    # 判定提示
    print("\n── 结论提示 ──")
    if len(v3_set) == 0:
        print("  v3 今日零信号（震荡/数据缺失）。")
    elif len(overlap) / max(1, len(v3_set)) >= 0.5:
        print(f"  v3 与 生产高重合({len(overlap)/max(1,len(v3_set)):.0%})，增量信号有限。")
    else:
        print(f"  v3 与 生产低重合，{len(v3_only)} 笔为 v3 独有 —— 重点看这些是否'真增量'还是'噪声'。")


if __name__ == '__main__':
    main()
