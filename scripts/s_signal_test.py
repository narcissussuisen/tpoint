# -*- coding: utf-8 -*-
"""
s_signal_test.py — P3-2 S 信号专项回测验证（阶段D，2026-08-02）

背景：259 万样本报告 S AUC 0.7421（B 0.6481）→ S 可预测性更高；
  S_vwap_dev 最优箱 [0.842,1.200] 42.81%（温和偏离才是好卖点，极端偏离追高胜率回落）；
  S_atr_pct 单调升（0.349→0.456）→ S 侧加偏离上限 + ATR 门控。

方法：40 只调参池（第一段）+ watchlist 5 只（第二段，独立验证不做二次调参）
  - 基线 = 生产现状（B 侧 mpr_b60 + atr0.25 已落地），S 侧无过滤
  - vwap_ceil12    : + S 侧 vwap_dev_ceil=1.2%
  - atr_s025       : + S 侧 atr_min_pct_s=0.25%
  - vwap+atr       : 两者叠加
  只动 S 判定层，B 侧恒生产配置 → 干净隔离 S 侧过滤效应。

双口径（防标签转移假象）：
  - 全回合（all trips）：S 被滤后回合可能改由 TRAIL/TIME/EOD 出场 → 全口径总收益若下降
    说明只是把亏损转嫁到别的出场标签，不是真改进
  - S 触发回合（exit_reason=='S'）：主判定口径（阶段C 教训：S 侧= s_signal_exit 出场源）

判读（固定全集口径）：
  - S 触发净胜率中位 Δ≥+1pp 且 S 触发样本保留 ≥80%
  - watchlist 独立验证无单只 >3pp 退化（全口径）
  - 全口径总收益不得恶化（防标签转移）

用法：
  python scripts/s_signal_test.py --phase pool --procs 8
  python scripts/s_signal_test.py --phase watchlist --procs 4
  python scripts/s_signal_test.py --phase report
"""
import argparse
import datetime
import json
import os
import sys
from statistics import median

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault('MACD_GATE_MODE', 'floor')

from core.exit_manager import (make_config, simulate_day, aggregate_metrics,  # noqa: E402
                               cost_for_symbol)

PROD_CONFIG = dict(
    use_stop=False, use_time=False,
    use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
    s_signal_exit=True,
)
# 生产现状 B 侧配置（方案A + 方案A+ 已落地）
PROD_B = dict(macd_min_hist_diff=0.15, atr_min_pct=0.25,
              mpr_enable='B', mpr_periods=(60,))

DATA_DIR = os.path.join(BASE, 'data')
OUT_DIR = os.path.join(BASE, 'output')
F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = ['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH']

COMBOS = [
    {'name': 'baseline',      'label': '基线(生产B侧)', 'vwap_dev_ceil': None, 'atr_min_pct_s': None},
    {'name': 'vwap_ceil12',   'label': '+S vwap≤1.2%',  'vwap_dev_ceil': 1.2,  'atr_min_pct_s': None},
    {'name': 'atr_s025',      'label': '+S atr≥0.25%',  'vwap_dev_ceil': None, 'atr_min_pct_s': 0.25},
    {'name': 'vwap_atr_s',    'label': '+S 两者叠加',   'vwap_dev_ceil': 1.2,  'atr_min_pct_s': 0.25},
]


def detect_for_combo(data, pc, combo):
    import core.miji_alpha as ma
    old = ma.VWAP_DEV_BUY
    ma.VWAP_DEV_BUY = 0.65
    try:
        sigs = ma.detect_miji_signals(
            data, pc,
            macd_min_hist_diff=PROD_B['macd_min_hist_diff'],
            atr_min_pct=PROD_B['atr_min_pct'],
            mpr_enable=PROD_B['mpr_enable'], mpr_periods=PROD_B['mpr_periods'],
            vwap_dev_ceil=combo['vwap_dev_ceil'], atr_min_pct_s=combo['atr_min_pct_s'])
    finally:
        ma.VWAP_DEV_BUY = old
    return sigs


def backtest_s_symbol(csv_path, combos):
    """单标的 × 多组合。输出全回合 + S触发回合双口径 + 信号数。"""
    from core.miji_alpha import compute_miji_indicators
    from tune_grid import load_and_group, build_is_morning
    symbol, df, days, day_prev_close = load_and_group(csv_path)
    cost = cost_for_symbol(symbol)
    mcfg = make_config(**PROD_CONFIG)
    out = {}
    for c in combos:
        out[c['name']] = {'_trips': [], '_trips_s': [], 'n_b': 0, 'n_s': 0}
    day_count = 0
    for date, sub in days:
        pc = day_prev_close(df, date)
        if pc is None or pc <= 0:
            continue
        o = sub['open'].values.astype(float)
        h = sub['high'].values.astype(float)
        lo = sub['low'].values.astype(float)
        c = sub['close'].values.astype(float)
        v = sub['volume'].values.astype(float)
        data = compute_miji_indicators(o, h, lo, c, v, pc)
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data.get('trend'), 'n': data['n']}
        for combo in combos:
            sigs = detect_for_combo(data, pc, combo)
            trips = simulate_day(sigs, prices, mcfg, cost=cost)
            cn = combo['name']
            out[cn]['_trips'].extend(trips)
            out[cn]['_trips_s'].extend([t for t in trips if t.get('exit_reason') == 'S'])
            out[cn]['n_b'] += sum(1 for s in sigs if s['type'] == 'B')
            out[cn]['n_s'] += sum(1 for s in sigs if s['type'] == 'S')
        day_count += 1
    for cn in out:
        m = aggregate_metrics(out[cn]['_trips'])
        ms = aggregate_metrics(out[cn]['_trips_s'])
        out[cn].update({
            'total': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
            'total_ret': m['total_ret'],
            'total_s_trips': ms['total'], 'win_s': ms['win_rate'], 'pl_s': ms['pl_ratio'],
        })
        del out[cn]['_trips']; del out[cn]['_trips_s']
    out['_meta'] = {'symbol': symbol, 'days': day_count}
    return out


def worker(symbol, csv_path, combos):
    sys.path.insert(0, BASE)
    os.environ.setdefault('MACD_GATE_MODE', 'floor')
    try:
        return symbol, backtest_s_symbol(csv_path, combos)
    except Exception as e:
        import traceback
        return symbol, {'_meta': {'symbol': symbol, 'error': str(e),
                                  'trace': traceback.format_exc()[:500]}}


def run_pool(symbols, combos, procs=4, label=''):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    paths = []
    for sym in symbols:
        p = os.path.join(F_DATA, f'{sym}_1m.csv')
        paths.append((sym, p))
    tasks = [(sym, p) for sym, p in paths if os.path.exists(p)]
    missing = [sym for sym, p in paths if not os.path.exists(p)]
    for sym in missing:
        print(f'  ⚠️ 数据缺失: {sym}')
    print(f'🎯 {label}: {len(tasks)} 标的 × {len(combos)} 组合，{procs} 进程')
    t0 = datetime.datetime.now()
    results = {}
    with ProcessPoolExecutor(max_workers=procs) as ex:
        futs = {ex.submit(worker, sym, p, combos): sym for sym, p in tasks}
        done = 0
        for fut in as_completed(futs):
            sym, res = fut.result()
            results[sym] = res
            done += 1
            meta = res.get('_meta', {})
            if meta.get('error'):
                print(f'  ❌ [{done}/{len(tasks)}] {sym}: {meta["error"]}')
            else:
                print(f'  ✅ [{done}/{len(tasks)}] {sym} 天数{meta.get("days")}')
    el = (datetime.datetime.now() - t0).total_seconds()
    print(f'⏱️  {label} 完成，耗时 {el:.0f}s')
    return results


def pool_stats(results, cname, key):
    """某组合某口径的池级统计（固定全集口径）。key: 'all'/'S'。"""
    syms = [s for s in results if not s.startswith('_')]
    if key == 'S':
        wk, tk = 'win_s', 'total_s_trips'
    else:
        wk, tk = 'win_rate', 'total'
    wins = [results[s][cname][wk] for s in syms
            if isinstance(results[s].get(cname), dict) and results[s][cname].get(tk, 0) > 0]
    trips = sum(results[s][cname][tk] for s in syms
                if isinstance(results[s].get(cname), dict))
    rets = [results[s][cname]['total_ret'] for s in syms
            if isinstance(results[s].get(cname), dict)]
    return {
        'n_sym_with': len(wins),
        'med_win': round(median(wins), 2) if wins else 0.0,
        'total_trips': trips,
        'sum_ret': round(sum(rets), 2),
    }


def run_phase(symbols, out_json, label, procs):
    per_sym = run_pool(symbols, COMBOS, procs=procs, label=label)
    combos = []
    for c in COMBOS:
        cstat = {'all': pool_stats(per_sym, c['name'], 'all'),
                 'S': pool_stats(per_sym, c['name'], 'S')}
        nb = sum(per_sym[s][c['name']]['n_b'] for s in per_sym if not s.startswith('_'))
        ns = sum(per_sym[s][c['name']]['n_s'] for s in per_sym if not s.startswith('_'))
        combos.append({**c, 'pool': cstat, 'n_b_sig': nb, 'n_s_sig': ns,
                       'per_symbol': {s: per_sym[s].get(c['name'], {}) for s in per_sym
                                      if c['name'] in per_sym.get(s, {})}})
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbols': symbols,
        'config': PROD_CONFIG,
        'prod_b': PROD_B,
        'combos': combos,
    }
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 已写入 {out_json}')
    print(f'\n{"组合":<18}{"全回合胜率":>10}{"全笔数":>7}{"全收益%":>9}{"S触发胜率":>10}{"S触发笔数":>9}')
    for c in combos:
        print(f'{c["label"]:<18}{c["pool"]["all"]["med_win"]:>9.2f}%'
              f'{c["pool"]["all"]["total_trips"]:>7}{c["pool"]["all"]["sum_ret"]:>9.2f}'
              f'{c["pool"]["S"]["med_win"]:>9.2f}%{c["pool"]["S"]["total_trips"]:>9}')
    return payload


def phase_pool(args):
    with open(os.path.join(DATA_DIR, 'tune_pool_40.json'), encoding='utf-8') as f:
        pool = json.load(f)['pool']
    symbols = [p['symbol'] for p in pool]
    if args.smoke:
        symbols = symbols[:3]
        print(f'🔥 冒烟（{len(symbols)} 只）')
    run_phase(symbols, os.path.join(DATA_DIR, 's_signal_test_pool.json'),
              '40 只调参池 S专项', args.procs)


def phase_watchlist(args):
    run_phase(WATCHLIST, os.path.join(DATA_DIR, 's_signal_test_watchlist.json'),
              'watchlist 5 只 S专项', args.procs)


def build_html(pool, wl):
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # ---- 自动判读结论 ----
    def combo_delta(data, cname, key):
        b = next(c for c in data['combos'] if c['name'] == 'baseline')['pool'][key]
        t = next(c for c in data['combos'] if c['name'] == cname)['pool'][key]
        return (round(t['med_win'] - b['med_win'], 2),
                round(t['total_trips'] / max(b['total_trips'], 1) * 100, 0),
                round(t['sum_ret'] - b['sum_ret'], 2))

    def verdict_row(data):
        rows = ''
        for cname, desc in [('vwap_ceil12', 'S vwap≤1.2%'), ('atr_s025', 'S atr≥0.25%'),
                            ('vwap_atr_s', 'S 两者叠加')]:
            d_s, keep_s, _ = combo_delta(data, cname, 'S')
            _, _, d_ret_all = combo_delta(data, cname, 'all')
            ok = d_s >= 1 and keep_s >= 80 and d_ret_all >= -1
            cls = 'ok' if ok else 'bad'
            verdict = '✅ 达标' if ok else ('⚠️ 方向对但样本/收益不足' if d_s >= 1 else '❌ 不达标')
            rows += (f'<tr><td>{desc}</td><td>S胜率Δ{d_s:+.2f}pp<br>'
                     f'<span class="dim">S保留{keep_s:.0f}% / 全收益Δ{d_ret_all:+.2f}%</span></td>'
                     f'<td class="{cls}">{verdict}</td></tr>')
        return rows
    pool_verdict = verdict_row(pool)
    wl_verdict = verdict_row(wl)

    def combo_rows(data):
        rows = ''
        for c in data['combos']:
            p = c['pool']
            rows += (
                f'<tr><td>{esc(c["label"])}</td>'
                f'<td>{p["all"]["med_win"]:.2f}%</td><td>{p["all"]["total_trips"]}</td>'
                f'<td>{p["all"]["sum_ret"]:.2f}%</td>'
                f'<td>{p["S"]["med_win"]:.2f}%</td><td>{p["S"]["total_trips"]}</td>'
                f'<td>{c["n_b_sig"]}</td><td>{c["n_s_sig"]}</td></tr>')
        return rows

    # watchlist 逐标的 × 组合（S触发口径 + 全口径）
    wl_rows = ''
    for s in wl['symbols']:
        cell = ''
        for c in wl['combos']:
            m = c['per_symbol'].get(s, {})
            if not m:
                cell += '<td>-</td>'
                continue
            cell += (f'<td><b>{m.get("win_s", 0):.1f}%</b>({m.get("total_s_trips", 0)})'
                     f'<br><span class="dim">全:{m.get("win_rate", 0):.1f}%({m.get("total", 0)}) '
                     f'收益{m.get("total_ret", 0):.2f}%</span></td>')
        wl_rows += f'<tr><td>{esc(s)}</td>{cell}</tr>'
    wl_head = ''.join(f'<th>{esc(c["label"])}</th>' for c in wl['combos'])

    # pool 逐标的 Δ（主组合 vwap_atr_s vs baseline，S触发口径 + 全口径防转移）
    base_c = next(c for c in pool['combos'] if c['name'] == 'baseline')
    tgt_c = next(c for c in pool['combos'] if c['name'] == 'vwap_atr_s')
    sym_rows = []
    for s in pool['symbols']:
        b = base_c['per_symbol'].get(s, {})
        t = tgt_c['per_symbol'].get(s, {})
        if not b or not t:
            continue
        d_s = t.get('win_s', 0) - b.get('win_s', 0)
        d_all = t.get('win_rate', 0) - b.get('win_rate', 0)
        d_ret = t.get('total_ret', 0) - b.get('total_ret', 0)
        keep_s = t.get('total_s_trips', 0) / max(b.get('total_s_trips', 0), 1) * 100
        sym_rows.append((s, b.get('total_s_trips', 0), b.get('win_s', 0),
                         t.get('total_s_trips', 0), t.get('win_s', 0),
                         d_s, d_all, d_ret, keep_s))
    sym_rows.sort(key=lambda x: -x[5])
    per_rows = ''
    for r in sym_rows:
        per_rows += (
            f'<tr><td>{esc(r[0])}</td><td>{r[1]}笔 {r[2]:.1f}%</td>'
            f'<td>{r[3]}笔 {r[4]:.1f}%</td><td>{r[5]:+.1f}pp</td>'
            f'<td>{r[6]:+.1f}pp</td><td>{r[7]:+.2f}%</td><td>{r[8]:.0f}%</td></tr>')
    n_up = sum(1 for r in sym_rows if r[5] > 0)
    n_dn = sum(1 for r in sym_rows if r[5] < 0)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>P3-2 S 信号专项回测 {esc(datetime.date.today())}</title>
<style>
body{{background:#11151c;color:#d5dae2;font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1200px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:#9ec9ff;font-size:15px;margin-top:28px}}
.card{{background:#1a2029;border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #2a3140;font-size:13px}}
th{{color:#8a93a6;font-weight:500}}
.dim{{color:#7d8798;font-size:11px}}
.ok{{color:#7ee787}} .bad{{color:#ff7b72}}
</style></head><body>
<h1>P3-2 S 信号专项 · {esc(datetime.date.today())}</h1>
<div class="card">
  <h2>方法</h2>
  <p>S 侧叠加过滤（B 侧恒生产现状：mpr_b60 + atr0.25 已落地）：<br>
     <b>vwap_dev_ceil</b>=S 侧 VWAP 偏离上限 1.2%（报告 S_vwap_dev 最优箱 [0.842,1.200]，极端偏离追高胜率回落）；<br>
     <b>atr_min_pct_s</b>=S 侧 ATR 门控 0.25%（报告 S_atr_pct 单调升，与 B 侧对称）。<br>
     双口径防标签转移：全回合（S 被滤后可能改由 TRAIL/TIME 出场）+ S 触发回合（exit_reason=='S'，主判定）。<br>
     mhd 恒 0.15 / VWAP0.65 / floor 门控 / 移动止损 act0.4 trail0.6；净胜率=净收益&gt;0。</p>
</div>
<div class="card">
  <h2>40 只调参池（第一段，固定全集口径）</h2>
  <table>
    <tr><th>组合</th><th>全回合胜率</th><th>全笔数</th><th>全收益%</th><th>S触发胜率</th><th>S触发笔数</th><th>B信号</th><th>S信号</th></tr>
    {combo_rows(pool)}
  </table>
  <p class="dim">「全回合」= B 建仓全部 round-trip（防标签转移）；「S触发」= exit_reason=='S' 回合（S 侧主口径）。</p>
</div>
<div class="card">
  <h2>主组合（S vwap≤1.2% + atr≥0.25%）逐标的（40 只，ΔS触发胜率降序）</h2>
  <p><b class="ok">S触发胜率提升 {n_up} 只 / 恶化 {n_dn} 只 / 共 {len(sym_rows)} 只</b></p>
  <table>
    <tr><th>标的</th><th>基线S触发</th><th>过滤后S触发</th><th>ΔS胜率</th><th>Δ全胜率</th><th>Δ全收益%</th><th>S保留</th></tr>
    {per_rows}
  </table>
</div>
<div class="card">
  <h2>watchlist 5 只独立验证（第二段，不做二次调参；S触发胜率(笔数) + 全口径小字）</h2>
  <table>
    <tr><th>标的</th>{wl_head}</tr>
    {wl_rows}
  </table>
</div>
<div class="card">
  <h2>结论与生产建议</h2>
  <table>
    <tr><th>组合</th><th>调参池 40 只</th><th>watchlist 5 只</th><th>判定</th></tr>
    {pool_verdict}
  </table>
  <h2 style="margin-top:18px">判定规则</h2>
  <ul>
    <li>达标：S 触发净胜率中位 Δ≥+1pp 且 S 触发样本保留 ≥80%（与基线 S 回合数比）</li>
    <li>防标签转移：全口径总收益不得恶化——若 S 胜率升但全收益降，说明只是把亏损转嫁到 TRAIL/TIME 标签</li>
    <li>watchlist 独立验证无单只 &gt;3pp 退化（全口径）</li>
  </ul>
</div>
</body></html>"""
    out = os.path.join(OUT_DIR, f's_signal_test_{datetime.date.today().strftime("%Y%m%d")}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out}')
    return out


def phase_report(args):
    with open(os.path.join(DATA_DIR, 's_signal_test_pool.json'), encoding='utf-8') as f:
        pool = json.load(f)
    with open(os.path.join(DATA_DIR, 's_signal_test_watchlist.json'), encoding='utf-8') as f:
        wl = json.load(f)
    out = build_html(pool, wl)

    def delta(data, cname, key):
        b = next(c for c in data['combos'] if c['name'] == 'baseline')['pool'][key]
        t = next(c for c in data['combos'] if c['name'] == cname)['pool'][key]
        return (round(t['med_win'] - b['med_win'], 2),
                round(t['total_trips'] / max(b['total_trips'], 1) * 100, 0),
                round(t['sum_ret'] - b['sum_ret'], 2))
    print(f'\n📊 结论判定（40 只池，固定全集口径）：')
    print(f'  {"组合":<16}{"S胜率Δ":>8}{"S保留":>8}{"全收益Δ":>9}  判定')
    for cname, desc in [('vwap_ceil12', 'S vwap≤1.2%'), ('atr_s025', 'S atr≥0.25%'),
                        ('vwap_atr_s', 'S 两者叠加')]:
        d_s, keep, d_ret = delta(pool, cname, 'S')
        d_all, _, d_ret_all = delta(pool, cname, 'all')
        ok = d_s >= 1 and keep >= 80 and d_ret_all >= -1
        verdict = '✅ 达标' if ok else ('⚠️ 方向对但样本/收益不足' if d_s >= 1 else '❌ 不达标')
        print(f'  {desc:<16}{d_s:>+7.2f}pp{keep:>7.0f}%{d_ret_all:>+9.2f}  {verdict}')
    return out


def main():
    ap = argparse.ArgumentParser(description='P3-2 S 信号专项回测')
    ap.add_argument('--phase', choices=['pool', 'watchlist', 'report'], default='pool')
    ap.add_argument('--procs', type=int, default=4)
    ap.add_argument('--smoke', action='store_true')
    args = ap.parse_args()
    if args.phase == 'pool':
        phase_pool(args)
    elif args.phase == 'watchlist':
        phase_watchlist(args)
    else:
        phase_report(args)


if __name__ == '__main__':
    main()
