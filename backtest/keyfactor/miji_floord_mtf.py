# -*- coding: utf-8 -*-
"""floord 隔日(波段)可成交回测 + 多时间框架共振(MTF resonance)降噪。

方向一落地: 用 5m / 15m 更高周期 floord 因果摆点, 与 1m 摆点做同向共振门控, 过滤 5-30分 HFT 噪声桶。

信号: 复用 floord_pivot 因果 K-bar 摆点(W=K=5, GAP=3) + miji_alpha 指标。
  - 1m 摆点: 现有战场信号(确认bar下一bar收盘执行)。
  - 高周期摆点: 把当日 1m 聚合成 5m / 15m 连续 K 线(每 tf 个 1m 一根, 跨午休不串根),
    在同阈值/同 basis 下跑 pivot_signals, 得到"高周期买/卖摆点可见的 1m 全局索引"。
  - 共振门控: 1m 买信号在 exec bar i 执行前, 要求存在"同向"高周期摆点, 其可见 1m 索引 j<=i 且 i-j<=LOOKBACK_1m。
    即: 只在更高周期也已转多时, 才允许 1m 抄底; 反之过滤(典型 HFT 逆势假突破)。

前视规避(铁律): 高周期摆点的可见索引严格用 (idx+1)*tf 映射, 与 1m 自身 exec=idx+1 平行 ->
  高周期摆点在 1m bar i 处"可见"当且仅当它在真实时间上已确认, 绝不引用未来。

战场/成本/出场同 miji_floord_eval.py:
  战场=5-∞ 分钟; 个股(T+1)最早次日卖 / ETF(T+0)可同日; 最小持有5min; 保护止损1.5%; 最长3日强制平。
  成本: 个股 买0.05%/卖0.10%(含印花税); ETF 买0.05%/卖0.05%。

变体:
  V1    = 无门控(基线, 复现 prior V1)
  V15   = MTF 门控 [15m]        lookback 240(4h)
  V5    = MTF 门控 [5m]         lookback 120(2h)
  Vboth = MTF 门控 [5m OR 15m]  lookback 同上

输出: output/miji_floord_mtf/{metrics.json, report.html, pf_by_bucket.png, filtered.csv}
"""
import os
import sys
import json
import bisect
import collections
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)  # 命名空间包: 解析 miji_alpha -> backtest.keyfactor._gate_floor

import miji_alpha as MA
from pivot_walkforward_p0 import (pivot_signals, W, K, GAP, SYMS,
                                  _load_sym, load_day_for, all_dates)

TICK_DIR = os.path.join(ROOT, 'data', 'tick_cache')
OUT = os.path.join(ROOT, 'output', 'miji_floord_mtf')
os.makedirs(OUT, exist_ok=True)

STOP_PCT = 1.5        # 保护止损(%) 隔日swing用较宽
MIN_HOLD = 5          # 最小持有 5 分钟, 抑制1-5min即时反转
MAX_HOLD_BARS = 720   # 最长持有 ≈3交易日(240/日), 强制平仓

# MTF 共振参数
TF_LISTS = {
    'V15':   ([15], 240),
    'V5':    ([5], 120),
    'Vboth': ([5, 15], 240),   # OR: 任一高周期共振即通过
}

# 成本(c): (买单边, 卖单边) 已含佣金+滑点(+个股印花税)
COST = {
    'bidirectional': (0.0005, 0.0010),  # 个股
    'longonly':      (0.0005, 0.0005),  # ETF/LOF
}

CONFIGS = {
    'P0-A+B': (0.4, 'extreme'),    # floord 摆点代表 (重点)
    'baseline': (1.0, 'close'),    # 对照
}

# 持有桶(5-∞ 战场, 含跨夜)
BUCKETS = [
    ('5-30m', 5, 30),
    ('30-120m', 30, 120),
    ('120-240m 日内长swing', 120, 240),
    ('1日(240-480m)', 240, 480),
    ('2日+(480m+)', 480, 10 ** 9),
]


def get_common():
    return sorted(set.intersection(*[set(all_dates(s)) for s, _, _ in SYMS]))


def aggregate_tf(day_1m, tf):
    """把当日 1m(连续索引, 跨午休不串根)聚合成 tf 分钟 K 线。每 tf 个 1m 一根。"""
    n = len(day_1m)
    nb = (n + tf - 1) // tf
    rows = []
    for k in range(nb):
        s = day_1m.iloc[k * tf:(k + 1) * tf]
        if len(s) == 0:
            continue
        rows.append({
            'open': float(s['open'].iloc[0]),
            'high': float(s['high'].max()),
            'low': float(s['low'].min()),
            'close': float(s['close'].iloc[-1]),
            'volume': float(s['volume'].sum()),
            'trade_time': str(s['trade_time'].iloc[-1]),
        })
    return pd.DataFrame(rows)


def build_symbol_series_mtf(sym, common, thr, basis, tfs):
    """跨日拼接 1m, 同时产出各高周期摆点的全局可见 1m 索引。

    返回 dict:
      o,h,lo,c,v,day,tt : 1m 长线数组
      signals           : 1m 摆点执行指令(exec_bar, action)
      htf               : {tf: {'buy':[global_1m_idx...], 'sell':[...]}}  (已按 global 索引升序)
      n                 : 总 1m 根数
    """
    o_l, h_l, lo_l, c_l, v_l, day_l, tt_l = [], [], [], [], [], [], []
    signals = []
    htf = {tf: {'buy': [], 'sell': []} for tf in tfs}
    base = 0
    for date in common:
        day, pc = load_day_for(sym, date)
        if len(day) < W + K + 1:
            continue
        n = len(day)
        o_l.extend(day['open'].values.astype(float))
        h_l.extend(day['high'].values.astype(float))
        lo_l.extend(day['low'].values.astype(float))
        c_l.extend(day['close'].values.astype(float))
        v_l.extend(day['volume'].values.astype(float))
        day_l.extend([str(date)] * n)
        tt_l.extend(list(day['trade_time'].values.astype(str)))
        # 1m 摆点
        buys, sells = pivot_signals(day, pc, thr, basis)
        for e in buys:
            signals.append({'exec_bar': base + e['idx'] + 1, 'action': 'enter'})
        for e in sells:
            signals.append({'exec_bar': base + e['idx'] + 1, 'action': 'exit'})
        # 高周期摆点 -> 全局可见 1m 索引
        for tf in tfs:
            agg = aggregate_tf(day, tf)
            if len(agg) < W + K + 1:
                continue
            hb, hs = pivot_signals(agg, pc, thr, basis)
            for e in hb:
                j = base + (e['idx'] + 1) * tf     # 平行 1m exec = idx+1
                j = min(j, base + n - 1)
                htf[tf]['buy'].append(j)
            for e in hs:
                j = base + (e['idx'] + 1) * tf
                j = min(j, base + n - 1)
                htf[tf]['sell'].append(j)
        base += n
    N = base
    signals = [s for s in signals if 0 <= s['exec_bar'] < N]
    for tf in tfs:
        htf[tf]['buy'].sort()
        htf[tf]['sell'].sort()
    return dict(o=np.array(o_l), h=np.array(h_l), lo=np.array(lo_l),
                c=np.array(c_l), v=np.array(v_l),
                day=np.array(day_l), tt=np.array(tt_l),
                signals=signals, htf=htf, n=N)


def passes_resonance(action, i, htf, tf_list, lookback):
    """1m 买(action='enter')需存在同向高周期买摆点可见索引 j<=i 且 i-j<=lookback。"""
    need = 'buy' if action == 'enter' else 'sell'
    for tf in tf_list:
        arr = htf[tf][need]
        pos = bisect.bisect_right(arr, i) - 1
        if pos >= 0 and (i - arr[pos]) <= lookback:
            return True
    return False


def simulate_overnight(series, model, gate, tf_list, lookback,
                       min_hold, stop_pct, max_hold_bars, cost):
    """隔日(波段)重放 + 可选 MTF 共振门控。返回 (trips, leftover, filtered_n)。"""
    c = series['c']
    lo = series['lo']
    day = series['day']
    n = series['n']
    htf = series['htf']
    cost_buy, cost_sell = cost
    orders_exec = [[] for _ in range(n)]
    for sig in series['signals']:
        eb = sig['exec_bar']
        if 0 <= eb < n:
            orders_exec[eb].append(sig)
    pos = None
    trips = []
    filtered_n = 0
    for i in range(n):
        # 1) 保护止损
        if pos is not None:
            if lo[i] <= pos['entry_raw'] * (1 - stop_pct):
                exit_px = pos['entry_raw'] * (1 - stop_pct) * (1 - cost_sell)
                pnl = (exit_px - pos['entry']) / pos['entry'] * 100.0
                trips.append({'side': 'long', 'entry_bar': pos['entry_bar'],
                              'exit_bar': i, 'hold': i - pos['entry_bar'],
                              'pnl': float(pnl), 'reason': 'stop',
                              'entry_date': pos['entry_day']})
                pos = None
                continue
        # 2) 本bar执行指令
        for od in orders_exec[i]:
            if od['action'] == 'enter':
                if pos is None:
                    if gate and not passes_resonance('enter', i, htf, tf_list, lookback):
                        filtered_n += 1
                        continue
                    entry_raw = c[i]
                    entry = entry_raw * (1 + cost_buy)
                    pos = {'entry': entry, 'entry_raw': entry_raw,
                           'entry_bar': i, 'entry_day': day[i]}
            else:  # exit
                if pos is not None:
                    if model == 'bidirectional' and day[i] == pos['entry_day']:
                        continue
                    if (i - pos['entry_bar']) < min_hold:
                        continue
                    exit_px = c[i] * (1 - cost_sell)
                    pnl = (exit_px - pos['entry']) / pos['entry'] * 100.0
                    trips.append({'side': 'long', 'entry_bar': pos['entry_bar'],
                                  'exit_bar': i, 'hold': i - pos['entry_bar'],
                                  'pnl': float(pnl), 'reason': 'signal',
                                  'entry_date': pos['entry_day']})
                    pos = None
        # 3) 最长持有强制平仓
        if pos is not None and (i - pos['entry_bar']) >= max_hold_bars:
            exit_px = c[i] * (1 - cost_sell)
            pnl = (exit_px - pos['entry']) / pos['entry'] * 100.0
            trips.append({'side': 'long', 'entry_bar': pos['entry_bar'],
                          'exit_bar': i, 'hold': i - pos['entry_bar'],
                          'pnl': float(pnl), 'reason': 'max_hold',
                          'entry_date': pos['entry_day']})
            pos = None
    leftover = 1 if pos is not None else 0
    return trips, leftover, filtered_n


def agg_trips(trips):
    if not trips:
        return dict(trades=0, win_rate=None, net_pct=0.0, pf=0.0,
                    avg_win=None, avg_loss=None, avg_hold=None, sharpe=None)
    wins = [t for t in trips if t['pnl'] > 0]
    losses = [t for t in trips if t['pnl'] < 0]
    gw = sum(t['pnl'] for t in wins)
    gl = -sum(t['pnl'] for t in losses)
    pf = (gw / gl) if gl > 0 else (float('inf') if gw > 0 else 0.0)
    net = sum(t['pnl'] for t in trips)
    pnls = np.array([t['pnl'] for t in trips])
    sharpe = float(pnls.mean() / pnls.std()) if pnls.std() > 0 else None
    return dict(trades=len(trips), win_rate=len(wins) / len(trips) * 100.0,
                net_pct=net, pf=pf,
                avg_win=gw / len(wins) if wins else None,
                avg_loss=gl / len(losses) if losses else None,
                avg_hold=float(np.mean([t['hold'] for t in trips])),
                sharpe=sharpe)


def bucket_agg(trips, buckets=BUCKETS):
    out = {}
    for label, lo, hi in buckets:
        bt = [t for t in trips if lo < t['hold'] <= hi]
        if not bt:
            out[label] = dict(n=0, win_rate=None, net_pct=0.0, pf=0.0, avg_hold=None)
            continue
        wins = [t for t in bt if t['pnl'] > 0]
        losses = [t for t in bt if t['pnl'] < 0]
        gw = sum(t['pnl'] for t in wins)
        gl = -sum(t['pnl'] for t in losses)
        pf = (gw / gl) if gl > 0 else (float('inf') if gw > 0 else 0.0)
        out[label] = dict(n=len(bt),
                          win_rate=len(wins) / len(bt) * 100.0,
                          net_pct=sum(t['pnl'] for t in bt), pf=pf,
                          avg_hold=float(np.mean([t['hold'] for t in bt])))
    return out


def main():
    common = get_common()
    print(f"共同交易日数: {len(common)} ({common[0]}..{common[-1]})")

    # 变体: 名称 -> (gate_on, tf_list, lookback)
    variants = {'V1': (False, [], 0)}
    for vname, (tf_list, lb) in TF_LISTS.items():
        variants[vname] = (True, tf_list, lb)

    results = {}        # cfg -> variant -> sym -> {agg, buckets, left, filtered, is, oos}
    pool_accum = {f'{cfg}/{v}': []
                  for cfg in CONFIGS for v in variants}
    pool_filtered = {f'{cfg}/{v}': 0
                     for cfg in CONFIGS for v in variants}
    focused_trades = []   # V15 / P0-A+B 成交明细(供自检透明度)
    mid = len(common) // 2
    early_dates = set(common[:mid])
    late_dates = set(common[mid:])

    for cfg, (thr, basis) in CONFIGS.items():
        results[cfg] = {v: {} for v in variants}
        for sym, name, model in SYMS:
            tfs = sorted({tf for _, (tl, _) in TF_LISTS.items() for tf in tl})
            series = build_symbol_series_mtf(sym, common, thr, basis, tfs)
            cost = COST[model]
            for vname, (gate, tf_list, lb) in variants.items():
                trips, leftover, filtered_n = simulate_overnight(
                    series, model, gate, tf_list, lb, MIN_HOLD,
                    STOP_PCT / 100.0, MAX_HOLD_BARS, cost)
                is_t = [t for t in trips if t['entry_date'] in early_dates]
                oos_t = [t for t in trips if t['entry_date'] in late_dates]
                results[cfg][vname][sym] = {
                    'model': model, 'agg': agg_trips(trips),
                    'buckets': bucket_agg(trips),
                    'leftover': leftover, 'filtered': filtered_n,
                    'is': agg_trips(is_t), 'oos': agg_trips(oos_t),
                }
                pool_accum[f'{cfg}/{vname}'].extend(trips)
                pool_filtered[f'{cfg}/{vname}'] += filtered_n
                if cfg == 'P0-A+B' and vname == 'V15':
                    for t in trips:
                        focused_trades.append({'sym': sym, 'name': name, **t})
                a = results[cfg][vname][sym]['agg']
                wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
                print(f"  [{cfg}/{vname}] {sym:<10} n={len(trips):>4} "
                      f"PF={a['pf']:.2f} net={a['net_pct']:+.1f}% WR={wr} "
                      f"filt={filtered_n}")

    pooled = {key: {'agg': agg_trips(ts), 'buckets': bucket_agg(ts)}
              for key, ts in pool_accum.items()}

    # trades.csv (V15 / P0-A+B) —— 供自检前/后5行与透明度
    if focused_trades:
        import csv as _csv
        fpath = os.path.join(OUT, 'trades.csv')
        cols = ['sym', 'name', 'side', 'entry_bar', 'exit_bar', 'hold',
                'pnl', 'reason', 'entry_date']
        with open(fpath, 'w', newline='', encoding='utf-8') as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for t in focused_trades:
                w.writerow({c: t.get(c, '') for c in cols})
        print('TRADES ->', fpath, f'({len(focused_trades)} rows)')

    dump = {
        'common': common, 'configs': CONFIGS, 'syms': [(s, n, m) for s, n, m in SYMS],
        'stop_pct': STOP_PCT, 'min_hold': MIN_HOLD, 'max_hold_bars': MAX_HOLD_BARS,
        'tf_lists': TF_LISTS, 'cost': COST,
        'pool_filtered': pool_filtered,
        'results': results, 'pooled': pooled,
    }
    with open(os.path.join(OUT, 'metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=2,
                  default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else o)
    print('\nDONE ->', OUT)

    try:
        _write_report(common, results, pooled, pool_filtered)
    except Exception as e:
        print('REPORT 生成失败:', e)


def _write_report(common, results, pooled, pool_filtered):
    # ---- PF 分桶图 (P0-A+B: V1 vs V15 vs Vboth) ----
    # matplotlib 为可选依赖: 缺失时跳过图表, 报告其余部分照常生成
    chart_path = None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        FONT = r'C:/Windows/Fonts/simhei.ttf'
        if os.path.exists(FONT):
            fm.fontManager.addfont(FONT)
            plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
        plt.rcParams['axes.unicode_minus'] = False

        labels = [b[0].split(' ')[0] for b in BUCKETS]
        cfgs_plot = [('P0-A+B', 'V1', '#888888'),
                     ('P0-A+B', 'V15', '#4c72b0'),
                     ('P0-A+B', 'Vboth', '#dd8452')]
        x = np.arange(len(labels))
        w = 0.26
        fig, ax = plt.subplots(figsize=(10, 4.5))
        for i, (cfg, variant, color) in enumerate(cfgs_plot):
            vals = []
            for b in BUCKETS:
                pf = pooled[f'{cfg}/{variant}']['buckets'][b[0]]['pf']
                vals.append(pf if pf != float('inf') else 0.0)
            ax.bar(x + (i - 1) * w, vals, w, label=f'{cfg}/{variant}', color=color)
        ax.axhline(1.0, color='red', ls='--', lw=1, label='PF=1 (盈亏线)')
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylabel('盈利因子 PF')
        ax.set_title('floord 隔日回测 + MTF共振: 持有周期分桶 (V1 无门控 vs MTF门控)')
        ax.legend()
        fig.tight_layout()
        chart_path = os.path.join(OUT, 'pf_by_bucket.png')
        fig.savefig(chart_path, dpi=110)
        plt.close(fig)
    except Exception as e:
        print('CHART 跳过(matplotlib 不可用或失败):', e)
        chart_path = None

    def fmt(x, nd=2, pct=False):
        if x is None:
            return '-'
        if x == float('inf'):
            return 'inf'
        return (f'{x:+.2f}%' if pct else f'{x:.{nd}f}')

    variant_order = ['V1', 'V15', 'V5', 'Vboth']

    # 池化表
    pool_rows = ''
    for cfg in CONFIGS:
        for v in variant_order:
            key = f'{cfg}/{v}'
            if key not in pooled:
                continue
            a = pooled[key]['agg']
            pf_s = 'inf' if a['pf'] == float('inf') else f"{a['pf']:.2f}"
            wr = f"{a['win_rate']:.1f}%" if a['win_rate'] is not None else '-'
            sh = f"{a['sharpe']:.2f}" if a['sharpe'] is not None else '-'
            flt = pool_filtered.get(key, 0)
            pool_rows += (f"<tr><td>{key}</td><td>{a['trades']}</td><td>{wr}</td>"
                          f"<td>{fmt(a['net_pct'], pct=True)}</td><td>{pf_s}</td>"
                          f"<td>{sh}</td><td>{fmt(a['avg_hold'], nd=1)}</td><td>{flt}</td></tr>")

    # 分桶表 (P0-A+B)
    bucket_rows = ''
    for label, lo, hi in BUCKETS:
        cells = ''
        for v in variant_order:
            key = f'P0-A+B/{v}'
            if key not in pooled:
                continue
            b = pooled[key]['buckets'][label]
            pf_s = 'inf' if b['pf'] == float('inf') else f"{b['pf']:.2f}"
            wr = f"{b['win_rate']:.0f}%" if b['win_rate'] is not None else '-'
            cls = ' class="pos"' if (b['pf'] != float('inf') and b['pf'] > 1) else ''
            cells += (f"<td{cls}>{b['n']}</td><td{cls}>{wr}</td>"
                      f"<td{cls}>{fmt(b['net_pct'], pct=True)}</td><td{cls}>{pf_s}</td>")
        bucket_rows += f"<tr><td>{label}</td>{cells}</tr>"

    # 分标的表 (P0-A+B: 各变体 PF/胜率/净额/笔数)
    sym_rows = ''
    for sym, name, model in SYMS:
        cells = ''
        for v in variant_order:
            r = results['P0-A+B'][v][sym]['agg']
            pf_s = 'inf' if r['pf'] == float('inf') else f"{r['pf']:.2f}"
            wr = f"{r['win_rate']:.0f}%" if r['win_rate'] is not None else '-'
            flt = results['P0-A+B'][v][sym]['filtered']
            cells += (f"<td>{r['trades']}/{wr}/{fmt(r['net_pct'], pct=True)}/{pf_s}"
                      f"{('/'+str(flt)) if v!='V1' else ''}</td>")
        sym_rows += (f"<tr><td>{sym}</td><td>{name}</td><td>{model}</td>{cells}</tr>")

    # IS/OOS 表 (P0-A+B, 各变体)
    isoos_rows = ''
    for sym, name, model in SYMS:
        cells = ''
        for v in variant_order:
            is_a = results['P0-A+B'][v][sym]['is']
            oos_a = results['P0-A+B'][v][sym]['oos']
            pf_i = 'inf' if is_a['pf'] == float('inf') else f"{is_a['pf']:.2f}"
            pf_o = 'inf' if oos_a['pf'] == float('inf') else f"{oos_a['pf']:.2f}"
            cells += (f"<td>{is_a['trades']}/{fmt(is_a['net_pct'], pct=True)}/{pf_i}<br>"
                      f"{oos_a['trades']}/{fmt(oos_a['net_pct'], pct=True)}/{pf_o}</td>")
        isoos_rows += f"<tr><td>{sym}</td>{cells}</tr>"

    chart_html = f'<img src="pf_by_bucket.png">' if chart_path else '<p>(图表未生成)</p>'

    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>floord 隔日回测 + 多时间框架共振</title>
<style>
body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;margin:24px;color:#222}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}}
th,td{{border:1px solid #ccc;padding:5px 8px;text-align:right}}
th{{background:#f0f0f0}} td:first-child,td:nth-child(2){{text-align:left}}
tr.pos{{background:#e8f5e9}}
.warn{{color:#b00;font-size:13px}} .ok{{color:#070;font-weight:bold}}
img{{max-width:960px}}
footer{{margin-top:30px;color:#888;font-size:12px}}
</style></head><body>
<h1>floord 隔日(波段)可成交回测 + 多时间框架共振(MTF)降噪</h1>
<p>标的=8家族 ({common[0]}~{common[-1]}, {len(common)}日); 信号=floord_pivot因果K-bar摆点(W=K=5,GAP=3) + miji_alpha指标;
战场=5-∞分钟(个股隔日跨夜 / ETF含日内做T); 入场=确认bar下一bar收盘; 止损={STOP_PCT}%; 最长持有=3日; 最小持有={MIN_HOLD}min。</p>
<p>MTF共振: 把当日1m聚合成 5m/15m 连续K线(每tf个1m一根, 跨午休不串根), 同阈值/同basis跑 pivot_signals, 得高周期摆点"可见1m索引"=(idx+1)*tf(与1m exec=idx+1平行, 无前视)。
共振门控: 1m买信号执行前, 要求存在同向高周期摆点 j&lt;=i 且 i-j&lt;=lookback(V15:15m/240, V5:5m/120, Vboth:5m或15m/240)。</p>
<p>成本: 个股 买0.05%/卖0.10%(含印花税), ETF 买0.05%/卖0.05%。</p>
<p class="warn">注: 本回测在分钟级上实现, 属项目 miji 自有信号框架(非通用日级回测); 末日仍持仓记为未实现不计入PF(保守)。</p>

<h2>池化汇总（8标的, 过滤数=被MTF门控拦截的1m买信号数）</h2>
<table><tr><th>变体</th><th>笔数</th><th>胜率</th><th>净额%</th><th>盈利因子PF</th><th>Sharpe</th><th>均持有(分)</th><th>过滤数</th></tr>
{pool_rows}</table>

<h2>P0-A+B 按持有周期分桶（V1 / V15 / V5 / Vboth）</h2>
<table><tr><th>持有桶</th>
<th colspan="4">V1</th><th colspan="4">V15</th><th colspan="4">V5</th><th colspan="4">Vboth</th></tr>
<tr><th></th>
<th>笔</th><th>胜</th><th>净</th><th>PF</th>
<th>笔</th><th>胜</th><th>净</th><th>PF</th>
<th>笔</th><th>胜</th><th>净</th><th>PF</th>
<th>笔</th><th>胜</th><th>净</th><th>PF</th></tr>
{bucket_rows}</table>

<h2>分标的（P0-A+B: 各变体 = 笔数/胜率/净额%/PF，MTF变体尾部/F=被过滤数）</h2>
<table><tr><th>标的</th><th>名称</th><th>模型</th>
<th>V1</th><th>V15</th><th>V5</th><th>Vboth</th></tr>
{sym_rows}</table>

<h2>P0-A+B 稳定性 IS(前半)/OOS(后半)（每格: IS笔/净/PF 换行 OOS笔/净/PF）</h2>
<table><tr><th>标的</th><th>V1</th><th>V15</th><th>V5</th><th>Vboth</th></tr>
{isoos_rows}</table>

<h2>PF 分桶图</h2>
{chart_html}

<h2>判读要点</h2>
<ul>
<li><b>V1 池化PF&lt;1</b>: 5-30分 HFT 噪声桶(PF~0.37)拖累全局 → 需降噪。</li>
<li><b>MTF门控目标</b>: 过滤掉逆势/无高周期共振的1m假信号, 保留与高周期同向的 swing 信号, 抬升池化PF。</li>
<li>前视铁律: 高周期摆点可见索引=(idx+1)*tf, 严格因果, 不引用未来。</li>
</ul>
<footer>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</footer>
</body></html>"""
    with open(os.path.join(OUT, 'report.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print('REPORT ->', os.path.join(OUT, 'report.html'))


if __name__ == '__main__':
    main()
