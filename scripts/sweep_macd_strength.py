# -*- coding: utf-8 -*-
"""macd_min_hist_diff 强度阈值扫描：验证"只放行强背离"能否把 m 因子翻正。
对比 prod(默认 m 全放行) vs m_strength(带强度阈值) 的消融结果。
"""
import os, sys
os.environ['MACD_GATE_MODE'] = 'floor'
sys.path.insert(0, '.')
from core import miji_alpha
from scripts.backtest_screener import load_1m_csv, group_by_day, day_prev_close
from core.exit_manager import simulate_day, aggregate_metrics, make_config, cost_for_symbol
from scripts.backtest_screener import PROD_CONFIG

SYMBOLS = ['688146.SH', '600206.SH', '688347.SH', '600584.SH',
           '688766.SH', '161129.SZ', '513310.SH', '688111.SH']
DATA_DIR = 'F:/keyfactor_data/1m'
THRESHOLDS = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5]


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
        parts = {'train': days[:cut], 'test': days[cut:]}
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


def agg_across(results):
    tot = wins = ret = 0
    pl_w = pl_l = 0.0
    for m in results.values():
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


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--oos', action='store_true', help='样本外切分验证')
    args = ap.parse_args()
    if args.oos:
        print(f"{'阈值':>8s} {'train净收益':>11s} {'train胜率':>9s} {'test净收益':>10s} {'test胜率':>8s}  {'train笔':>7s} {'test笔':>6s}")
        for mhd in THRESHOLDS:
            res = {}
            tr_ret = te_ret = 0.0
            tr_w = te_w = 0
            tr_tot = te_tot = 0
            for sym in SYMBOLS:
                r = run_sym(sym, mhd, oos=True)
                tr_ret += r['train']['total_ret']
                te_ret += r['test']['total_ret']
                tr_w += r['train']['total'] * r['train']['win_rate']
                te_w += r['test']['total'] * r['test']['win_rate']
                tr_tot += r['train']['total']
                te_tot += r['test']['total']
            tr_wr = tr_w / tr_tot if tr_tot else 0
            te_wr = te_w / te_tot if te_tot else 0
            print(f"{mhd:8.2f} {tr_ret:11.2f}% {tr_wr:8.1f}% {te_ret:10.2f}% {te_wr:7.1f}%  "
                  f"{tr_tot:7d} {te_tot:6d}")
    else:
        print(f"{'阈值':>8s} {'总笔':>6s} {'净胜率':>7s} {'盈亏比':>6s} {'净收益':>9s}  逐标的净收益")
        for mhd in THRESHOLDS:
            res = {}
            per_sym = []
            for sym in SYMBOLS:
                r = run_sym(sym, mhd)
                m = r['full']
                res[sym] = m
                per_sym.append(f'{m["total_ret"]:+.1f}')
            a = agg_across(res)
            print(f"{mhd:8.2f} {a['total']:6d} {a['win_rate']:6.1f}% {a['pl_ratio']:6.2f} "
                  f"{a['total_ret']:8.2f}%  {' '.join(per_sym)}")
