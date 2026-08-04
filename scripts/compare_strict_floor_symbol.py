#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_strict_floor_symbol.py — 对单只标的在指定交易日，用同一隔离分析引擎
分别以 strict(生产默认) 与 floor(拟flip) 两种 MACD 门控跑信号，并计算因果前向收益，
输出对比 CSV + summary JSON，供 render_compare_html_symbol.py 渲染报告。

隔离 / 只读 / 零生产依赖:
  - 仅 import 本地隔离引擎 backtest/keyfactor/miji_engine (纯 numpy)
  - 仅读取 core/datasource 的 live 行情 (MootdxDataSource.intraday)
  - 不调用生产 monitor/推送, 不写任何生产配置/持仓文件

因果约束 (禁用后视镜):
  - 只喂"当日已收盘的分钟棒" (intraday 自动按 trade_date==date 过滤, 不含进行中那根)
  - 单交易日分段: 整段即单日 -> VWAP/EMA/每日上限 按本日封顶
  - pc(前收) 取自前一交易日 1d 收盘
  - 前向收益 = 信号棒收盘 -> +6/12/24 根收盘 (仅用 idx 之后已存在数据, 无未来函数)

用法: python compare_strict_floor_symbol.py <SYM> <DATE>
  例: python compare_strict_floor_symbol.py 159985.SZ 2026-07-20
"""
import os
import sys
import json
import csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))

from core.datasource import MootdxDataSource  # noqa: E402
import miji_engine as ME  # noqa: E402

SYM = sys.argv[1] if len(sys.argv) > 1 else '159985.SZ'
DATE = sys.argv[2] if len(sys.argv) > 2 else '2026-07-20'
MODES = ('strict', 'floor')
FWD_K = (6, 12, 24)


def fwd_ret(c, idx, k):
    j = idx + k
    if j >= len(c):
        return None
    if c[idx] <= 0:
        return None
    return (c[j] - c[idx]) / c[idx] * 100.0


def main():
    tf = MootdxDataSource()

    df = tf.klines.intraday(SYM)
    if df is None or len(df) == 0:
        print(f'❌ 未能取到 {SYM} 行情 (非交易时段 / 网络失败 / 代码错误)。未输出任何信号。')
        sys.exit(1)

    df = df.sort_values('trade_time').reset_index(drop=True)
    n_bars = len(df)
    t_last = str(df['trade_time'].iloc[-1])

    try:
        day_df = tf.klines.get(SYM, '1d', count=2)
        pc = float(day_df['close'].iloc[-2])
    except Exception as e:
        print(f'❌ 取前收失败: {e}')
        sys.exit(1)

    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc)

    out_dir = os.path.join(ROOT, 'output')
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f'{SYM}_strict_floor_{DATE}.csv')
    json_path = os.path.join(out_dir, f'{SYM}_strict_floor_{DATE}_summary.json')

    rows = []
    for mode in MODES:
        sigs = ME.detect_miji_signals(data, pc, macd_gate_mode=mode, enable=(True, True, True))
        for s in sigs:
            idx = s['idx']
            t = str(df['trade_time'].iloc[idx]) if idx < n_bars else '—'
            direction = 'B' if s['type'] == 'B' else 'S'
            f = s.get('factors', {})
            fr = {f'fwd{k}%': fwd_ret(c, idx, k) for k in FWD_K}
            rows.append({
                'mode': mode,
                'sym': SYM,
                'time': t,
                'dir': direction,
                'price': s['price'],
                'resonance_score': s.get('resonance_score'),
                'gravity': f.get('gravity'),
                'vol_div': f.get('vol_div'),
                'macd_div': f.get('macd_div'),
                'day_chg': s.get('chg'),
                'fwd6%': fr['fwd6%'],
                'fwd12%': fr['fwd12%'],
                'fwd24%': fr['fwd24%'],
                'detail': s.get('detail', ''),
            })

    # 写 CSV
    fieldnames = ['mode', 'sym', 'time', 'dir', 'price', 'resonance_score',
                  'gravity', 'vol_div', 'macd_div', 'day_chg',
                  'fwd6%', 'fwd12%', 'fwd24%', 'detail']
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 指标
    def metrics(mode):
        mr = [r for r in rows if r['mode'] == mode]
        b = [r for r in mr if r['dir'] == 'B']
        s = [r for r in mr if r['dir'] == 'S']
        def acc(sigs, want_up):
            vals = [r['fwd12%'] for r in sigs if r['fwd12%'] is not None]
            if not vals:
                return None
            ok = sum(1 for x in vals if (x > 0) == want_up)
            return ok / len(vals) * 100.0
        def mean(sigs):
            vals = [r['fwd12%'] for r in sigs if r['fwd12%'] is not None]
            return (sum(vals) / len(vals)) if vals else None
        def mean_abs():
            vals = [r['fwd12%'] for r in mr if r['fwd12%'] is not None]
            return (sum(abs(x) for x in vals) / len(vals)) if vals else None
        return {
            'n_signals': len(mr), 'nB': len(b), 'nS': len(s),
            'B_acc12': acc(b, True), 'S_acc12': acc(s, False),
            'B_mean_fwd12': mean(b), 'S_mean_fwd12': mean(s),
            'mean_abs_fwd12': mean_abs(),
        }

    summary = {'sym': SYM, 'date': DATE, 'n_bars': n_bars, 'pc': pc,
               'by_mode': {m: metrics(m) for m in MODES}}
    with open(json_path, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    # 控制台汇总
    print('=' * 78)
    print(f'strict vs floor 信号对比 — {SYM}  {DATE}  (数据截止: {t_last}, 共 {n_bars} 根已收盘1m棒)')
    print(f'前收 pc = {pc:.3f} | 引擎: 隔离 miji_engine (与生产同构, 零生产依赖)')
    print('—' * 78)
    for m in MODES:
        ms = summary['by_mode'][m]
        print(f'[{m}] 信号 {ms["n_signals"]} (B{ms["nB"]}/S{ms["nS"]}) | '
              f'B准确率@12m={_f(ms["B_acc12"])} S准确率@12m={_f(ms["S_acc12"])} | '
              f'均B前收={_f(ms["B_mean_fwd12"])} 均S前收={_f(ms["S_mean_fwd12"])}')
    print('—' * 78)
    print(f'CSV : {csv_path}')
    print(f'JSON: {json_path}')
    print('因果自检: 触发价=信号棒收盘; 前向收益=信号棒收盘→+6/12/24棒收盘; pc 取前一日。')


def _f(x):
    return f'{x:.1f}%' if isinstance(x, (int, float)) else '—'


if __name__ == '__main__':
    main()
