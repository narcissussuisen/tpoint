# -*- coding: utf-8 -*-
"""全市场通用性验证：macd_min_hist_diff 强度阈值在跨板块抽样样本上的表现。

回答用户问题1（"8 标的是否具有全市场通用性"）：
  8 标的基线（科创板/半导体集中）上 m 门控 -167pp、强度阈值 0.15 翻正。
  本脚本用 F 盘 68 只跨板块抽样（沪主板/深主板/创业板/科创板/北交所/ETF-LOF）
  重跑 {0.0, 0.15} 阈值 × 全样本/OOS，对比板块差异，判断结论能否外推。

输出: output/market_generality_2026-08-01.json + 终端汇总
"""
import json
import os
import sys

os.environ['MACD_GATE_MODE'] = 'floor'
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import miji_alpha  # noqa: E402
from core.exit_manager import (simulate_day, aggregate_metrics, make_config,  # noqa: E402
                               cost_for_symbol)
from scripts.backtest_screener import PROD_CONFIG, load_1m_csv, group_by_day, day_prev_close  # noqa: E402

DATA_DIR = 'F:/keyfactor_data/1m'
OUT_JSON = os.path.join(BASE, 'output', 'market_generality_2026-08-01.json')


def load_sample_universe(path=None):
    path = path or os.path.join(BASE, 'output', 'market_sample_universe.json')
    with open(path, encoding='utf-8') as fh:
        meta = json.load(fh)
    out = []
    for sector, items in meta['sample'].items():
        for it in items:
            out.append({'sector': sector, 'code': it['code']})
    return out


def run_sym(sym, mhd, oos=False):
    df = load_1m_csv(f'{DATA_DIR}/{sym}_1m.csv')
    cost = cost_for_symbol(sym)
    mcfg = make_config(**PROD_CONFIG)
    miji_alpha.VOL_DIV_ENABLED = False
    miji_alpha.FLOOR_DEV_PCT = 1.5
    miji_alpha.MACD_GATE_MODE = 'floor'
    days = group_by_day(df)
    if oos:
        cut = int(len(days) * 0.7)
        parts = {'train': days[:cut], 'test': days[cut:], 'full': days}
    else:
        parts = {'full': days}
    out = {}
    for pname, pdays in parts.items():
        trips = []
        for date, sub in pdays:
            pc = day_prev_close(df, date)
            if pc is None or pc <= 0:
                continue
            o = sub['open'].values.astype(float)
            h = sub['high'].values.astype(float)
            lo = sub['low'].values.astype(float)
            c = sub['close'].values.astype(float)
            v = sub['volume'].values.astype(float)
            data = miji_alpha.compute_miji_indicators(o, h, lo, c, v, pc)
            sigs = miji_alpha.detect_miji_signals(data, pc, enable=(True, False, True),
                                                  macd_gate_mode='floor',
                                                  macd_min_hist_diff=mhd)
            prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                      'trend': data.get('trend'), 'n': data['n']}
            trips.extend(simulate_day(sigs, prices, mcfg, cost=cost))
        out[pname] = aggregate_metrics(trips)
    if oos:
        return out
    return {'full': out['full']}


def agg_across(metrics_list):
    tot = wins = ret = 0
    pl_w = pl_l = 0.0
    for m in metrics_list:
        n = m.get('total') or 0
        tot += n
        wins += n * (m.get('win_rate') or 0) / 100
        ret += m.get('total_ret') or 0
        pl_w += n * (m.get('avg_win') or 0)
        pl_l += n * (m.get('avg_loss') or 0)
    return {'total': tot,
            'win_rate': round(wins / tot * 100, 1) if tot else 0,
            'pl_ratio': round(pl_w / abs(pl_l), 2) if pl_l else 99,
            'total_ret': round(ret, 2)}


def main():
    universe = load_sample_universe()
    print(f'抽样样本: {len(universe)} 只', flush=True)
    sectors = {}
    for it in universe:
        sectors.setdefault(it['sector'], []).append(it['code'])
    print('板块分布:', {k: len(v) for k, v in sectors.items()}, flush=True)

    results = {'th0': {}, 'th015': {}}
    for mhd, key in [(0.0, 'th0'), (0.15, 'th015')]:
        print(f'\n=== 阈值 {mhd} ===', flush=True)
        for it in universe:
            sym = it['code']
            try:
                r = run_sym(sym, mhd, oos=True)
            except Exception as e:
                print(f'  ! {sym} 失败: {e}', flush=True)
                continue
            results[key][sym] = {'sector': it['sector'], 'metrics': r}
            f = r['full']
            t = r['test']
            print(f"  {sym:12s} [{it['sector']:8s}] full: {f['total']:3d}笔 "
                  f"{f['win_rate']:5.1f}% {f['total_ret']:+7.2f}% | "
                  f"test: {t['total']:3d}笔 {t['win_rate']:5.1f}% {t['total_ret']:+7.2f}%", flush=True)

    # ---- 汇总：按板块 + 总体 ----
    print('\n=== 板块汇总（full 全样本） ===')
    summary = {'by_sector': {}, 'overall': {}}
    for key, mhd_label in [('th0', 0.0), ('th015', 0.15)]:
        by_sector = {}
        for sector in sectors:
            syms = sectors[sector]
            ms = [results[key][s]['metrics']['full'] for s in syms if s in results[key]]
            by_sector[sector] = agg_across(ms)
        summary['by_sector'][key] = by_sector
        all_ms = [results[key][s]['metrics']['full'] for s in results[key]]
        summary['overall'][key] = agg_across(all_ms)
        print(f'阈值 {mhd_label}:')
        for sector in sectors:
            a = by_sector[sector]
            print(f"  {sector:10s} {a['total']:4d}笔 {a['win_rate']:5.1f}% 盈亏比{a['pl_ratio']:4.2f} "
                  f"净收益{a['total_ret']:+8.2f}%")
        a = summary['overall'][key]
        print(f"  {'总 体':10s} {a['total']:4d}笔 {a['win_rate']:5.1f}% 盈亏比{a['pl_ratio']:4.2f} "
              f"净收益{a['total_ret']:+8.2f}%")

    # ---- OOS test 汇总 ----
    print('\n=== 总体 OOS test 汇总 ===')
    for key, mhd_label in [('th0', 0.0), ('th015', 0.15)]:
        tr_ms = [results[key][s]['metrics']['train'] for s in results[key]]
        te_ms = [results[key][s]['metrics']['test'] for s in results[key]]
        tr = agg_across(tr_ms)
        te = agg_across(te_ms)
        print(f"阈值 {mhd_label}: train {tr['total']}笔 {tr['win_rate']}% {tr['total_ret']:+.2f}% | "
              f"test {te['total']}笔 {te['win_rate']}% {te['total_ret']:+.2f}%")

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as fh:
        json.dump({'universe': universe, 'results': results, 'summary': summary},
                  fh, ensure_ascii=False, indent=1, default=str)
    print(f'\n已保存 → {OUT_JSON}')


if __name__ == '__main__':
    main()
