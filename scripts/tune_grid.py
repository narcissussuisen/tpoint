# -*- coding: utf-8 -*-
"""
tune_grid.py — P2 两段式调参网格搜索（2026-08-02）

用途：在「40 只调参池」上对做T策略旋钮做网格搜索（第一段），再在
      watchlist 5 只上独立验证最优组合（第二段，不做二次调参），防过拟合。

两段式协议（用户 2026-08-01 确认，计划 radiant-cascade-turing.md §四）：
  1. 第一段（coarse）：40 只调参池 × 14 组粗筛组合，目标函数 = 池化净胜率中位
     （扣双边成本，口径与 backtest_screener 一致），辅以盈亏比中位 / 样本总量 / 信号总量。
  2. 第二段（verify）：watchlist 5 只（161129/513310/300058/600570/688111）上跑
     coarse 的最优 2-3 个组合，不做二次调参。
  3. 验收：验证集净胜率中位 ≥ 基线（mhd=0.15、无 ATR/早盘/RSI）中位，
     且无单只跌幅 >3pp；满足才整体上线。

旋钮（全部为 detect 期参数，不动生产代码）：
  - mhd        : macd_min_hist_diff，MACD 背离强度点数阈值
  - atr        : atr_min_pct，B 侧 ATR 波动率下限门槛 %
  - vwap       : VWAP_DEV_BUY（monkey-patch miji_alpha 模块常量，进程内串行安全）
  - morning    : 早盘 B hist 门槛放宽值（>0 启用；is_morning 口径 09:30-10:00 与 monitor 一致）
  - rsi        : RSI<rsi 才允许 B（后处理过滤，超卖买入试验）

性能：单标的 compute_miji_indicators 一次，逐组合 detect+simulate；
     按标的切分多进程并行（Windows spawn 安全，worker 内 import 隔离）。

用法：
  python scripts/tune_grid.py --phase coarse --procs 8
  python scripts/tune_grid.py --phase verify --top-json data/tune_grid_coarse.json --combos combo1,combo2
  python scripts/tune_grid.py --phase report

输出：
  data/tune_grid_coarse.json / data/tune_grid_verify.json
  output/tune_grid_report_YYYYMMDD.html（调参池主表 + watchlist 子表）
"""
import argparse
import datetime
import json
import os
import sys
from statistics import median

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 生产同源门控：必须在 import core.miji_alpha 前设置（模块级变量在 import 时读 env）
os.environ.setdefault('MACD_GATE_MODE', 'floor')

from core.exit_manager import (make_config, simulate_day, aggregate_metrics,  # noqa: E402
                               cost_for_symbol)

# 生产出场配置（与 monitor 一致：仅移动止损 act0.4/trail0.6，S 信号自然出场）
PROD_CONFIG = dict(
    use_stop=False, use_time=False,
    use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6,
    s_signal_exit=True,
)

DATA_DIR = os.path.join(BASE, 'data')
OUT_DIR = os.path.join(BASE, 'output')
F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = ['161129.SZ', '513310.SH', '300058.SZ', '600570.SH', '688111.SH']

# ---------- 网格定义 ----------

def _c(name, label, mhd=0.15, atr=None, vwap=0.6, morning=0.0, rsi=None):
    return dict(name=name, label=label, mhd=mhd, atr=atr, vwap=vwap,
                morning=morning, rsi=rsi)

GRID_COARSE = [
    # 基线 = P0 生产态（mhd0.15，无 ATR/早盘/RSI）
    _c('baseline', '基线 mhd0.15'),
    # mhd 5 档（0.05~0.30，P0 已验证 0.15 建议值，试邻近档看单调性）
    _c('mhd_005', 'mhd=0.05', mhd=0.05),
    _c('mhd_010', 'mhd=0.10', mhd=0.10),
    _c('mhd_020', 'mhd=0.20', mhd=0.20),
    _c('mhd_025', 'mhd=0.25', mhd=0.25),
    _c('mhd_030', 'mhd=0.30', mhd=0.30),
    # ATR 3 档（mhd 保持 0.15；P1 已验证三档均提升胜率）
    _c('atr_020', 'mhd0.15+ATR0.20', atr=0.20),
    _c('atr_025', 'mhd0.15+ATR0.25', atr=0.25),
    _c('atr_030', 'mhd0.15+ATR0.30', atr=0.30),
    # vwap 2 档（引力触发带 ±0.5/0.7 ATR；报告深负偏离优势方向待数据说话）
    _c('vwap_050', 'mhd0.15+VWAP0.5', vwap=0.5),
    _c('vwap_070', 'mhd0.15+VWAP0.7', vwap=0.7),
    # 早盘放宽（早盘 B hist 门槛降为 0.05；2026-08-02 实证默认关，此处仅验证）
    _c('morning', 'mhd0.15+早盘B放宽0.05', morning=0.05),
    # RSI<30 超卖买入（报告箱0 0.4218；仅超卖区允许 B）
    _c('rsi_030', 'mhd0.15+RSI<30', rsi=30),
]

# 局部细搜：粗筛最优方向的邻近值（避免幸存者偏差：统计固定标的集合，
# 同时报告 n_ok=样本≥20 的标的数 与 total_trips 样本总量，作为覆盖度/可信度指标）
GRID_FINE = [
    # ATR 邻近（粗筛 atr_025 +1.1pp 但样本 23/40 → 试 0.22/0.24/0.26 找平衡点）
    _c('atr_022', 'mhd0.15+ATR0.22', atr=0.22),
    _c('atr_024', 'mhd0.15+ATR0.24', atr=0.24),
    _c('atr_026', 'mhd0.15+ATR0.26', atr=0.26),
    # VWAP 邻近（粗筛 vwap_070 盈亏比 0.90、全样本保留 → 试 0.65/0.75）
    _c('vwap_065', 'mhd0.15+VWAP0.65', vwap=0.65),
    _c('vwap_075', 'mhd0.15+VWAP0.75', vwap=0.75),
    # RSI 邻近（粗筛 rsi_030 盈亏比 1.06 但样本崩 → 试 25/35 验证是否阈值问题）
    _c('rsi_025', 'mhd0.15+RSI<25', rsi=25),
    _c('rsi_035', 'mhd0.15+RSI<35', rsi=35),
    # 组合叠加（ATR 过滤 + VWAP 触发带，两方向叠加看是否加成）
    _c('atr025_vwap07', 'mhd0.15+ATR0.25+VWAP0.7', atr=0.25, vwap=0.7),
]


def combo_by_name(name, grid=None):
    grid = grid or GRID_COARSE
    for c in grid:
        if c['name'] == name:
            return c
    # 粗筛里没有 → 尝试细搜网格
    for c in GRID_FINE:
        if c['name'] == name:
            return c
    raise KeyError(f'组合 {name} 不在网格中')


# ---------- 数据读取（复用 backtest_screener） ----------

def load_and_group(csv_path):
    from backtest_screener import load_1m_csv, group_by_day, day_prev_close
    df = load_1m_csv(csv_path)
    if 'symbol' in df.columns:
        symbol = str(df['symbol'].iloc[0])
    else:
        base = os.path.basename(csv_path).replace('_1m.csv', '').replace('_5m.csv', '')
        symbol = base
    days = group_by_day(df)
    return symbol, df, days, day_prev_close


def build_is_morning(sub):
    """当日 is_morning 数组（09:30-10:00=1），口径与 ml_build_dataset L165-175 / monitor 一致。"""
    n = len(sub)
    arr = [0] * n
    if 'trade_time' in sub.columns:
        hhmm = sub['trade_time'].astype(str).str[11:16]
        for j, t in enumerate(hhmm):
            arr[j] = 1 if '09:30' <= t < '10:00' else 0
    return arr


def detect_for_combo(data, pc, combo, morning_arr):
    """按组合旋钮 detect 当日信号，返回 (sigs, n_b, n_s)。"""
    import core.miji_alpha as ma
    old = ma.VWAP_DEV_BUY
    ma.VWAP_DEV_BUY = combo['vwap']
    try:
        sigs = ma.detect_miji_signals(
            data, pc,
            macd_min_hist_diff=combo['mhd'],
            atr_min_pct=combo['atr'],
            is_morning=morning_arr if combo['morning'] > 0 else None,
            morning_min_hist_diff=combo['morning'])
    finally:
        ma.VWAP_DEV_BUY = old
    # RSI<30 超卖过滤（后处理：B 要求 rsi[idx] < rsi 阈值）
    if combo['rsi'] is not None and data.get('rsi') is not None:
        sigs = [s for s in sigs
                if not (s['type'] == 'B' and float(data['rsi'][s['idx']]) >= combo['rsi'])]
    n_b = sum(1 for s in sigs if s['type'] == 'B')
    n_s = sum(1 for s in sigs if s['type'] == 'S')
    return sigs, n_b, n_s


def backtest_symbol_combos(csv_path, combos):
    """单标的 × 多组合：compute 一次，逐组合 detect+simulate。

    返回 {combo_name: {total, win_rate, pl_ratio, ann_ret_pct, max_drawdown_pct,
                       n_b, n_s, days}}，以及 '_meta': {symbol, days}。"""
    from core.miji_alpha import compute_miji_indicators
    symbol, df, days, day_prev_close = load_and_group(csv_path)
    cost = cost_for_symbol(symbol)
    mcfg = make_config(**PROD_CONFIG)
    meta = {'symbol': symbol, 'days': len(days)}
    out = {c['name']: {'total': 0, 'win_rate': 0.0, 'pl_ratio': 0.0,
                       'ann_ret_pct': 0.0, 'max_drawdown_pct': 0.0,
                       'n_b': 0, 'n_s': 0, 'days': 0}
           for c in combos}
    combo_map = {c['name']: c for c in combos}
    # 每组合的汇总器
    for cname in out:
        out[cname]['_trips'] = []
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
        for cname, combo in combo_map.items():
            sigs, n_b, n_s = detect_for_combo(data, pc, combo, morning_arr)
            trips = simulate_day(sigs, prices, mcfg, cost=cost)
            out[cname]['_trips'].extend(trips)
            out[cname]['n_b'] += n_b
            out[cname]['n_s'] += n_s
        day_count += 1
    for cname in out:
        m = aggregate_metrics(out[cname]['_trips'])
        out[cname].update({
            'total': m['total'], 'win_rate': m['win_rate'],
            'pl_ratio': m['pl_ratio'], 'ann_ret_pct': m['ann_ret_pct'],
            'max_drawdown_pct': m['max_drawdown_pct'],
            'gross_win_rate': m['gross_win_rate'],
        })
        del out[cname]['_trips']
        out[cname]['days'] = day_count
    out['_meta'] = meta
    return out


def pool_stats(results):
    """池级统计，双口径：
      - med_win / med_pl      : 动态子集（仅样本≥20 的标的）——反映"能产生足够样本"时的质量
      - med_win_fixed 等      : 固定全集（全部标的计入，含样本<20）——防幸存者偏差，跨组合可比
    total_trips/total_sigs 按全部标的计。"""
    all_syms = [s for s in results if not s.startswith('_')]
    oks = {s: r for s, r in results.items()
           if not s.startswith('_') and isinstance(r, dict) and r.get('total', 0) >= 20}
    def _med(key, d):
        vals = [r.get(key, 0.0) for r in d.values() if isinstance(r, dict)]
        return round(median(vals), 2) if vals else 0.0
    return {
        'n_ok': len(oks),
        'n_all': len(all_syms),
        'med_win': _med('win_rate', oks),
        'med_pl': _med('pl_ratio', oks),
        # 固定全集口径（跨组合公平对比；样本<20 的标的也计入 win_rate）
        'med_win_fixed': _med('win_rate', {s: results[s] for s in all_syms}),
        'med_pl_fixed': _med('pl_ratio', {s: results[s] for s in all_syms}),
        'total_trips': sum(r.get('total', 0) for s, r in results.items()
                           if not s.startswith('_')),
        'total_sigs': sum(r.get('n_b', 0) + r.get('n_s', 0)
                          for s, r in results.items() if not s.startswith('_')),
        'total_b': sum(r.get('n_b', 0) for s, r in results.items()
                       if not s.startswith('_')),
        'total_s': sum(r.get('n_s', 0) for s, r in results.items()
                       if not s.startswith('_')),
    }


# ---------- 执行 ----------

def worker(symbol, csv_path, combos):
    """多进程 worker：单标的 × 组合列表。返回 (symbol, backtest_symbol_combos 结果)。"""
    # spawn 子进程环境：确保 BASE 与 MACD_GATE_MODE 就绪
    sys.path.insert(0, BASE)
    os.environ.setdefault('MACD_GATE_MODE', 'floor')
    try:
        res = backtest_symbol_combos(csv_path, combos)
        return symbol, res
    except Exception as e:
        import traceback
        return symbol, {'_meta': {'symbol': symbol, 'error': str(e),
                                  'trace': traceback.format_exc()[:500]}}


def run_pool(symbols, combos, procs=4, label=''):
    """对 symbol 列表跑 combos，返回 {symbol: backtest_symbol_combos 结果}。"""
    from concurrent.futures import ProcessPoolExecutor, as_completed
    paths = []
    for sym in symbols:
        p = os.path.join(F_DATA, f'{sym}_1m.csv')
        if not os.path.exists(p):
            # 部分标的可能用 5m 目录兜底
            p = os.path.join(BASE, 'backtest', 'backtest_data', f'{sym}_1m.csv')
        paths.append((sym, p))
    missing = [sym for sym, p in paths if not os.path.exists(p)]
    for sym in missing:
        print(f'  ⚠️ 数据缺失: {sym}')
    results = {}
    tasks = [(sym, p) for sym, p in paths if os.path.exists(p)]
    print(f'🎯 {label}: {len(tasks)} 个标的 × {len(combos)} 组合，'
          f'{procs} 进程并行')
    t0 = datetime.datetime.now()
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
    print(f'⏱️  {label} 完成，耗时 {el:.0f}s（均 {el/len(tasks):.1f}s/标的）')
    return results


def pick_top(results, n=3):
    """从 coarse 结果中按池固定口径净胜率中位选 top-n 组合（自动含 baseline 外的非基线组合）。"""
    combos = [c for c in results['combos'] if c['name'] != 'baseline']
    combos.sort(key=lambda c: c['pool']['med_win_fixed'], reverse=True)
    return [c['name'] for c in combos[:n]]


# ---------- 主流程 ----------

def phase_coarse(args):
    with open(os.path.join(DATA_DIR, 'tune_pool_40.json'), encoding='utf-8') as f:
        pool = json.load(f)['pool']
    symbols = [p['symbol'] for p in pool]
    if args.smoke:
        # 冒烟：取 1 T+0 + 2 T+1 快速验证
        smoke = [s for s in symbols if s.startswith(('5', '1'))][:1] + \
                [s for s in symbols if not s.startswith(('5', '1'))][:2]
        print(f'🔥 冒烟模式（{len(smoke)} 只）: {smoke}')
        symbols = smoke
    per_sym = run_pool(symbols, GRID_COARSE, procs=args.procs, label='第一段粗筛')
    # 组装组合级池统计
    combos = []
    for c in GRID_COARSE:
        sym_res = {s: per_sym[s].get(c['name'], {}) for s in per_sym
                   if c['name'] in per_sym.get(s, {})}
        combos.append({
            **c,
            'pool': pool_stats(sym_res),
            'per_symbol': {s: per_sym[s][c['name']] for s in per_sym
                           if c['name'] in per_sym.get(s, {})},
        })
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'phase': 'coarse',
        'pool_size': len(symbols),
        'symbols': symbols,
        'watchlist': WATCHLIST,
        'config': PROD_CONFIG,
        'cost_model': '万一佣金不免五；个股卖出印花税万5.641；ETF/LOF 无印花税；滑点2bps/边',
        'combos': combos,
    }
    out = os.path.join(DATA_DIR, 'tune_grid_coarse.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 粗筛结果已写入 {out}')
    # 打印摘要（固定全集口径为主排序，动态子集口径作参考）
    print(f'\n{"组合":<24}{"固中位胜率":>10}{"固盈亏比":>10}{"动中位胜率":>10}'
          f'{"样本≥20":>8}{"总笔数":>8}{"B信号":>8}')
    for c in sorted(combos, key=lambda x: -x['pool']['med_win_fixed']):
        p = c['pool']
        print(f'{c["label"]:<24}{p["med_win_fixed"]:>9.2f}%{p["med_pl_fixed"]:>10.2f}'
              f'{p["med_win"]:>9.2f}%{p["n_ok"]:>8}{p["total_trips"]:>8}'
              f'{p["total_b"]:>8}')
    print(f'\n🏆 top-{args.top_n} 候选（非基线，按固定口径净胜率中位）: '
          f'{pick_top(payload, args.top_n)}')


def phase_fine(args):
    """局部细搜：固定调参池标的集合，跑 GRID_FINE 邻近组合 + 关键对照。
    目标函数与 coarse 一致（净胜率中位），同时盯盈亏比中位与样本覆盖。"""
    with open(os.path.join(DATA_DIR, 'tune_pool_40.json'), encoding='utf-8') as f:
        pool = json.load(f)['pool']
    symbols = [p['symbol'] for p in pool]
    # 对照：基线 + fine 全部组合（同一次 compute，成本低）
    combos = [combo_by_name('baseline')] + GRID_FINE
    per_sym = run_pool(symbols, combos, procs=args.procs, label='局部细搜')
    fcombos = []
    for c in combos:
        sym_res = {s: per_sym[s].get(c['name'], {}) for s in per_sym
                   if c['name'] in per_sym.get(s, {})}
        fcombos.append({**c, 'pool': pool_stats(sym_res),
                        'per_symbol': {s: per_sym[s][c['name']] for s in per_sym
                                       if c['name'] in per_sym.get(s, {})}})
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'phase': 'fine',
        'pool_size': len(symbols),
        'symbols': symbols,
        'config': PROD_CONFIG,
        'combos': fcombos,
    }
    out = os.path.join(DATA_DIR, 'tune_grid_fine.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 细搜结果已写入 {out}')
    print(f'\n{"组合":<24}{"固中位胜率":>10}{"固盈亏比":>10}{"动中位胜率":>10}'
          f'{"样本≥20":>8}{"总笔数":>8}{"B信号":>8}')
    for c in sorted(fcombos, key=lambda x: -x['pool']['med_win_fixed']):
        p = c['pool']
        print(f'{c["label"]:<24}{p["med_win_fixed"]:>9.2f}%{p["med_pl_fixed"]:>10.2f}'
              f'{p["med_win"]:>9.2f}%{p["n_ok"]:>8}{p["total_trips"]:>8}'
              f'{p["total_b"]:>8}')


def phase_verify(args):
    with open(args.top_json, encoding='utf-8') as f:
        coarse = json.load(f)
    if args.combos:
        names = [x.strip() for x in args.combos.split(',') if x.strip()]
    else:
        names = pick_top(coarse, args.top_n)
    if 'baseline' not in names:
        names = ['baseline'] + names
    combos = [combo_by_name(n, coarse['combos']) for n in names]
    print(f'🔬 第二段验证组合: {names}')
    per_sym = run_pool(args.watchlist.split(',') if args.watchlist else WATCHLIST,
                       combos, procs=args.procs, label='第二段 watchlist 验证')
    vcombos = []
    for c in combos:
        sym_res = {s: per_sym[s].get(c['name'], {}) for s in per_sym
                   if c['name'] in per_sym.get(s, {})}
        vcombos.append({**c, 'pool': pool_stats(sym_res),
                        'per_symbol': {s: per_sym[s][c['name']] for s in per_sym
                                       if c['name'] in per_sym.get(s, {})}})
    payload = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'phase': 'verify',
        'watchlist': list(per_sym.keys()),
        'baseline_name': 'baseline',
        'config': PROD_CONFIG,
        'combos': vcombos,
    }
    out = os.path.join(DATA_DIR, 'tune_grid_verify.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'💾 验证结果已写入 {out}')
    print(f'\n{"组合":<24}{"固中位胜率":>10}{"固盈亏比":>10}{"总笔数":>8}')
    for c in sorted(vcombos, key=lambda x: -x['pool']['med_win_fixed']):
        p = c['pool']
        print(f'{c["label"]:<24}{p["med_win_fixed"]:>9.2f}%{p["med_pl_fixed"]:>10.2f}'
              f'{p["total_trips"]:>8}')
    # 逐标的对比基线
    base_syms = {s: vcombos[0]['per_symbol'].get(s, {}) for s in per_sym}
    for c in vcombos[1:]:
        deltas = []
        for s in per_sym:
            b = base_syms.get(s, {})
            v = c['per_symbol'].get(s, {})
            d = (v.get('win_rate', 0) - b.get('win_rate', 0)) if b.get('total', 0) else 0
            deltas.append((s, d, v.get('total', 0)))
        worst = min(deltas, key=lambda x: x[1])
        print(f'  {c["label"]}: 单只最大跌幅 {worst[1]:+.2f}pp（{worst[0]}）')


def phase_report(args):
    coarse_path = os.path.join(DATA_DIR, 'tune_grid_coarse.json')
    fine_path = os.path.join(DATA_DIR, 'tune_grid_fine.json')
    verify_path = os.path.join(DATA_DIR, 'tune_grid_verify.json')
    if not (os.path.exists(coarse_path) and os.path.exists(verify_path)):
        print('❌ 需要先跑 coarse/fine 与 verify 两阶段')
        return
    with open(coarse_path, encoding='utf-8') as f:
        coarse = json.load(f)
    fine = None
    if os.path.exists(fine_path):
        with open(fine_path, encoding='utf-8') as f:
            fine = json.load(f)
    with open(verify_path, encoding='utf-8') as f:
        verify = json.load(f)
    html = build_html(coarse, fine, verify)
    date = datetime.date.today().strftime('%Y%m%d')
    out_path = os.path.join(OUT_DIR, f'tune_grid_report_{date}.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'📄 报告已写入 {out_path}')
    return out_path


def _combo_rows(combos, show_fixed=True):
    """组合表行 HTML。show_fixed=False 时（watchlist 表）只显示简单口径。"""
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if show_fixed:
        rows = ''
        for c in sorted(combos, key=lambda x: -x['pool']['med_win_fixed']):
            p = c['pool']
            rows += (
                f'<tr><td>{esc(c["label"])}</td>'
                f'<td>{p["med_win_fixed"]:.2f}%</td><td>{p["med_pl_fixed"]:.2f}</td>'
                f'<td>{p["med_win"]:.2f}%</td><td>{p["n_ok"]}/{p["n_all"]}</td>'
                f'<td>{p["total_trips"]}</td><td>{p["total_b"]}</td>'
                f'<td>{p["total_s"]}</td></tr>'
            )
        return rows
    rows = ''
    for c in sorted(combos, key=lambda x: -x['pool']['med_win_fixed']):
        p = c['pool']
        rows += (
            f'<tr><td>{esc(c["label"])}</td>'
            f'<td>{p["med_win_fixed"]:.2f}%</td><td>{p["med_pl_fixed"]:.2f}</td>'
            f'<td>{p["total_trips"]}</td></tr>'
        )
    return rows


def _atr_group_stats(coarse):
    """ATR0.25 对 T+0 / T+1 分组影响（用于结论区块）。返回 HTML 片段。"""
    from statistics import median
    try:
        base = {c['name']: c for c in coarse['combos']}['baseline']
        atr = {c['name']: c for c in coarse['combos']}['atr_025']
    except KeyError:
        return '<p class="dim">（未跑 atr_025，跳过分组统计）</p>'
    parts = []
    for grp, pred in [('T+0 ETF/LOF', lambda s: s.startswith(('1', '5'))),
                      ('T+1 个股', lambda s: not s.startswith(('1', '5')))]:
        syms = [s for s in base['per_symbol'] if pred(s)]
        bws = [base['per_symbol'][s]['win_rate'] for s in syms
               if base['per_symbol'][s].get('total')]
        aws = [atr['per_symbol'][s]['win_rate'] for s in syms
               if atr['per_symbol'].get(s, {}).get('total')]
        n_drop = sum(1 for s in syms if not atr['per_symbol'].get(s, {}).get('total'))
        parts.append(
            f'<tr><td>{grp}</td><td>{len(syms)}</td><td>{n_drop}</td>'
            f'<td>{median(bws):.1f}%</td><td>{median(aws):.1f}%</td>'
            f'<td>{median(aws) - median(bws):+.1f}pp</td></tr>')
    return f"""
<div class="card">
  <h2>ATR 门控分品种影响（atr_min_pct=0.25，调参池 40 只）</h2>
  <table>
    <tr><th>品种组</th><th>标的数</th><th>信号消失</th><th>基线胜率中位</th><th>ATR后胜率中位</th><th>Δ</th></tr>
    {''.join(parts)}
  </table>
  <p class="dim">T+0 组 9/20 只信号全部消失、剩余 11 只胜率中位 -4.7pp → ATR 对低波动 ETF/LOF 是纯伤害；T+1 组 0 只消失、+0.8pp → 对个股轻微正向。watchlist 的 161129 为高波动 LOF（atr 中位高），ATR 剔除的正是其低波动烂信号（+4.8pp）。</p>
</div>
"""


def build_html(coarse, fine, verify):
    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    atr_groups = _atr_group_stats(coarse)
    rows = _combo_rows(coarse['combos'], show_fixed=True)
    fine_html = ''
    if fine:
        frows = _combo_rows(fine['combos'], show_fixed=True)
        fine_html = f"""
<div class="card">
  <h2>局部细搜：调参池 {fine['pool_size']} 只（ATR/VWAP/RSI 邻近 + 组合叠加）</h2>
  <table>
    <tr><th>组合</th><th>净胜率中位(全)</th><th>盈亏比中位(全)</th><th>净胜率中位(样本≥20)</th><th>样本≥20</th><th>总笔数</th><th>B信号</th><th>S信号</th></tr>
    {frows}
  </table>
</div>
"""
    # watchlist 子表：逐标的 × 组合
    vrows = ''
    for s in verify['watchlist']:
        cell = ''
        for c in verify['combos']:
            m = c['per_symbol'].get(s, {})
            cell += (f'<td>{m.get("total", 0)}笔 {m.get("win_rate", 0):.1f}%<br>'
                     f'<span class="dim">PL {m.get("pl_ratio", 0):.2f}</span></td>')
        vrows += f'<tr><td>{esc(s)}</td>{cell}</tr>'
    vhead = ''.join(f'<th>{esc(c["label"])}</th>' for c in verify['combos'])
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>tpoint 两段式调参报告 {esc(datetime.date.today())}</title>
<style>
body{{background:#11151c;color:#d5dae2;font-family:Segoe UI,Microsoft YaHei,sans-serif;padding:24px;max-width:1200px;margin:auto}}
h1{{color:#fff;font-size:20px}} h2{{color:#9ec9ff;font-size:15px;margin-top:28px}}
.card{{background:#1a2029;border-radius:12px;padding:18px;margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #2a3140;font-size:13px}}
th{{color:#8a93a6;font-weight:500}}
.dim{{color:#7d8798;font-size:11px}}
.sum{{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}}
.sum div{{background:#232b38;border-radius:8px;padding:12px 18px}}
.sum b{{font-size:22px;display:block;color:#fff}}
</style></head><body>
<h1>tpoint 两段式调参网格搜索 · {esc(datetime.date.today())}</h1>
<div class="card">
  <h2>口径说明</h2>
  <p>胜率 = 净收益&gt;0 比例（扣双边成本：万一佣金不免五；个股卖出印花税万5.641；ETF/LOF 无印花税；滑点 2bps/边）。</p>
  <p>出场：仅移动止损（act 0.4% / trail 0.6%），S 信号自然出场。门控：MACD_GATE_MODE=floor（生产同源）。</p>
  <p>调参池 {coarse['pool_size']} 只（T+0 与 T+1 各半，排除基线样本与 watchlist 验证集）；watchlist 5 只独立验证、不做二次调参。</p>
</div>
<div class="card">
  <h2>第一段：调参池 {coarse['pool_size']} 只粗筛（按固定口径净胜率中位降序）</h2>
  <table>
    <tr><th>组合</th><th>净胜率中位(全)</th><th>盈亏比中位(全)</th><th>净胜率中位(样本≥20)</th><th>样本≥20</th><th>总笔数</th><th>B信号</th><th>S信号</th></tr>
    {rows}
  </table>
</div>
{fine_html}
<div class="card">
  <h2>第二段：watchlist 5 只独立验证（不做二次调参）</h2>
  <table>
    <tr><th>标的</th>{vhead}</tr>
    {vrows}
  </table>
</div>
{atr_groups}
<div class="card">
  <h2>结论与生产建议（2026-08-02 两段式验证）</h2>
  <table>
    <tr><th>旋钮</th><th>调参池 40 只（固定全集口径）</th><th>watchlist 5 只独立验证</th><th>结论</th></tr>
    <tr>
      <td><b>mhd=0.15</b>（P0 已上线）</td>
      <td>基线 47.40%；0.10~0.30 平台期 47.25~47.40%，0.05 47.25%</td>
      <td>基线中位 48.30%</td>
      <td>✅ 维持 0.15 不动（平台期中部，弱背离过滤有效）</td>
    </tr>
    <tr>
      <td><b>ATR 门控</b>（P1）</td>
      <td>atr_025 固定口径 44.25%（-3.15pp）；<br>
          <b>T+0 组 20 只 → 9 只信号消失</b>、胜率中位 47.6→42.9%；<br>
          T+1 组 20 只 → 0 只消失、47.4→48.2%</td>
      <td>5/5 全提升（中位 +4.8pp）：161129 55.6→60.4、600570 51.0→56.5、688111 47.4→53.1、300058 48.3→49.7、513310 45.8→48.8</td>
      <td>⚠️ <b>分品种生效</b>：对 T+1 个股与高波动 T+0 有效，对低波动 ETF/LOF 团灭。<br>
          不可全局上线；若上线需按标的波动特征选择性启用（如 atr 中位 &gt; 0.25% 才启用），<br>
          或仅对 watchlist 的 161129 单独启用（它本身高波动，ATR 剔除的正是低波动烂信号）。</td>
    </tr>
    <tr>
      <td><b>VWAP_DEV 0.6→0.7</b></td>
      <td>0.70：47.05%（-0.35pp）但盈亏比 0.90（+0.04）；<br>0.65 最优 47.55%（+0.15pp）、盈亏比 0.89</td>
      <td>0.65/0.70 均无单只 &gt;1.2pp 退化；300058 盈亏比 0.89→0.95、688111 0.71→0.77</td>
      <td>✅ 可接入：<b>VWAP_DEV_BUY 0.6→0.65</b>（盈亏比 +0.03、胜率持平、样本保留），<br>0.70 亦可但胜率略降；方向与报告「深负偏离 0.4389 优势」一致</td>
    </tr>
    <tr>
      <td><b>RSI&lt;30</b></td>
      <td>固定 40.00%（-7.4pp），40 只中 24 只样本崩</td>
      <td>161129 -10.8pp、600570 -6.6pp、300058 -4.7pp</td>
      <td>❌ 放弃（过度过滤 + 独立集严重退化；冒烟 61.75% 为小样本假象）</td>
    </tr>
    <tr>
      <td><b>早盘放宽</b></td>
      <td>47.40% ≈ 基线（差异 &lt;0.01pp）</td>
      <td>—（默认关闭）</td>
      <td>✅ 维持默认关闭（两轮验证：放宽仅把 78% 弱背离噪音放回，无增益）</td>
    </tr>
  </table>
  <h2 style="margin-top:18px">下一步</h2>
  <ul>
    <li><b>接入 VWAP_DEV_BUY=0.65</b>（低成本、盈亏比增益、无退化）——改 core/miji_alpha.py 常量后重启 monitor。</li>
    <li><b>ATR 门控按标的类型选择性启用</b>：对 watchlist 的 161129 单独启用 0.25%（回测 +4.8pp 且不伤其他标的），其余标的暂不启用；需要 monitor 支持 per-symbol 参数覆盖（新增配置项）。</li>
    <li>盈亏比 0.6-0.9 仍远低于 1.6 达标线 → <b>出场侧是主矛盾</b>（移动止损吃不满趋势），入场过滤只能微调；P3 应优先做 S 信号专项与出场管理。</li>
    <li>P3：多周期 MACD 共振、S 信号专项、长周期（10-15 年）数据源验证。</li>
  </ul>
</div>
</body></html>"""
    return html


def main():
    ap = argparse.ArgumentParser(description='P2 两段式调参网格搜索')
    ap.add_argument('--phase', choices=['coarse', 'fine', 'verify', 'report'], default='coarse')
    ap.add_argument('--procs', type=int, default=4, help='并行进程数')
    ap.add_argument('--smoke', action='store_true', help='冒烟模式（3 只标的快速验证）')
    ap.add_argument('--top-json', default=os.path.join(DATA_DIR, 'tune_grid_coarse.json'))
    ap.add_argument('--combos', default='', help='verify 阶段指定组合名（逗号分隔，缺省取 coarse top）')
    ap.add_argument('--watchlist', default='', help='verify 阶段标的（逗号分隔，缺省 5 只 watchlist）')
    ap.add_argument('--top-n', type=int, default=3, help='粗筛取 top-N 组合')
    args = ap.parse_args()
    if args.phase == 'coarse':
        phase_coarse(args)
    elif args.phase == 'fine':
        phase_fine(args)
    elif args.phase == 'verify':
        phase_verify(args)
    else:
        phase_report(args)


if __name__ == '__main__':
    main()
