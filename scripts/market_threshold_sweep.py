# -*- coding: utf-8 -*-
"""阈值敏感性分析：macd_min_hist_diff ∈ {0, 0.05, ..., 0.6} 对净收益/胜率的影响。

回答用户问题2（"阈值数值不同，会影响准确率吗"）：
  在全市场 68 只抽样样本上跑阈值网格，分析净收益/胜率随阈值变化的曲线，
  区分「平台期」（阈值附近结果稳定）与「敏感区」（小步长即大波动），
  为最终选阈值提供稳健性依据（防过拟合）。

复用 market_generality_check 的 run_sym，但只跑 full（全样本）+ 汇总 OOS test。
输出: output/market_threshold_sweep_2026-08-01.json + 终端表格
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
from scripts.market_generality_check import load_sample_universe, agg_across  # noqa: E402

DATA_DIR = 'F:/keyfactor_data/1m'
OUT_JSON = os.path.join(BASE, 'output', 'market_threshold_sweep_2026-08-01.json')
THRESHOLDS = [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6]


def run_sym(sym, mhd, oos=True):
    df = load_1m_csv(f'{DATA_DIR}/{sym}_1m.csv')
    cost = cost_for_symbol(sym)
    mcfg = make_config(**PROD_CONFIG)
    miji_alpha.VOL_DIV_ENABLED = False
    miji_alpha.FLOOR_DEV_PCT = 1.5
    miji_alpha.MACD_GATE_MODE = 'floor'
    days = group_by_day(df)
    cut = int(len(days) * 0.7)
    parts = {'train': days[:cut], 'test': days[cut:], 'full': days} if oos else {'full': days}
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
    return out


def main():
    universe = load_sample_universe()
    print(f'抽样样本: {len(universe)} 只', flush=True)
    rows = []
    for mhd in THRESHOLDS:
        tr_ms, te_ms = [], []
        per_sym = []
        for it in universe:
            sym = it['code']
            try:
                r = run_sym(sym, mhd)
            except Exception as e:
                print(f'  ! {sym} 失败: {e}', flush=True)
                continue
            tr_ms.append(r['train'])
            te_ms.append(r['test'])
            per_sym.append((sym, r['full']))
        tr = agg_across(tr_ms)
        te = agg_across(te_ms)
        full = agg_across([m for _, m in per_sym])
        # 正收益标的比例
        pos_syms = sum(1 for _, m in per_sym if m['total_ret'] > 0)
        rows.append({
            'threshold': mhd, 'full': full, 'train': tr, 'test': te,
            'pos_sym_ratio': round(pos_syms / len(per_sym) * 100, 1),
            'n_syms': len(per_sym),
        })
        print(f"mhd={mhd:4.2f} full: {full['total']:5d}笔 {full['win_rate']:5.1f}% "
              f"{full['total_ret']:+8.2f}% | train: {tr['total']:4d}笔 {tr['win_rate']:5.1f}% "
              f"{tr['total_ret']:+8.2f}% | test: {te['total']:4d}笔 {te['win_rate']:5.1f}% "
              f"{te['total_ret']:+8.2f}% | 正收益标的 {pos_syms}/{len(per_sym)}", flush=True)

    # ---- 敏感性分段：相邻阈值净收益/胜率变化 ----
    print('\n=== 敏感性分段（相邻阈值全样本净收益变化） ===')
    seg = []
    for i in range(1, len(rows)):
        d_ret = rows[i]['full']['total_ret'] - rows[i - 1]['full']['total_ret']
        d_wr = rows[i]['full']['win_rate'] - rows[i - 1]['full']['win_rate']
        d_te = rows[i]['test']['total_ret'] - rows[i - 1]['test']['total_ret']
        seg.append({'from': rows[i - 1]['threshold'], 'to': rows[i]['threshold'],
                    'd_full_ret': round(d_ret, 2), 'd_full_wr': round(d_wr, 2),
                    'd_test_ret': round(d_te, 2)})
        print(f"  {rows[i-1]['threshold']:.2f}→{rows[i]['threshold']:.2f}: "
              f"full净收益 {d_ret:+.2f}pp 胜率 {d_wr:+.2f}pp test净收益 {d_te:+.2f}pp")

    out = {'thresholds': THRESHOLDS, 'rows': rows, 'segments': seg}
    with open(OUT_JSON, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    print(f'\n已保存 → {OUT_JSON}')


if __name__ == '__main__':
    main()
