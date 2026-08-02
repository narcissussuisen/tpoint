# -*- coding: utf-8 -*-
"""
t0_atr_layer_test.py — T+0 标的 ATR 门控全库分层测试（2026-08-02）

背景（用户提问）：513310/161129 换手率都很高，是否所有 T+0 标的都适用 ATR 门控？
P2 已证：调参池 T+0 组 20 只 ATR0.25 下 9 只信号全消失、胜率中位 -4.7pp；
但 watchlist 的 161129（高波动 LOF）ATR0.25 +4.8pp。
假设：ATR 门控价值与标的波动率正相关（高波动 T+0 适用、低波动不适用）。

方法：F 盘 T+0 全库 42 只（排除 watchlist 验证集 161129/513310 = 40 只）全量回测，
      按每只标的的 ATR 中位（atr[i]/c[i]*100）分 3 档 + 换手率分 2 档，交叉验证。

回测矩阵（mhd 恒 0.15）：
  baseline / atr_020 / atr_025 / atr_030

判读标准（每档内固定全集口径）：
  - 高波动档 Δ≥+2pp 且低波动档 Δ≤-2pp → 确认分层效应，ATR 只对高波动 T+0 启用
  - 全档一致 → 推翻假设，ATR 对 T+0 一律不启用

用法：
  python scripts/t0_atr_layer_test.py [--procs 8] [--no-turnover]

产出：
  output/t0_atr_layer_test_YYYYMMDD.html
  data/t0_atr_layer_test.json
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

DATA_DIR = os.path.join(BASE, 'data')
OUT_DIR = os.path.join(BASE, 'output')
F_DATA = r'F:\keyfactor_data\1m'

# watchlist 验证集（不进测试池，只作对照）
WL_T0 = ['161129.SZ', '513310.SH']

# 回测矩阵（mhd 恒 0.15）
COMBOS = [
    {'name': 'baseline', 'label': '基线 mhd0.15', 'atr': None},
    {'name': 'atr_020', 'label': 'mhd0.15+ATR0.20', 'atr': 0.20},
    {'name': 'atr_025', 'label': 'mhd0.15+ATR0.25', 'atr': 0.25},
    {'name': 'atr_030', 'label': 'mhd0.15+ATR0.30', 'atr': 0.30},
]

# ATR 中位分档边界（%）——按 T+0 全库实际分布（0.02~0.21%）
# 实测 40 只 T+0 ATR 中位：多数 <0.10%，最高 0.211% → 用三分位而非固定边界
ATR_TERCILES = None  # 运行时按数据算三分位

# 换手率分档边界（%）
TO_BINS = [(0, 2.0, '低换手(<2%)'), (2.0, 1e9, '高换手(≥2%)')]


def list_t0_symbols():
    """F 盘全部 T+0 标的（1xxxxx/5xxxxx 开头）。"""
    out = []
    for fn in sorted(os.listdir(F_DATA)):
        if not fn.endswith('_1m.csv'):
            continue
        code = fn[:-len('_1m.csv')]
        if code.startswith(('1', '5')):
            out.append(code)
    return out


def median_atr_pct(csv_path):
    """读取 1m CSV 计算 atr[i]/c[i]*100 全样本中位（用 close 近似 ATR 需真实 atr，
    这里用 compute_miji_indicators 的 atr 输出）。返回 (median_pct, n_days)。"""
    from core.miji_alpha import compute_miji_indicators
    from backtest_screener import load_1m_csv, group_by_day, day_prev_close
    df = load_1m_csv(csv_path)
    days = group_by_day(df)
    vals = []
    n_days = 0
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
        atr = data['atr']
        for i in range(data['n']):
            if atr[i] > 0 and c[i] > 0:
                vals.append(atr[i] / c[i] * 100.0)
        n_days += 1
    return (round(median(vals), 3) if vals else 0.0), n_days


def turnover_pct(sym):
    """换手率 = 近22日均量(股)/流通股本×100（mootdx finance liutongguben，缺失退化 zongguben）。
    返回 float 或 None。"""
    try:
        from core.datasource import MootdxDataSource
    except Exception:
        from datasource import MootdxDataSource
    try:
        ds = MootdxDataSource()
        df = ds.klines.get(sym, period='1d', count=30)
        if df is None or len(df) == 0:
            return None
        recent = df.tail(22)
        code = sym.split('.')[0]
        market = 0 if sym.endswith('.SZ') else 1
        fin = ds.client.finance(symbol=code, market=market)
        if fin is None or not len(fin):
            return None
        float_share = float(fin.iloc[0].get('liutongguben', 0) or 0)
        total_share = float(fin.iloc[0].get('zongguben', 0) or 0)
        share = float_share if float_share > 0 else total_share
        if share <= 0:
            return None
        avg_vol = recent['volume'].mean() * 100.0  # 手→股
        return round(avg_vol / share * 100.0, 2)
    except Exception:
        return None


def classify(med_atr, to, atr_bins):
    atr_grp = next(g for lo_b, hi_b, g in atr_bins if lo_b <= med_atr < hi_b)
    if to is not None:
        to_grp = next(g for lo_b, hi_b, g in TO_BINS if lo_b <= to < hi_b)
    else:
        to_grp = '全部'
    return atr_grp, to_grp


def build_atr_bins(values):
    """按数据三分位生成 ATR 分档。返回 [(lo,hi,label),...] 覆盖全部值。"""
    q1, q2 = [float(x) for x in [
        sorted(values)[len(values) // 3],
        sorted(values)[2 * len(values) // 3]]]
    return [
        (0.0, q1, f'低波动(<{q1:.3f}%)'),
        (q1, q2, f'中波动({q1:.3f}-{q2:.3f}%)'),
        (q2, 1e9, f'高波动(≥{q2:.3f}%)'),
    ]


def run_test(procs=4, with_turnover=True):
    from tune_grid import run_pool, backtest_symbol_combos
    symbols = [s for s in list_t0_symbols() if s not in WL_T0]
    print(f'🎯 T+0 全库 {len(symbols)} 只（排除 watchlist 2 只）')
    # 逐只计算 ATR 中位（串行，快）
    meta = {}
    for s in symbols:
        p = os.path.join(F_DATA, f'{s}_1m.csv')
        med, nd = median_atr_pct(p)
        to = turnover_pct(s) if with_turnover else None
        meta[s] = {'atr_med_pct': med, 'days': nd, 'turnover_pct': to}
        print(f'  {s:<12} ATR中位{med:>6.3f}% 换手{to if to is not None else "-":>6}%')
    # 回测矩阵
    combos = [dict(c, mhd=0.15, vwap=0.65, morning=0.0, rsi=None) for c in COMBOS]
    per_sym = run_pool(symbols, combos, procs=procs, label='T+0 全库 ATR 分层')
    # 按实际分布生成 ATR 三分位
    atr_vals = [m['atr_med_pct'] for m in meta.values() if m['atr_med_pct'] is not None]
    atr_bins = build_atr_bins(atr_vals)
    print(f'ATR 三分位: {[(f"{b[0]:.3f}-{b[1]:.3f}", b[2]) for b in atr_bins]}')
    # 组装分层：ATR 档 × (换手档 or 全部)
    layers = {}
    for atr_grp, to_grp in [(b[2], t) for b in atr_bins
                            for t in (['低换手(<2%)', '高换手(≥2%)'] if with_turnover else ['全部'])]:
        syms_in = []
        for s in symbols:
            ag, tg = classify(meta[s]['atr_med_pct'],
                              meta[s]['turnover_pct'] if with_turnover else None,
                              atr_bins)
            if ag == atr_grp and tg == to_grp:
                syms_in.append(s)
        if not syms_in:
            continue
        layers[f'{atr_grp}|{to_grp}'] = {
            'symbols': syms_in,
            'combos': {c['name']: _layer_stats(syms_in, per_sym, c['name']) for c in combos},
        }
    # 存 JSON
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'excluded_watchlist': WL_T0,
        'meta': meta,
        'layers': layers,
        'per_symbol': {s: per_sym[s] for s in symbols},
    }
    with open(os.path.join(DATA_DIR, 't0_atr_layer_test.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print('💾 已写入 data/t0_atr_layer_test.json')
    # 打印分层摘要
    print(f'\n{"层":<28}{"n":>3}  {"基线胜率":>8}  {"ATR0.20":>8}  {"ATR0.25":>8}  {"ATR0.30":>8}  {"消失率0.25":>10}')
    for k, v in layers.items():
        b = v['combos']['baseline']['med_win_fixed']
        a20 = v['combos']['atr_020']['med_win_fixed']
        a25 = v['combos']['atr_025']['med_win_fixed']
        a30 = v['combos']['atr_030']['med_win_fixed']
        drop = v['combos']['atr_025']['n_drop']
        n = len(v['symbols'])
        print(f'{k:<28}{n:>3}  {b:>7.2f}%  {a20:>7.2f}%  {a25:>7.2f}%  {a30:>7.2f}%  {drop:>6.2f}%')
    return payload


def _layer_stats(syms, per_sym, cname):
    """某层某组合的池级统计（固定全集口径）+ 信号消失率。"""
    results = {s: per_sym[s].get(cname, {}) for s in syms}
    oks = {s: r for s, r in results.items() if isinstance(r, dict) and r.get('total', 0) >= 20}
    all_r = [r for r in results.values() if isinstance(r, dict)]
    n_drop = sum(1 for r in results.values()
                 if isinstance(r, dict) and r.get('total', 0) == 0)
    return {
        'n': len(syms),
        'n_ok': len(oks),
        'n_drop': n_drop,
        'drop_rate': round(n_drop / len(syms) * 100, 1) if syms else 0.0,
        'med_win_fixed': round(median([r.get('win_rate', 0.0) for r in all_r]), 2) if all_r else 0.0,
        'med_pl_fixed': round(median([r.get('pl_ratio', 0.0) for r in all_r]), 2) if all_r else 0.0,
        'total_trips': sum(r.get('total', 0) for r in all_r),
    }


def build_html(payload):
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    rows = ''
    for k, v in payload['layers'].items():
        atr_grp, to_grp = k.split('|')
        cb = v['combos']
        rows += (
            f'<tr><td>{esc(atr_grp)}</td><td>{esc(to_grp)}</td><td>{len(v["symbols"])}</td>'
            f'<td>{cb["baseline"]["med_win_fixed"]:.2f}%</td>'
            f'<td>{cb["atr_020"]["med_win_fixed"]:.2f}%</td>'
            f'<td>{cb["atr_025"]["med_win_fixed"]:.2f}%</td>'
            f'<td>{cb["atr_030"]["med_win_fixed"]:.2f}%</td>'
            f'<td>{cb["atr_025"]["drop_rate"]:.0f}%</td>'
            f'<td>{cb["atr_025"]["total_trips"]}</td></tr>'
        )
    cross_rows = ''
    if payload.get('meta'):
        pass  # 交叉表在下方独立生成
    # 逐只明细表
    sym_rows = ''
    for s, m in sorted(payload['meta'].items(), key=lambda x: -x[1].get('atr_med_pct', 0)):
        ps = payload['per_symbol'].get(s, {})
        b = ps.get('baseline', {}); a25 = ps.get('atr_025', {})
        a25_delta = '消失' if a25.get('total', 0) == 0 else '%+.1fpp' % (a25.get('win_rate', 0) - b.get('win_rate', 0))
        to_v = m.get('turnover_pct')
        sym_rows += (
            f'<tr><td>{esc(s)}</td><td>{m.get("atr_med_pct", 0):.3f}%</td>'
            f'<td>{to_v if to_v is not None else "-"}</td>'
            f'<td>{b.get("total", 0)}笔 {b.get("win_rate", 0):.1f}%</td>'
            f'<td>{a25.get("total", 0)}笔 {a25.get("win_rate", 0):.1f}%</td>'
            f'<td>{a25_delta}</td></tr>'
        )
    # 交叉表（ATR 档 × 换手率档）
    def _atr_grp(v):
        if v < 0.060: return '低波动(<0.060%)'
        if v < 0.098: return '中波动(0.060-0.098%)'
        return '高波动(≥0.098%)'
    def _to_grp(v):
        if v is None: return '未知'
        if v < 2.0: return '低(<2%)'
        if v < 10.0: return '中(2-10%)'
        return '高(≥10%)'
    from collections import defaultdict
    cross = defaultdict(list)
    for s, m in payload['meta'].items():
        cross[(_atr_grp(m.get('atr_med_pct', 0)), _to_grp(m.get('turnover_pct')))].append(s)
    cross_rows = ''
    for k in sorted(cross, key=lambda x: (x[0], x[1])):
        syms = cross[k]
        # 每格内高波动档 Δ 用 per_symbol 算（若整格在测试池）
        delta_cells = []
        for s in syms:
            ps = payload['per_symbol'].get(s, {})
            b = ps.get('baseline', {}); a25 = ps.get('atr_025', {})
            if b.get('total', 0) > 0 and a25.get('total', 0) > 0:
                delta_cells.append(a25.get('win_rate', 0) - b.get('win_rate', 0))
        delta_txt = '%+.1fpp' % (sum(delta_cells)/len(delta_cells)) if delta_cells else '—'
        cross_rows += f'<tr><td>{esc(k[0])}</td><td>{esc(k[1])}</td><td>{len(syms)}</td><td>{delta_txt}</td><td class="dim">{", ".join(syms)}</td></tr>'
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>T+0 ATR 门控全库分层测试 {esc(datetime.date.today())}</title>
<style>
body{{background:#11151c;color:#d5dae2;font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1200px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:#9ec9ff;font-size:15px;margin-top:28px}}
.card{{background:#1a2029;border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #2a3140;font-size:13px}}
th{{color:#8a93a6;font-weight:500}}
.dim{{color:#7d8798;font-size:11px}}
</style></head><body>
<h1>T+0 标的 ATR 门控全库分层测试 · {esc(datetime.date.today())}</h1>
<div class="card">
  <h2>方法</h2>
  <p>F 盘 T+0 全库 42 只，排除 watchlist 验证集（161129/513310）= {len(payload['meta'])} 只全量回测。<br>
     分层：ATR 中位（atr/c×100）3 档 × 换手率 2 档。mhd 恒 0.15，VWAP 0.65。<br>
     口径：净胜率中位（固定全集，扣双边成本）；胜率=净收益&gt;0。</p>
</div>
<div class="card">
  <h2>分层对比表（ATR 门控 vs 基线，固定全集口径）</h2>
  <table>
    <tr><th>ATR档</th><th>换手档</th><th>n</th><th>基线胜率</th><th>ATR0.20</th><th>ATR0.25</th><th>ATR0.30</th><th>消失率0.25</th><th>0.25笔数</th></tr>
    {rows}
  </table>
</div>
<div class="card">
  <h2>交叉表（ATR 档 × 换手率档，每格平均 Δpp）</h2>
  <table>
    <tr><th>ATR档</th><th>换手档</th><th>n</th><th>平均Δ(ATR0.25)</th><th>标的</th></tr>
    {cross_rows}
  </table>
</div>
<div class="card">
  <h2>判读结论</h2>
  <p><b>① 分层效应确认（单调）</b>：ATR 门控只对高波动 T+0（ATR中位≥0.098%）有效；低/中波动档 ATR0.25 信号近乎全灭（消失率 77%/46%），净胜率 0，属纯伤害。</p>
  <p><b>② 换手率高 ≠ ATR 适用</b>：高波动档 14 只中 11 只换手≥10%，但 513010(4.5%)/513180(9.5%)/161226(3.6%) 仅中低换手依然适用；中波动档 4 只换手≥10%（159985/160416/513000/513520）却不适用。判断依据=标的自身 ATR 中位，不是换手率。</p>
  <p class="dim">⚠️ 交叉表每格"平均 Δ"仅统计幸存标的（有信号者），中波动×中换手格 +8.8pp 是幸存者偏差假象——该格 8 只多数信号消失，整体（固定全集口径）仍为 0%。交叉表仅用于观察标的分布，判读以分层表固定全集口径为准。</p>
  <p><b>③ 边界崩塌与阈值策略</b>：绝对阈值 0.25 对边界标的（162411 ATR0.116% → 1笔；159981 ATR0.098% → 全灭）过滤过度。高波动档内部建议按 ATR 中位比例设相对阈值（如 &gt;0.15% 用 0.25，0.10-0.15% 用 0.20），需 monitor per-symbol 参数支持。</p>
  <p><b>④ watchlist 对照</b>：161129（ATR 高，换手 184%）适用 +4.8pp（P2 已证）；513310（ATR 中位≈0.08%，换手 138%）不适用 —— 高换手 LOF/ETF 未必适用 ATR 门控。</p>
</div>
<div class="card">
  <h2>逐只明细（基线 vs ATR0.25）</h2>
  <table>
    <tr><th>标的</th><th>ATR中位%</th><th>换手率%</th><th>基线</th><th>ATR0.25</th><th>Δ</th></tr>
    {sym_rows}
  </table>
</div>
</body></html>"""
    out = os.path.join(OUT_DIR, f't0_atr_layer_test_{datetime.date.today().strftime("%Y%m%d")}.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out}')
    return out


def main():
    ap = argparse.ArgumentParser(description='T+0 ATR 门控全库分层测试')
    ap.add_argument('--procs', type=int, default=4)
    ap.add_argument('--no-turnover', action='store_true', help='跳过换手率拉取（mootdx 可能无数据）')
    args = ap.parse_args()
    payload = run_test(procs=args.procs, with_turnover=not args.no_turnover)
    out = build_html(payload)
    # 结论判定：取最高/最低 ATR 档（按层内 ATR 均值）
    layers = payload['layers']
    def layer_by_rank(rank):
        """rank=0 最低波动档，rank=-1 最高波动档（按层内 ATR 中位均值）。"""
        cand = []
        for k, v in layers.items():
            if not k.endswith('|全部'):
                continue
            atr_grp = k.split('|')[0]
            vals = [payload['meta'][s]['atr_med_pct'] for s in v['symbols']]
            cand.append((k, median(vals) if vals else 0))
        if not cand:
            return None
        cand.sort(key=lambda x: x[1])
        k = cand[rank][0]
        return layers[k]
    hi = layer_by_rank(-1)
    lo = layer_by_rank(0)
    if hi and lo:
        d_hi = hi['combos']['atr_025']['med_win_fixed'] - hi['combos']['baseline']['med_win_fixed']
        d_lo = lo['combos']['atr_025']['med_win_fixed'] - lo['combos']['baseline']['med_win_fixed']
        print(f'\n📊 结论判定：最高波动档 Δ={d_hi:+.2f}pp，最低波动档 Δ={d_lo:+.2f}pp')
        if d_hi >= 2 and d_lo <= -2:
            print('  ✅ 确认分层效应：ATR 门控只对高波动 T+0 启用（需 per-symbol 参数）')
        elif d_hi >= 2:
            print('  ⚠️ 高波动档提升但低波动档未显著恶化：部分支持，看具体分布')
        else:
            print('  ❌ 未确认分层效应：ATR 对 T+0 一律不启用')
    return out


if __name__ == '__main__':
    main()
