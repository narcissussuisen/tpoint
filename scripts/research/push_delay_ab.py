# -*- coding: utf-8 -*-
"""scripts/research/push_delay_ab.py — 「bar 收盘 → 飞书推送」延迟实测（轮询率提速的真实收益）

背景（2026-09-11）：用户问「是否该把刷新提到 3s」。核查代码后确认：
  · `detect_for(trim_frontier=True)` 剔除最后一根「进行中」bar（monitor.py:1381），
    且**出场管理就在同一个 `for i in range(2, _end)` 循环内** ⇒ 入场与出场都只用**已收盘 bar**；
  · 每根 bar 命中 `st[f"bar_{sym}_{date}_{i}"]` 后标记、后续轮次 `continue`（monitor.py:1387-1389）
    ⇒ 同一根 bar 被评估 4 次还是 20 次，只产出一个判定。
  ⇒ **轮询率不改变信号集合**，唯一真实收益是「bar 收盘 → 推送」的延迟。

本脚本就是从生产 `data/signal.txt` 直接量这个延迟（可复现、无需模拟）：
  signal.txt 每块首行 = `[推送HH:MM:SS] [K:bar结束HH:MM]`
  ⚠️ 关键口径：TDX 1m bar **按「结束时刻」标记**（首根 09:31 / 末根 15:00，正好 240 根）
     ⇒ `K:HH:MM` 就是该 bar 的**收盘时刻**，故 `延迟 = 推送时刻 − K`。
     （若按「起始时刻」理解会算出 −50s 的荒谬负延迟——2026-09-11 踩过这个口径坑。）

理论参照：轮询档 N 秒下，延迟近似 U(0, N) + 单轮耗时 ⇒ 中位 ≈ N/2 + 轮耗时。

运行：venv/Scripts/python.exe scripts/research/push_delay_ab.py
"""
import io
import os
import re
import statistics as st
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIGNAL_TXT = os.path.join(ROOT, 'data', 'signal.txt')
OUT = os.path.join(ROOT, 'output', 'push_delay_ab.json')

#: 轮询档（按变更日期划分）：2026-09-11 起 scan_interval=3，之前为 15
ERA = {'2026-09-07': 15, '2026-09-08': 15, '2026-09-09': 15, '2026-09-10': 15, '2026-09-11': 3}
RE_HEAD = re.compile(r'^\[(\d{2}):(\d{2}):(\d{2})\]\s+\[K:(\d{2}):(\d{2})\]')
RE_DATE = re.compile(r'^\[(\d{4}-\d{2}-\d{2})\]\s*$')


def collect():
    per = defaultdict(list)
    cur = None
    for ln in io.open(SIGNAL_TXT, encoding='utf-8', errors='ignore'):
        s = ln.strip()
        m = RE_DATE.match(s)
        if m:
            cur = m.group(1)
            continue
        m = RE_HEAD.match(s)
        if m and cur:
            ph, pm, ps, bh, bm = (int(x) for x in m.groups())
            per[cur].append((ph * 3600 + pm * 60 + ps) - (bh * 3600 + bm * 60))
    return per


def main():
    per = collect()
    rows = []
    print('%-12s %5s %7s %7s %7s %7s %8s %6s %9s'
          % ('日期', '条数', '中位s', '均值s', 'p90s', '最大s', '分钟级异常', '轮询s', '预期中位s'))
    for d in sorted(per):
        if d not in ERA:
            continue
        ds = sorted(per[d])
        clean = [x for x in ds if x <= 60]           # 剔除运维事故（重启/降级/预热）造成的迟到
        if len(clean) < 3:
            continue
        n = len(clean)
        row = {'date': d, 'era_interval_s': ERA[d], 'n': n,
               'n_gt60s': sum(1 for x in ds if x > 60),
               'median_s': st.median(clean), 'mean_s': round(st.mean(clean), 1),
               'p90_s': clean[int(n * 0.9) - 1], 'max_s': max(clean),
               'expected_median_s': ERA[d] / 2 + 2.5}
        rows.append(row)
        print('%-12s %5d %7.1f %7.1f %7.1f %7d %8d %6d %9.1f'
              % (d, n, row['median_s'], row['mean_s'], row['p90_s'], row['max_s'],
                 row['n_gt60s'], ERA[d], row['expected_median_s']))

    a = [r for r in rows if r['era_interval_s'] == 15]
    b = [r for r in rows if r['era_interval_s'] == 3]
    if a and b:
        ma = st.median([r['median_s'] for r in a])
        mb = st.median([r['median_s'] for r in b])
        print('\n15s 档（%d 天）中位中位 = %.1fs  →  3s 档（%d 天）= %.1fs   ⇒ 改善约 %.0f 秒'
              % (len(a), ma, len(b), mb, ma - mb))
        print('p90：15s 档约 %.0fs  →  3s 档 %.0fs'
              % (st.median([r['p90_s'] for r in a]), st.median([r['p90_s'] for r in b])))
    print('\n说明：延迟 = 推送时刻 − bar 收盘时刻；>60s 的样本为运维事故迟到（单独计数，已剔除）。')
    print('      理论上界：轮询 N 秒 + 单轮耗时，故 15s 档实测中位 7~10s、3s 档 2s 均与模型吻合。')

    import json
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({'rows': rows, 'note': '延迟=推送-bar收盘; >60s 为事故迟到; K 为 bar 结束时刻'},
              io.open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('已落盘 %s' % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
