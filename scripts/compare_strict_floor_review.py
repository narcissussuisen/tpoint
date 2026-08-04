#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_strict_floor_review.py — 用"今日实际1m走势"对比 strict(生产默认) 与 floor(拟flip) 信号表现。

数据来源(只读):
  - strict 信号: logs/morning_review_2026-07-20.html 第三节明细表(生产引擎 MACD_GATE_MODE=strict, 早盘09:30-11:30)
  - floor  信号: output/161129.SZ_floor_2026-07-20.csv + output/688347.SH_floor_2026-07-20.csv (本会话跑的隔离floor)
  - 实际走势: core/datasource.MootdxDataSource().klines.intraday (今日1m, 仅用于量前向收益, 不重跑信号引擎)

方法(一致口径): 每个信号按其 trade_time 定位到当日1m棒 idx, 前向收益 = c[idx+k]/c[idx]-1 (k=6/12/24), 严格因果(只看信号之后).
输出: output/compare_strict_floor_2026-07-20.csv (逐信号) + output/compare_strict_floor_summary.json (聚合)
"""
import os
import re
import json
import sys
import csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

from core.datasource import MootdxDataSource  # noqa: E402

SYM_NAME = {'161129.SZ': '原油LOF易方达', '688347.SH': '华虹宏力'}
HTML_PATH = os.path.join(ROOT, 'logs', 'morning_review_2026-07-20.html')
FLOOR_CSV = {
    '161129.SZ': os.path.join(ROOT, 'output', '161129.SZ_floor_2026-07-20.csv'),
    '688347.SH': os.path.join(ROOT, 'output', '688347.SH_floor_2026-07-20.csv'),
}
HORIZONS = [6, 12, 24]


def fetch_bars(sym):
    tf = MootdxDataSource()
    df = tf.klines.intraday(sym)
    df = df.sort_values('trade_time').reset_index(drop=True)
    return df


def parse_strict_from_html():
    """解析早盘复盘 HTML 第三节信号明细表 -> list of dict."""
    html = open(HTML_PATH, encoding='utf-8').read()
    m = re.search(r'三、信号明细.*?</table>', html, re.S)
    if not m:
        return []
    tbl = m.group(0)
    rows = re.findall(r'<tr>(.*?)</tr>', tbl, re.S)
    out = []
    for r in rows[1:]:  # 跳过表头
        cells = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        if len(cells) < 9:
            continue
        t = re.sub(r'<[^>]+>', '', cells[0]).strip()
        d = re.sub(r'<[^>]+>', '', cells[1]).strip()  # 'B' / 'S'
        name = re.sub(r'<[^>]+>', '', cells[2]).strip()
        price = float(re.sub(r'<[^>]+>', '', cells[3]).strip())
        fwd = []
        for k in (6, 7, 8):  # 6min/12min/24min 列
            v = re.sub(r'<[^>]+>', '', cells[k]).replace('%', '').strip()
            fwd.append(float(v) if v not in ('—', '') else None)
        sym = next((s for s, n in SYM_NAME.items() if n == name), None)
        if sym is None:
            continue
        out.append({'sym': sym, 'time': t, 'dir': d, 'price': price,
                    'fwd6': fwd[0], 'fwd12': fwd[1], 'fwd24': fwd[2], 'mode': 'strict'})
    return out


def load_floor_csv(sym):
    path = FLOOR_CSV[sym]
    out = []
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            out.append({'sym': sym, 'time': row['trade_time'][11:19], 'dir': 'B' if row['direction'] == '买入' else 'S',
                        'price': float(row['price']), 'mode': 'floor'})
    return out


def compute_fwd_from_bars(sig, df):
    """用实际1m棒重算前向收益(因果, 只看信号之后). 返回 (fwd6,fwd12,fwd24) 百分比或 None."""
    times = df['trade_time'].astype(str).tolist()
    c = df['close'].values.astype(float)
    # 匹配 idx: floor 用 HH:MM:SS, strict 用 HH:MM
    target = sig['time']
    idx = None
    for i, tt in enumerate(times):
        tt_hms = tt[11:19] if len(tt) > 8 else tt
        if tt_hms == target or tt_hms[:5] == target[:5]:
            idx = i
            break
    if idx is None:
        return None, None, None
    res = []
    for k in HORIZONS:
        j = idx + k
        if j < len(c):
            res.append((c[j] / c[idx] - 1) * 100.0)
        else:
            res.append(None)
    return tuple(res)


def aggregate(sigs):
    nB = sum(1 for s in sigs if s['dir'] == 'B')
    nS = sum(1 for s in sigs if s['dir'] == 'S')
    def acc(dirn, sign_ok):
        sub = [s for s in sigs if s['dir'] == dirn and s['fwd12_calc'] is not None]
        if not sub:
            return None, None
        ok = sum(1 for s in sub if sign_ok(s['fwd12_calc']))
        mean = sum(s['fwd12_calc'] for s in sub) / len(sub)
        return ok / len(sub), mean
    b_acc, b_mean = acc('B', lambda x: x > 0)
    s_acc, s_mean = acc('S', lambda x: x < 0)
    # 每信号平均绝对值前向(12min) 作为"信号强度"
    all12 = [s['fwd12_calc'] for s in sigs if s['fwd12_calc'] is not None]
    mean_abs12 = (sum(abs(x) for x in all12) / len(all12)) if all12 else None
    return {
        'n_signals': len(sigs), 'nB': nB, 'nS': nS,
        'B_acc12': b_acc, 'B_mean_fwd12': b_mean,
        'S_acc12': s_acc, 'S_mean_fwd12': s_mean,
        'mean_abs_fwd12': mean_abs12,
    }


def main():
    bars = {sym: fetch_bars(sym) for sym in SYM_NAME}

    strict = parse_strict_from_html()
    floor = []
    for sym in SYM_NAME:
        floor += load_floor_csv(sym)

    # 重算前向收益(统一从实际棒)
    for s in strict + floor:
        f6, f12, f24 = compute_fwd_from_bars(s, bars[s['sym']])
        s['fwd6_calc'], s['fwd12_calc'], s['fwd24_calc'] = f6, f12, f24

    # 逐信号 CSV
    out_dir = os.path.join(ROOT, 'output')
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, 'compare_strict_floor_2026-07-20.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['mode', 'sym', 'name', 'time', 'dir', 'price',
                    'fwd6_recalc%', 'fwd12_recalc%', 'fwd24_recalc%'])
        for s in strict + floor:
            w.writerow([s['mode'], s['sym'], SYM_NAME[s['sym']], s['time'], s['dir'],
                        f"{s['price']:.3f}",
                        f"{s['fwd6_calc']:.2f}" if s['fwd6_calc'] is not None else '',
                        f"{s['fwd12_calc']:.2f}" if s['fwd12_calc'] is not None else '',
                        f"{s['fwd24_calc']:.2f}" if s['fwd24_calc'] is not None else ''])

    # 聚合
    summary = {'by_mode': {}, 'by_mode_sym': {}}
    for mode in ('strict', 'floor'):
        msigs = [s for s in strict + floor if s['mode'] == mode]
        summary['by_mode'][mode] = aggregate(msigs)
        for sym in SYM_NAME:
            ss = [s for s in msigs if s['sym'] == sym]
            summary['by_mode_sym'][f'{mode}_{sym}'] = aggregate(ss)

    summary['meta'] = {
        'strict_coverage': '早盘 09:30-11:30 (来自 morning_review, 生产 strict)',
        'floor_coverage': '全日 09:30-15:00 (来自 floor_signals_today.py, 隔离 floor)',
        'fwd_method': 'c[idx+k]/c[idx]-1, k=6/12/24, 仅用信号之后棒(因果)',
        'bars_161129': len(bars['161129.SZ']),
        'bars_688347': len(bars['688347.SH']),
    }
    json_path = os.path.join(out_dir, 'compare_strict_floor_summary.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 控制台
    print('=' * 78)
    print('strict vs floor — 今日实际走势对比 (前向收益口径一致)')
    print('=' * 78)
    for mode in ('strict', 'floor'):
        a = summary['by_mode'][mode]
        print(f'\n[{mode}] 总信号={a["n_signals"]} (B{a["nB"]}/S{a["nS"]}) | '
              f'覆盖={"早盘" if mode=="strict" else "全日"}')
        print(f'  B准确率@12min={a["B_acc12"]}  均B前收@12min={a["B_mean_fwd12"]}')
        print(f'  S准确率@12min={a["S_acc12"]}  均S前收@12min={a["S_mean_fwd12"]}')
        print(f'  每信号|前收|@12min均值={a["mean_abs_fwd12"]}')
    print('\nCSV :', csv_path)
    print('JSON:', json_path)


if __name__ == '__main__':
    main()
