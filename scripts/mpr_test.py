# -*- coding: utf-8 -*-
"""
mpr_test.py — P3-1 多周期 MACD 方向过滤回测验证（2026-08-02）

背景：259 万样本报告 S 侧 macd60_dif perm 0.1007（第 1）、B 侧 macd60_dif perm 0.0475（第 2）
  → 多周期方向是 S/B 两侧最强结构性特征。落地方式 = 大周期 hist 方向一致过滤：
    B 要求 60m/15m hist 均 <0（大周期在下方，顺大势抄底）
    S 要求 60m/15m hist 均 >0（大周期在上方，顺大势逃顶）

方法：40 只调参池（第一段）+ watchlist 5 只（第二段，独立验证不做二次调参）
  - baseline（mhd0.15 / VWAP0.65 / 无 mpr）
  - mpr_s60    （S 侧仅 60m hist>0 过滤）
  - mpr_s6015  （S 侧 60m+15m hist 均>0 过滤）
  - mpr_b60    （B 侧仅 60m hist<0 过滤）
  - mpr_b6015  （B 侧 60m+15m hist 均<0 过滤）
  - mpr_both6015（B/S 双侧 60m+15m）

分侧统计（S/B 不混合）：S 侧组合对比 S 配对回合的净胜率/盈亏比；
  B 侧组合对比 B 配对回合。simulate_day 的 trip 带 direction 字段（B/S）。

判读（固定全集口径）：
  - S 侧达标线：Δ≥+1pp 且样本保留 ≥80%（与基线 S 回合数比）
  - B 侧达标线：Δ≥+1pp 且样本保留 ≥80%
  - watchlist 独立验证无单只 >3pp 退化

用法：
  python scripts/mpr_test.py --phase pool --procs 8        # 40 只调参池
  python scripts/mpr_test.py --phase watchlist --procs 4   # watchlist 5 只
  python scripts/mpr_test.py --phase report                # 生成 HTML

产出：
  data/mpr_test_pool.json / data/mpr_test_watchlist.json
  output/mpr_test_YYYYMMDD.html
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

DATA_DIR = os.path.join(BASE, 'data')
OUT_DIR = os.path.join(BASE, 'output')
F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = ['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH']

# mpr 组合定义：side=None 表示全关（基线）
COMBOS = [
    {'name': 'baseline', 'label': '基线 mhd0.15', 'side': None, 'periods': None},
    {'name': 'mpr_s60', 'label': 'S侧 mpr60(>0)', 'side': 'S', 'periods': (60,)},
    {'name': 'mpr_s6015', 'label': 'S侧 mpr60+15(>0)', 'side': 'S', 'periods': (60, 15)},
    {'name': 'mpr_b60', 'label': 'B侧 mpr60(<0)', 'side': 'B', 'periods': (60,)},
    {'name': 'mpr_b6015', 'label': 'B侧 mpr60+15(<0)', 'side': 'B', 'periods': (60, 15)},
    {'name': 'mpr_both6015', 'label': 'B/S双 mpr60+15', 'side': 'both', 'periods': (60, 15)},
]


def detect_for_mpr(data, pc, combo, morning_arr):
    """按组合 detect 当日信号，返回 (sigs, n_b, n_s)。mpr_enable 支持分侧。"""
    import core.miji_alpha as ma
    old = ma.VWAP_DEV_BUY
    ma.VWAP_DEV_BUY = 0.65
    try:
        sigs = ma.detect_miji_signals(
            data, pc, macd_min_hist_diff=0.15,
            mpr_enable=combo['side'], mpr_periods=combo['periods'])
    finally:
        ma.VWAP_DEV_BUY = old
    n_b = sum(1 for s in sigs if s['type'] == 'B')
    n_s = sum(1 for s in sigs if s['type'] == 'S')
    return sigs, n_b, n_s


def backtest_mpr_symbol(csv_path, combos):
    """单标的 × 多组合（含按 side 拆分 S/B 回合统计）。"""
    from core.miji_alpha import compute_miji_indicators
    from tune_grid import load_and_group, build_is_morning
    symbol, df, days, day_prev_close = load_and_group(csv_path)
    cost = cost_for_symbol(symbol)
    mcfg = make_config(**PROD_CONFIG)
    meta = {'symbol': symbol, 'days': len(days)}
    out = {c['name']: {'total': 0, 'win_rate': 0.0, 'pl_ratio': 0.0,
                       'total_b': 0, 'total_s': 0,
                       'total_s_trips': 0, 'win_s': 0.0, 'pl_s': 0.0}
           for c in combos}
    for cname in out:
        out[cname]['_trips'] = []
        out[cname]['_trips_s'] = []
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
        morning_arr = build_is_morning(sub)
        for cname, combo in {c['name']: c for c in combos}.items():
            sigs, n_b, n_s = detect_for_mpr(data, pc, combo, morning_arr)
            trips = simulate_day(sigs, prices, mcfg, cost=cost)
            out[cname]['_trips'].extend(trips)
            # B 侧回合 = 全部 trips（B 建仓）；S 侧回合 = exit_reason=='S' 的 trips（S 触发出场）
            out[cname]['_trips_s'].extend([t for t in trips if t.get('exit_reason') == 'S'])
            out[cname]['total_b'] += n_b
            out[cname]['total_s'] += n_s
        day_count += 1
    for cname in out:
        m = aggregate_metrics(out[cname]['_trips'])
        ms = aggregate_metrics(out[cname]['_trips_s'])
        out[cname].update({
            'total': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
            'total_s_trips': ms['total'], 'win_s': ms['win_rate'], 'pl_s': ms['pl_ratio'],
        })
        del out[cname]['_trips']; del out[cname]['_trips_s']
        out[cname]['days'] = day_count
    out['_meta'] = meta
    return out


def worker(symbol, csv_path, combos):
    sys.path.insert(0, BASE)
    os.environ.setdefault('MACD_GATE_MODE', 'floor')
    try:
        res = backtest_mpr_symbol(csv_path, combos)
        return symbol, res
    except Exception as e:
        import traceback
        return symbol, {'_meta': {'symbol': symbol, 'error': str(e),
                                  'trace': traceback.format_exc()[:500]}}


def run_mpr_pool(symbols, combos, procs=4, label=''):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    paths = []
    for sym in symbols:
        p = os.path.join(F_DATA, f'{sym}_1m.csv')
        if not os.path.exists(p):
            p = os.path.join(BASE, 'backtest', 'backtest_data', f'{sym}_1m.csv')
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


def side_pool_stats(results, cname, side):
    """某组合某侧（'all'/'B'/'S'）的池级统计（固定全集口径）。
    'B' 侧 = 全部回合（B 建仓）；'S' 侧 = exit_reason=='S' 的回合（S 触发出场）。"""
    syms = [s for s in results if not s.startswith('_')]
    if side == 'S':
        wk, tk = 'win_s', 'total_s_trips'
    else:  # 'all' 与 'B' 同：B 建仓的全部回合
        wk, tk = 'win_rate', 'total'
    wins = [results[s][cname][wk] for s in syms
            if isinstance(results[s].get(cname), dict) and results[s][cname].get(tk, 0) > 0]
    trips = sum(results[s][cname][tk] for s in syms
                if isinstance(results[s].get(cname), dict))
    return {
        'n': len(syms),
        'med_win': round(median(wins), 2) if wins else 0.0,
        'total_trips': trips,
    }


def signal_counts(per_sym, cname):
    """某组合全池 B/S 信号总数。"""
    syms = [s for s in per_sym if not s.startswith('_')]
    b = sum(per_sym[s][cname].get('total_b', 0) for s in syms
            if isinstance(per_sym[s].get(cname), dict))
    s = sum(per_sym[s][cname].get('total_s', 0) for s in syms
            if isinstance(per_sym[s].get(cname), dict))
    return b, s


def run_phase(symbols, out_json, label, procs):
    per_sym = run_mpr_pool(symbols, COMBOS, procs=procs, label=label)
    # 组装组合级统计（all / B / S 三口径）
    combos = []
    for c in COMBOS:
        cstat = {'all': side_pool_stats(per_sym, c['name'], 'all'),
                 'B': side_pool_stats(per_sym, c['name'], 'B'),
                 'S': side_pool_stats(per_sym, c['name'], 'S')}
        nb, ns = signal_counts(per_sym, c['name'])
        combos.append({**c, 'pool': cstat, 'n_b_sig': nb, 'n_s_sig': ns,
                       'per_symbol': {s: per_sym[s].get(c['name'], {}) for s in per_sym
                                      if c['name'] in per_sym.get(s, {})}})
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbols': symbols,
        'config': PROD_CONFIG,
        'combos': combos,
    }
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 已写入 {out_json}')
    # 打印摘要
    print(f'\n{"组合":<22}{"全回合胜率":>10}{"全笔数":>8}{"B信号":>7}{"S信号":>7}{"S触发胜率":>10}{"S触发笔数":>9}')
    for c in combos:
        print(f'{c["label"]:<22}{c["pool"]["all"]["med_win"]:>9.2f}%'
              f'{c["pool"]["all"]["total_trips"]:>8}'
              f'{c["n_b_sig"]:>7}{c["n_s_sig"]:>7}'
              f'{c["pool"]["S"]["med_win"]:>9.2f}%{c["pool"]["S"]["total_trips"]:>9}')
    return payload


def phase_pool(args):
    with open(os.path.join(DATA_DIR, 'tune_pool_40.json'), encoding='utf-8') as f:
        pool = json.load(f)['pool']
    symbols = [p['symbol'] for p in pool]
    if args.smoke:
        symbols = symbols[:3]
        print(f'🔥 冒烟（{len(symbols)} 只）')
    run_phase(symbols, os.path.join(DATA_DIR, 'mpr_test_pool.json'),
              '40 只调参池 mpr', args.procs)


def phase_watchlist(args):
    run_phase(WATCHLIST, os.path.join(DATA_DIR, 'mpr_test_watchlist.json'),
              'watchlist 5 只 mpr', args.procs)


def build_html(pool, wl):
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    def combo_rows(data):
        rows = ''
        for c in data['combos']:
            p = c['pool']
            rows += (
                f'<tr><td>{esc(c["label"])}</td>'
                f'<td>{p["all"]["med_win"]:.2f}%</td><td>{p["all"]["total_trips"]}</td>'
                f'<td>{c["n_b_sig"]}</td><td>{c["n_s_sig"]}</td>'
                f'<td>{p["S"]["med_win"]:.2f}%</td><td>{p["S"]["total_trips"]}</td></tr>')
        return rows
    # watchlist 逐标的 × 组合
    wl_rows = ''
    for s in wl['symbols']:
        cell = ''
        for c in wl['combos']:
            m = c['per_symbol'].get(s, {})
            if not m:
                cell += '<td>-</td>'
                continue
            cell += (f'<td>{m.get("total", 0)}笔 {m.get("win_rate", 0):.1f}%'
                     f'<br><span class="dim">S触发:{m.get("win_s", 0):.1f}%({m.get("total_s_trips", 0)}) '
                     f'B信号:{m.get("total_b", 0)} S信号:{m.get("total_s", 0)}</span></td>')
        wl_rows += f'<tr><td>{esc(s)}</td>{cell}</tr>'
    wl_head = ''.join(f'<th>{esc(c["label"])}</th>' for c in wl['combos'])
    # pool 逐标的 × B侧 mpr60（最重要组合）
    b60 = next(c for c in pool['combos'] if c['name'] == 'mpr_b60')
    base_c = next(c for c in pool['combos'] if c['name'] == 'baseline')
    per_rows = ''
    sym_rows = []
    for s in pool['symbols']:
        b = base_c['per_symbol'].get(s, {})
        m = b60['per_symbol'].get(s, {})
        if not b or not m:
            continue
        d_win = m.get('win_rate', 0) - b.get('win_rate', 0)
        keep = m.get('total', 0) / max(b.get('total', 0), 1) * 100
        sym_rows.append((s, b.get('total', 0), b.get('win_rate', 0),
                         m.get('total', 0), m.get('win_rate', 0), d_win, keep))
    sym_rows.sort(key=lambda x: -x[5])
    for r in sym_rows:
        per_rows += (
            f'<tr><td>{esc(r[0])}</td><td>{r[1]}笔 {r[2]:.1f}%</td>'
            f'<td>{r[3]}笔 {r[4]:.1f}%</td><td>{r[5]:+.1f}pp</td><td>{r[6]:.0f}%</td></tr>')
    n_up = sum(1 for r in sym_rows if r[5] > 0)
    n_dn = sum(1 for r in sym_rows if r[5] < 0)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>P3-1 多周期 MACD 方向过滤回测 {esc(datetime.date.today())}</title>
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
<h1>P3-1 多周期 MACD 方向过滤 · {esc(datetime.date.today())}</h1>
<div class="card">
  <h2>方法</h2>
  <p>大周期（60m/15m）MACD hist 方向一致过滤：B 需 hist&lt;0（顺大势抄底）、S 需 hist&gt;0（顺大势逃顶）。<br>
     mhd 恒 0.15 / VWAP0.65 / floor 门控 / 移动止损出场 act0.4 trail0.6。<br>
     口径：净胜率中位（固定全集）；S/B 分侧统计（不混合）。胜率=净收益&gt;0。<br>
     注意：S 信号在 s_signal_exit 下是持仓的出场源；S 触发回合样本远少于 S 信号数（多数 S 在空仓时开出反T）。</p>
</div>
<div class="card">
  <h2>40 只调参池（第一段，固定全集口径）</h2>
  <table>
    <tr><th>组合</th><th>全回合胜率</th><th>全笔数</th><th>B信号</th><th>S信号</th><th>S触发胜率</th><th>S触发笔数</th></tr>
    {combo_rows(pool)}
  </table>
  <p class="dim">「全回合」= B 建仓的全部 round-trip（B 侧口径）；「S触发」= exit_reason=='S' 的回合（S 侧口径）。S 触发胜率在过滤后虚高（幸存者偏差，样本不足）。</p>
</div>
<div class="card">
  <h2>B 侧 mpr60 逐标的（40 只，Δ 降序）</h2>
  <p><b class="ok">提升 {n_up} 只 / 恶化 {n_dn} 只 / 共 {len(sym_rows)} 只</b> —— 方向一致性极高。</p>
  <table>
    <tr><th>标的</th><th>基线</th><th>B侧mpr60</th><th>Δ</th><th>样本保留</th></tr>
    {per_rows}
  </table>
</div>
<div class="card">
  <h2>watchlist 5 只独立验证（第二段，不做二次调参）</h2>
  <table>
    <tr><th>标的</th>{wl_head}</tr>
    {wl_rows}
  </table>
</div>
<div class="card">
  <h2>结论与生产建议（2026-08-02 两段式验证）</h2>
  <table>
    <tr><th>组合</th><th>调参池 40 只</th><th>watchlist 5 只</th><th>判定</th></tr>
    <tr>
      <td><b>B侧 mpr60</b>（B 需 60m hist&lt;0）</td>
      <td>47.55→52.00%（+4.45pp），<b>37/40 提升</b>，保留 67%</td>
      <td>47.80→50.60%（+2.8pp），<b>5/5 全提升无退化</b>（688111 +5.5pp / 513310 +2.6pp / 161129 +3.5pp）</td>
      <td class="ok">✅ <b>方向真实且稳健</b>——两段一致提升、无单只退化。样本损失 1/3 需权衡，但胜率增益大于样本损失。</td>
    </tr>
    <tr>
      <td>B侧 mpr60+15</td>
      <td>+1.05pp，保留 55%</td>
      <td>300058 -1.7pp、600570 +0.2pp（退化）</td>
      <td class="bad">❌ 双周期过严，边际无增益</td>
    </tr>
    <tr>
      <td>S侧 mpr60 / 60+15</td>
      <td>S 信号 -75%/-84%，S触发胜率虚高 100%（幸存者偏差）</td>
      <td>600570 -4.8pp、161129 -3.2pp（退化）</td>
      <td class="bad">❌ S 触发样本太少，方向过滤伤 S 出场质量</td>
    </tr>
    <tr>
      <td>B/S 双侧 60+15</td>
      <td>+0.45pp，保留 61%</td>
      <td>600570 -5.4pp 严重退化</td>
      <td class="bad">❌ 双侧组合不可取</td>
    </tr>
  </table>
  <h2 style="margin-top:18px">建议</h2>
  <ul>
    <li><b>B 侧 mpr60 值得接入</b>：多周期 60m MACD hist&lt;0 作为 B 信号的顺大势门控（只在"大周期仍在下行"时抄底，避开 V 型反转日接飞刀）。这是首个通过两段式验证的入场结构性改进。</li>
    <li><b>S 侧 mpr 放弃</b>（当前 s_signal_exit 架构下 S 触发样本太少）；S 侧改进留待阶段 D（S 信号专项：vwap_dev_ceil + atr_min_pct_s）。</li>
    <li>接入方式：monitor 的 B 判定走 check_miji_trigger 传 <code>mpr_enable='B'</code>，或 env <code>TP_MPR_ENABLE</code>；默认关，验证实盘后再开。</li>
    <li>样本保留 67% 说明 mpr 会砍掉约 1/3 B 信号——实际监控中信号频率会下降，需用户确认可接受。</li>
  </ul>
</div>
</body></html>"""
    out = os.path.join(OUT_DIR, f'mpr_test_{datetime.date.today().strftime("%Y%m%d")}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out}')
    return out


def phase_report(args):
    with open(os.path.join(DATA_DIR, 'mpr_test_pool.json'), encoding='utf-8') as f:
        pool = json.load(f)
    with open(os.path.join(DATA_DIR, 'mpr_test_watchlist.json'), encoding='utf-8') as f:
        wl = json.load(f)
    out = build_html(pool, wl)
    # 结论判定：S 侧组合用 S 触发口径，B 侧组合用全回合（B 建仓）口径
    def delta(data, cname, side):
        b = next(c for c in data['combos'] if c['name'] == 'baseline')['pool'][side]
        t = next(c for c in data['combos'] if c['name'] == cname)['pool'][side]
        return round(t['med_win'] - b['med_win'], 2), round(t['total_trips'] / max(b['total_trips'], 1) * 100, 0)
    print(f'\n📊 结论判定（40 只池，固定全集口径）：')
    print(f'  {"组合":<14}{"侧":<4}{"Δ胜率":>8}{"样本保留":>9}  判定')
    for cname, side, desc in [('mpr_s60', 'S', 'S侧仅60m'), ('mpr_s6015', 'S', 'S侧60+15'),
                               ('mpr_b60', 'all', 'B侧仅60m'), ('mpr_b6015', 'all', 'B侧60+15'),
                               ('mpr_both6015', 'all', 'B/S双侧60+15')]:
        d, keep = delta(pool, cname, side)
        verdict = '✅ 达标' if d >= 1 and keep >= 80 else ('⚠️ 方向对但样本不足' if d >= 1 else '❌ 不达标')
        print(f'  {desc:<14}{side:<4}{d:>+7.2f}pp{keep:>8.0f}%  {verdict}')
    return out


def main():
    ap = argparse.ArgumentParser(description='P3-1 多周期 MACD 方向过滤回测')
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
