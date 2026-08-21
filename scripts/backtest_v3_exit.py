# -*- coding: utf-8 -*-
"""
backtest_v3_exit.py —— exit_v3 三条件止损 vs 旧 trail 出场 对比回测（干净数据）

对齐 toasty-cascade-tesla.md §6 R4 验收：单边日大亏笔数降 ≥50%；全集口径 +2pp。
对比：
  A. 旧出场：正T simulate_day + 反T simulate_bidirectional（trail 0.4/0.6）
  B. v3出场：exit_v3 三条件止损（hard=1.2ATR% vs 0.8% 取严 / trend VWAP反穿+MACD / time 60根）
数据：F:/keyfactor_data/1m_clean（2026-08-20 清洗后）
输出：output/backtest_v3_exit_<date>.json + .html（A vs B 双向对比）
"""
import sys, csv, json, os, argparse, datetime
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT, STRATEGY_VERSION, ENGINE_FULL
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data
from simulate_bidirectional import simulate_bidirectional
from exit_v3 import exit_v3

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')
DEFAULT_SYMS = ['161129.SZ', '513310.SH', '688111.SH', '603039.SH', '300058.SZ', '600570.SH']
NAME = {'603039.SH':'泛微网络','688111.SH':'金山办公','300058.SZ':'蓝色光标','600570.SH':'恒生电子',
        '161129.SZ':'原油LOF易方达','513310.SH':'中概互联网ETF'}


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = (np.array([float(x['open']) for x in rs], dtype=float),
                   np.array([float(x['high']) for x in rs], dtype=float),
                   np.array([float(x['low']) for x in rs], dtype=float),
                   np.array([float(x['close']) for x in rs], dtype=float),
                   np.array([float(x['volume']) for x in rs], dtype=float))
    return days


def run_symbol(sym, min_days=4):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    if len(dates) < min_days:
        return {'sym': sym, 'error': f'insufficient_days({len(dates)})'}
    cost = cost_for_symbol(sym)
    cfg = make_config()
    agg = {'old': {'long': [], 'short': []}, 'v3': {'long': [], 'short': []}}
    prev_close = None
    n_ok = 0
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'vwap': data['vwap'], 'hist': data['hist'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        # A. 旧出场
        agg['old']['long'].extend(simulate_day(sigs, prices, cfg, cost))
        agg['old']['short'].extend(simulate_bidirectional(sigs, prices, cfg, cost))
        # B. v3 出场
        agg['v3']['long'].extend(exit_v3(sigs, prices, direction='long', cost=cost))
        agg['v3']['short'].extend(exit_v3(sigs, prices, direction='short', cost=cost))
        n_ok += 1
        prev_close = c[-1]
    return {'sym': sym, 'days': n_ok, 'first': dates[0], 'last': dates[-1], 'agg': agg}


def summarize(trips):
    if not trips:
        return dict(n=0, wr=0.0, total_ret=0.0, avg=0.0, big_loss=0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    big = sum(1 for t in trips if t['ret_pct'] < -3.0)   # 单边日大亏 <−3%
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg=round(sum(rets) / n, 3), big_loss=big)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default=','.join(DEFAULT_SYMS))
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    syms = [s.strip() for s in a.syms.split(',') if s.strip()]

    results = {}
    for sym in syms:
        r = run_symbol(sym)
        if 'error' in r:
            print(f'[{sym}] SKIP: {r["error"]}')
            results[sym] = {'sym': sym, 'name': NAME.get(sym, sym), 'error': r['error']}
            continue
        row = {'sym': sym, 'name': NAME.get(sym, sym), 'days': r['days'],
               'range': f'{r["first"]}~{r["last"]}'}
        for tag in ('old', 'v3'):
            row[tag] = {'long': summarize(r['agg'][tag]['long']),
                        'short': summarize(r['agg'][tag]['short'])}
            lt, st = row[tag]['long'], row[tag]['short']
            row[tag]['dual_ret'] = round(lt['total_ret'] + st['total_ret'], 2)
        print(f'[{sym}] {row["name"]} days={row["days"]} | old: L{row["old"]["long"]["n"]}W{row["old"]["long"]["wr"]}% '
              f'S{row["old"]["short"]["n"]}W{row["old"]["short"]["wr"]}% 双向{row["old"]["dual_ret"]}% | '
              f'v3: L{row["v3"]["long"]["n"]}W{row["v3"]["long"]["wr"]}% S{row["v3"]["short"]["n"]}W{row["v3"]["short"]["wr"]}% 双向{row["v3"]["dual_ret"]}%')
        results[sym] = row

    # 池级
    pool = {}
    for tag in ('old', 'v3'):
        ll = [r[tag]['long'] for r in results.values() if 'error' not in r]
        ss = [r[tag]['short'] for r in results.values() if 'error' not in r]
        pool[tag] = {'long': summarize(sum([x['n'] and [{'ret_pct': 0}] or [] for x in ll], [])),  # placeholder
                     'short': summarize([])}
        # 直接按 trip 汇总
        lt_all, st_all = [], []
        for sym, r in results.items():
            if 'error' not in r:
                # 需要原始 trips → 重跑一次（小代价，保证口径）
                rr = run_symbol(sym)
                lt_all.extend(rr['agg'][tag]['long'])
                st_all.extend(rr['agg'][tag]['short'])
        pool[tag] = {'long': summarize(lt_all), 'short': summarize(st_all),
                     'dual_ret': round(summarize(lt_all)['total_ret'] + summarize(st_all)['total_ret'], 2)}
    print(f"\n池级 old: L{pool['old']['long']['n']}W{pool['old']['long']['wr']}% S{pool['old']['short']['n']}W{pool['old']['short']['wr']}% 双向{pool['old']['dual_ret']}%")
    print(f"池级 v3 : L{pool['v3']['long']['n']}W{pool['v3']['long']['wr']}% S{pool['v3']['short']['n']}W{pool['v3']['short']['wr']}% 双向{pool['v3']['dual_ret']}%")

    ver = {'strategy_version': STRATEGY_VERSION, 'engine_full': ENGINE_FULL}
    out = {'date': a.out_suffix, 'engine': 'general', **ver,
           'exit_compare': {'old': 'simulate_day/simulate_bidirectional(trail0.4/0.6)',
                            'v3': 'exit_v3(hard=1.2ATR%vs0.8%取严/trend=VWAP反穿+MACD/time=60根)'},
           'symbols': results, 'pool': pool}
    fn = f'backtest_v3_exit_{a.out_suffix}'
    with open(os.path.join(OUT, fn + '.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    build_html(out, os.path.join(OUT, fn + '.html'))


def build_html(out, path):
    def cell(x):
        return f'<td>{x["n"]}</td><td>{x["wr"]}%</td><td>{x["total_ret"]}%</td><td>{x["big_loss"]}</td>'
    rows = ''
    for sym, r in out['symbols'].items():
        if 'error' in r:
            rows += f'<tr><td>{sym}</td><td>{r["name"]}</td><td colspan="12">SKIP {r["error"]}</td></tr>'
            continue
        rows += f'<tr><td>{sym}</td><td>{r["name"]}</td><td>{r["days"]}</td>' \
                f'{cell(r["old"]["long"])}{cell(r["old"]["short"])}<td>{r["old"]["dual_ret"]}%</td>' \
                f'{cell(r["v3"]["long"])}{cell(r["v3"]["short"])}<td>{r["v3"]["dual_ret"]}%</td></tr>'
    p = out['pool']
    old_big = p['old']['long']['big_loss'] + p['old']['short']['big_loss']
    v3_big = p['v3']['long']['big_loss'] + p['v3']['short']['big_loss']
    big_reduce = round(100.0 * (old_big - v3_big) / old_big, 1) if old_big else 0.0
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>exit_v3 三条件止损 vs 旧trail 对比 {out['date']}</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:20px">
<div style="max-width:1400px;margin:0 auto">
<h2 style="color:#1f2a44">exit_v3 三条件止损 vs 旧trail 出场 · {out['date']}</h2>
<p style="color:#666">数据: F:/keyfactor_data/1m_clean（清洗后）｜ 信号: v5/GT general ｜ 旧: trail 0.4/0.6 ｜ v3: 硬止损1.2ATR%vs0.8%取严 + VWAP反穿MACD同向 + 时间60根</p>
<div style="background:#fff;border-radius:10px;padding:14px;border-left:4px solid #1565c0;margin-bottom:12px">
<b>池级对比（正T long + 反T short）</b><br>
旧: 双向净 {p['old']['dual_ret']}%（L WR{p['old']['long']['wr']}% n{p['old']['long']['n']} / S WR{p['old']['short']['wr']}% n{p['old']['short']['n']}）大亏{old_big}笔<br>
v3: 双向净 <b style="color:{'#2e7d32' if p['v3']['dual_ret']>p['old']['dual_ret'] else '#c62828'}">{p['v3']['dual_ret']}%</b>（L WR{p['v3']['long']['wr']}% n{p['v3']['long']['n']} / S WR{p['v3']['short']['wr']}% n{p['v3']['short']['n']}）大亏{v3_big}笔（{big_reduce}%↓）</div>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;width:100%;background:#fff;font-size:12px">
<tr style="background:#eef2f7"><th rowspan="2">标的</th><th rowspan="2">天数</th>
<th colspan="5" style="border-bottom:1px solid #ccc">旧出场 (trail)</th><th colspan="5">v3 三条件止损</th></tr>
<tr style="background:#f5f8fc"><th>L n</th><th>L WR</th><th>L 净</th><th>S n</th><th>S WR</th><th>S 净</th><th>双向</th><th>L n</th><th>L WR</th><th>L 净</th><th>S n</th><th>S WR</th><th>S 净</th><th>双向</th></tr>
{rows}</table>
<p style="color:#888;font-size:12px;margin-top:10px">口径: 信号质量回测（无冷却/仓位/regime）；大亏 = 单笔 net &lt; -3%。</p>
</div></body></html>"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML -> {path}')


if __name__ == '__main__':
    main()
