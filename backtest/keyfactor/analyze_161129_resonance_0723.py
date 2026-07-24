#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
161129 (原油LOF) 2026-07-23 单日 resonance 信号级审计。

- 信号检测: miji_engine macd_gate_mode='resonance', vol_div 强制开启, min_resonance=2
- 触发明细: 时间 / 类型 / 价格 / 当日涨跌幅 / resonance_score / 三因子参与 / g_dev / detail
- 后续走势验证:
    * 多档前瞻收益 fwd[6,12,24,48,120] (close-to-close, %)
    * B: 后续N分钟内最高价相对入场的最大有利偏移(MFE); S: 最低价相对入场的最大有利偏移
    * v9 真实出场纪律下的配对已实现 P&L (simulate_day: 移动止损0.4/0.6 + 硬止损 + 时间止损 + S出场)
输出:
  output/161129_resonance_20260723_signals.csv
  output/161129_resonance_20260723_trips.csv
  output/161129_resonance_20260723_summary.json
  output/161129_resonance_20260723.png  (价格 + 信号标注)
  output/161129_resonance_20260723_dashboard.html
"""
import sys, os, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))

from test_resonance_v930 import detect_daily, _segment_days, HORIZONS
import kf_utils as K
import miji_engine as ME
from exit_manager import simulate_day, make_config, aggregate_metrics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SYM = '161129.SZ'
DAY = '2026-07-23'
DATA_CSV = r"F:\keyfactor_data\1m\161129.SZ_1m.csv"
OUT = os.path.join('output', '161129_resonance_20260723')
EXTRA_H = [48, 120]
ALL_H = HORIZONS + EXTRA_H

# v9 生产出场配置
CFG = make_config(use_stop=True, stop_atr_mult=1.5, stop_mode='atr',
                  use_time=True, time_stop_bars=90,
                  use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
                  s_signal_exit=True)


def main():
    df = pd.read_csv(DATA_CSV)
    df['trade_date'] = df['trade_date'].astype(str)
    df['trade_time'] = df['trade_time'].astype(str)
    c_all = df['close'].values.astype(float)

    # 全段检测 (resonance), 以保持 prev_close/day 锚定一致
    sigs = detect_daily(df, macd_gate_mode='resonance', vol_div_enabled=True, min_resonance=ME.RESONANCE_THRESHOLD)

    # 定位 2026-07-23 段
    groups = _segment_days(df)
    seg = next((g for g in groups if df['trade_date'].iloc[g[0]] == DAY), None)
    if seg is None:
        print(f"⚠️ 数据里没有 {DAY}")
        return
    gs, ge = seg
    day_sigs = [s for s in sigs if gs <= s['idx'] < ge]

    # ---- 前瞻收益 (全段 close, 含 6/12/24/48/120) ----
    def fwd(idx, h):
        j = idx + h
        if j < len(c_all):
            return round((c_all[j] / c_all[idx] - 1.0) * 100.0, 3)
        return None
    # ---- 最大有利偏移 MFE (未来 N 根内) ----
    h_arr = df['high'].values.astype(float)
    lo_arr = df['low'].values.astype(float)
    def mfe(idx, bars=120, direction=1):
        """direction=1(B): 未来bars内最高价偏移; direction=-1(S): 未来bars内最低价偏移(负值=有利)"""
        end = min(idx + bars, len(c_all))
        if direction == 1:
            peak = np.max(h_arr[idx+1:end]) if end > idx+1 else c_all[idx]
            return round((peak / c_all[idx] - 1.0) * 100.0, 3)
        else:
            trough = np.min(lo_arr[idx+1:end]) if end > idx+1 else c_all[idx]
            return round((c_all[idx] / trough - 1.0) * 100.0, 3)

    rows = []
    for s in day_sigs:
        idx = s['idx']
        price = s['price']
        fac = s['factors']
        r = {
            'time': df['trade_time'].iloc[idx],
            'type': s['type'],
            'price': price,
            'day_chg_pct': s.get('chg'),
            'resonance_score': s['resonance_score'],
            'g': fac.get('gravity', 0),
            'vd': fac.get('vol_div', 0),
            'md': fac.get('macd_div', 0),
            'detail': s['detail'],
        }
        # 前瞻收益
        for h in ALL_H:
            r[f'fwd{h}'] = fwd(idx, h)
        # MFE (未来120根内最大有利偏移)
        r['mfe120'] = mfe(idx, 120, 1 if s['type'] == 'B' else -1)
        # 方向验证: B后涨(fwd>0) 或 S后跌(fwd<0)
        fwd24 = r['fwd24']
        r['dir_ok_24'] = (fwd24 is not None and
                          ((s['type'] == 'B' and fwd24 > 0) or (s['type'] == 'S' and fwd24 < 0)))
        rows.append(r)
    sig_df = pd.DataFrame(rows)

    # ---- v9 真实出场纪律配对 (单日 forward-T) ----
    o = df['open'].values.astype(float)[gs:ge]
    h = df['high'].values.astype(float)[gs:ge]
    lo = df['low'].values.astype(float)[gs:ge]
    c = df['close'].values.astype(float)[gs:ge]
    # 当日 indicators 取 atr
    pc = float(c_all[gs - 1]) if gs > 0 else float(c[0])
    data = ME.compute_miji_indicators(o, h, lo, c, None, pc, has_vol=False)
    atr = data['atr']
    prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': atr, 'n': len(c)}
    sim_sigs = [{'type': s['type'], 'idx': s['idx'] - gs, 'price': s['price'],
                 'reason': s['detail']} for s in day_sigs]
    trips = simulate_day(sim_sigs, prices, CFG)
    metrics = aggregate_metrics(trips)
    trip_df = pd.DataFrame(trips)

    # ---- 汇总 ----
    nB = sum(1 for r in rows if r['type'] == 'B')
    nS = sum(1 for r in rows if r['type'] == 'S')
    dir_ok = sum(1 for r in rows if r['dir_ok_24'])
    summary = {
        'symbol': SYM, 'day': DAY, 'mode': 'resonance', 'min_resonance': ME.RESONANCE_THRESHOLD,
        'n_signals': len(rows), 'n_B': nB, 'n_S': nS,
        'dir_ok_24_count': dir_ok, 'dir_ok_24_rate': round(dir_ok / len(rows) * 100, 1) if rows else 0,
        'factor_combo': {
            'B': sig_df[sig_df.type == 'B']['g'].astype(str) + sig_df[sig_df.type == 'B']['vd'].astype(str) + sig_df[sig_df.type == 'B']['md'].astype(str).tolist() if nB else [],
            'S': sig_df[sig_df.type == 'S']['g'].astype(str) + sig_df[sig_df.type == 'S']['vd'].astype(str) + sig_df[sig_df.type == 'S']['md'].astype(str).tolist() if nS else [],
        },
        'v9_roundtrip': metrics,
        'exit_config': CFG,
    }

    os.makedirs(OUT, exist_ok=True)
    sig_df.to_csv(os.path.join(OUT, 'signals.csv'), index=False, encoding='utf-8-sig')
    trip_df.to_csv(os.path.join(OUT, 'trips.csv'), index=False, encoding='utf-8-sig')
    with open(os.path.join(OUT, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    # ---- 价格图 + 信号标注 ----
    fig, ax = plt.subplots(figsize=(14, 5))
    t = range(len(c))
    ax.plot(t, c, color='#4a90d9', lw=0.9, label='close')
    for s in day_sigs:
        local = s['idx'] - gs
        if s['type'] == 'B':
            ax.scatter(local, s['price'], marker='^', color='#2ecc71', s=60, zorder=5)
        else:
            ax.scatter(local, s['price'], marker='v', color='#e74c3c', s=60, zorder=5)
    ax.set_title(f'{SYM} {DAY} resonance signals (B=green^ / S=red v)', fontsize=12)
    ax.set_xlabel('bar (1min)'); ax.set_ylabel('price')
    ax.legend(loc='best')
    ax.grid(alpha=0.25)
    fig.tight_layout()
    png = os.path.join(OUT, 'chart.png')
    fig.savefig(png, dpi=110)
    plt.close(fig)
    # base64 内嵌，保证 HTML 自包含
    import base64
    with open(png, 'rb') as fh:
        png_b64 = base64.b64encode(fh.read()).decode('ascii')

    # ---- HTML dashboard ----
    html = build_html(summary, sig_df, trip_df, png_b64)
    with open(os.path.join(OUT, 'dashboard.html'), 'w', encoding='utf-8') as f:
        f.write(html)

    # ---- 控制台摘要 ----
    print(f"=== {SYM} {DAY} resonance 信号审计 ===")
    print(f"总信号={len(rows)}  B={nB}  S={nS}")
    print(f"24分钟方向验证: {dir_ok}/{len(rows)} = {summary['dir_ok_24_rate']}%")
    print(f"v9配对: 笔数={metrics['total']} 胜率={metrics['win_rate']}% 盈亏比={metrics['pl_ratio']} 总收益={metrics['total_ret']}% 出场分布={metrics['by_reason']}")
    print(f"输出目录: {os.path.abspath(OUT)}")
    print("\n信号明细(前20):")
    cols = ['time','type','price','resonance_score','g','vd','md','fwd6','fwd24','fwd120','mfe120','dir_ok_24']
    with pd.option_context('display.width', 200, 'display.max_columns', 20):
        print(sig_df[cols].head(20).to_string(index=False))


def build_html(summary, sig_df, trip_df, png_b64):
    m = summary['v9_roundtrip']
    sig_rows = ""
    for _, r in sig_df.iterrows():
        cls = 'b' if r['type'] == 'B' else 's'
        ok = 'ok' if r['dir_ok_24'] else 'no'
        sig_rows += (f"<tr class='{cls}'><td>{r['time']}</td><td>{r['type']}</td>"
                     f"<td>{r['price']}</td><td>{r['resonance_score']}</td>"
                     f"<td>{r['g']}/{r['vd']}/{r['md']}</td>"
                     f"<td>{r['fwd6']}</td><td>{r['fwd24']}</td><td>{r['fwd120']}</td>"
                     f"<td>{r['mfe120']}</td><td class='{ok}'>{'✓' if r['dir_ok_24'] else '✗'}</td>"
                     f"<td class='det'>{r['detail']}</td></tr>")
    trip_rows = ""
    for _, t in trip_df.iterrows():
        trip_rows += (f"<tr><td>{t['entry_idx']}</td><td>{t['entry_price']}</td>"
                      f"<td>{t['exit_idx']}</td><td>{t['exit_price']}</td>"
                      f"<td>{t['exit_reason']}</td><td>{t['ret_pct']}</td><td>{t['hold_bars']}</td>"
                      f"<td class='det'>{t['entry_reason']}</td></tr>")

    return f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>161129 {summary['day']} resonance 信号审计</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;margin:0;background:#0f1115;color:#e6e6e6;padding:20px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8aa;font-size:13px;margin-bottom:18px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}}
.kpi{{background:#1a1e26;border:1px solid #2a2f3a;border-radius:10px;padding:12px 16px;min-width:120px}}
.kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:12px;color:#9aa}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:24px}}
th,td{{border:1px solid #2a2f3a;padding:5px 7px;text-align:center}}
th{{background:#1a1e26;color:#bcd}} tr.b td:first-child{{color:#2ecc71}} tr.s td:first-child{{color:#e74c3c}}
td.ok{{color:#2ecc71}} td.no{{color:#e74c3c}} td.det{{text-align:left;color:#bbb;font-size:11.5px}}
h2{{font-size:15px;border-left:3px solid #4a90d9;padding-left:8px;margin:24px 0 10px}}
img{{max-width:100%;border:1px solid #2a2f3a;border-radius:8px}}
.warn{{background:#2a1f1f;border:1px solid #5a3a3a;color:#f0b;padding:8px 12px;border-radius:8px;font-size:12.5px}}
</style></head><body>
<h1>161129 原油LOF · {summary['day']} · resonance 信号审计</h1>
<div class='sub'>模式=resonance (≥{summary['min_resonance']}因子同向) · vol_div 强制开启 · 数据=1分钟K</div>
<div class='kpis'>
  <div class='kpi'><div class='v'>{summary['n_signals']}</div><div class='l'>总信号</div></div>
  <div class='kpi'><div class='v' style='color:#2ecc71'>{summary['n_B']}</div><div class='l'>买入B</div></div>
  <div class='kpi'><div class='v' style='color:#e74c3c'>{summary['n_S']}</div><div class='l'>卖出S</div></div>
  <div class='kpi'><div class='v'>{summary['dir_ok_24_rate']}%</div><div class='l'>24m方向验证率</div></div>
  <div class='kpi'><div class='v'>{m['total']}</div><div class='l'>v9配对笔数</div></div>
  <div class='kpi'><div class='v'>{m['win_rate']}%</div><div class='l'>胜率</div></div>
  <div class='kpi'><div class='v'>{m['pl_ratio']}</div><div class='l'>盈亏比</div></div>
  <div class='kpi'><div class='v'>{m['total_ret']}%</div><div class='l'>总收益</div></div>
</div>
<div class='warn'>⚠️ 以上为模型回放，非实盘信号。resonance 为 v9.3.0 试验分支，生产仍以 floor 为准。</div>
<h2>价格走势 + 信号标注</h2>
<img src='data:image/png;base64,{png_b64}'>
<h2>全部信号明细</h2>
<table><thead><tr><th>时间</th><th>类型</th><th>价格</th><th>共振分</th><th>g/vd/md</th>
<th>fwd6</th><th>fwd24</th><th>fwd120</th><th>MFE120</th><th>24m对</th><th>触发条件</th></tr></thead>
<tbody>{sig_rows}</tbody></table>
<h2>v9 出场纪律配对 (移动止损0.4/0.6 + 硬止损 + 时间止损 + S出场)</h2>
<table><thead><tr><th>入场idx</th><th>入场价</th><th>出场idx</th><th>出场价</th><th>出场原因</th><th>收益%</th><th>持有</th><th>入场原因</th></tr></thead>
<tbody>{trip_rows}</tbody></table>
<div class='sub'>MFE120: 信号后120分钟内最大有利偏移(B看最高价/S看最低价)。fwdN: 信号后N分钟close-to-close收益%。</div>
</body></html>"""


if __name__ == '__main__':
    main()
