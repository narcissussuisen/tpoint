# -*- coding: utf-8 -*-
"""按交易日切分 monitor_console.log（GBK），统计每日扫描健康度。

诊断目标（2026-08-12）：确认 08-11 / 08-12 盘中是否持续 "no intraday data"
（即数据层拿到 bar 但 compute() 返回空 → 整轮标的从未进入 detect_for）。

用法：python scripts/diag_log_days.py [尾部行数]
输出：logs/diag_log_days.txt（UTF-8）
"""
import re
import sys
import os
import collections

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(BASE, 'logs', 'monitor_console.log')
OUT = os.path.join(BASE, 'logs', 'diag_log_days.txt')

_rb = open(LOG, 'rb').read()
# monitor 写日志统一 encoding='utf-8'（08-03 修复），但历史段可能含 GBK 行；
# 先整体 utf-8 解码，失败字节以 replace 兜底（逐行 fallback 成本过高且不影响关键词匹配）。
raw = _rb.decode('utf-8', errors='replace')
lines = raw.split('\n')
tail = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
start = max(0, len(lines) - tail)

buf = []


def w(s=''):
    buf.append(str(s))


w('total lines=%d, analyzing from %d' % (len(lines), start + 1))
w('')

# 用日期锚点（形如 2026-08-11T09:34:00 / 2026-08-11）划分区段
anchors = []
for i in range(start, len(lines)):
    m = re.search(r'2026-(\d\d)-(\d\d)', lines[i])
    if m:
        anchors.append((i, '2026-%s-%s' % (m.group(1), m.group(2))))

w('--- date anchors (last 40) ---')
for i, d in anchors[-40:]:
    w('%6d %s | %s' % (i + 1, d, re.sub(r'\s+', ' ', lines[i])[:110]))
w('')

# 逐行扫描，用 [HH:MM:SS] 时间戳 + "日K已刷新" 作为交易日开始标记
# 每次出现 "日K已刷新" 视为新的一天开盘前刷新
day_blocks = []  # (start_idx, label)
for i in range(start, len(lines)):
    if '日K已刷新' in lines[i]:
        day_blocks.append(i)

w('--- 日K已刷新 markers (新交易日开始) ---')
for i in day_blocks:
    w('%6d | %s' % (i + 1, re.sub(r'\s+', ' ', lines[i])[:130]))
w('')

# 对最后 3 个 day block 做健康度统计
def hhmm(line):
    m = re.search(r'\[(\d\d):(\d\d):(\d\d)\]', line)
    return m.group(1) + ':' + m.group(2) if m else None


bounds = day_blocks + [len(lines)]
for bi in range(max(0, len(day_blocks) - 3), len(day_blocks)):
    s = bounds[bi]
    e = bounds[bi + 1] if bi + 1 < len(bounds) else len(lines)
    w('=' * 70)
    w('BLOCK %d: lines %d..%d  head=%s' % (bi, s + 1, e, re.sub(r'\s+', ' ', lines[s])[:120]))
    stats = collections.Counter()
    noidata_times = []   # (hhmm, name)
    scan_times = []
    sig_lines = []
    cur_t = None
    for i in range(s, e):
        L = lines[i]
        t = hhmm(L)
        if t:
            cur_t = t
        if 'no intraday data' in L:
            stats['no_intraday'] += 1
            nm = L.split(']')[-1].replace('no intraday data', '').strip()
            noidata_times.append((cur_t, nm))
        if '本轮无信号' in L:
            stats['scan_nosig'] += 1
            if t:
                scan_times.append(t)
        if 'compute exception' in L:
            stats['compute_exc'] += 1
            sig_lines.append((i + 1, re.sub(r'\s+', ' ', L)[:140]))
        if 'process exception' in L:
            stats['process_exc'] += 1
            sig_lines.append((i + 1, re.sub(r'\s+', ' ', L)[:140]))
        if '新浪1m兜底成功' in L:
            stats['sina_ok'] += 1
        if 'B@' in L or 'S@' in L or '首扫白名单' in L:
            sig_lines.append((i + 1, re.sub(r'\s+', ' ', L)[:140]))
        if '推送' in L and ('成功' in L or '失败' in L):
            sig_lines.append((i + 1, re.sub(r'\s+', ' ', L)[:140]))
    w('stats: %s' % dict(stats))
    if scan_times:
        w('扫描轮时间范围: %s .. %s (共 %d 轮无信号)' % (scan_times[0], scan_times[-1], len(scan_times)))
    if noidata_times:
        # 按小时:分钟统计 no intraday data 的时间分布
        tt = [t for t, _ in noidata_times if t]
        w('no_intraday 时间范围: %s .. %s' % (tt[0] if tt else '?', tt[-1] if tt else '?'))
        # 开盘后（>=09:31）仍报 no intraday 的数量
        after = [(t, n) for t, n in noidata_times if t and t >= '09:31' and t <= '15:00']
        w('*** 开盘后(09:31-15:00) no_intraday 次数 = %d' % len(after))
        if after:
            w('    首条: %s %s   末条: %s %s' % (after[0][0], after[0][1], after[-1][0], after[-1][1]))
            byname = collections.Counter(n for _, n in after)
            w('    按标的: %s' % dict(byname))
    w('--- 关键事件行 ---')
    for ln, txt in sig_lines[:40]:
        w('%6d | %s' % (ln, txt))
    w('')

open(OUT, 'w', encoding='utf-8').write('\n'.join(buf))
print('WROTE', OUT, len(buf), 'lines')
