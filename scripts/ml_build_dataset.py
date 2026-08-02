# -*- coding: utf-8 -*-
"""全市场 ML 数据集构建：tpoint 做T信号因子研究

流程（对 F 盘全市场 1m 库）：
  1. 标的过滤（流动性/数据量/一字占比/涨停占比/价格区间/ST/北交所）
  2. 逐标的：compute_miji_indicators → detect_miji_signals → 信号点特征工程
  3. 标签：信号点后未来 N=20 根 1m bar 净收益（扣双边成本）>0
  4. B/S 分开建模（因子语义相反），样本写入 parquet/csv

特征：34 个（基础14 + 补充15 + 信号上下文3 + 标签），全部因果可用。

用法：
  python scripts/ml_build_dataset.py --limit 100 --workers 8 --out output/ml_dataset
"""
import argparse
import glob
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np
import pandas as pd

os.environ['MACD_GATE_MODE'] = 'floor'
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import miji_alpha  # noqa: E402
from core.exit_manager import make_config, cost_for_symbol  # noqa: E402
from core.miji_alpha import (ema, compute_macd, compute_trend,  # noqa: E402
                             compute_trend_strength, compute_miji_indicators,
                             detect_miji_signals, LOCAL_W, MACD_FAST, MACD_SLOW,
                             MACD_SIGNAL, FLOOR_DEV_PCT, SIGNAL_GAP)
from scripts.backtest_screener import load_1m_csv, group_by_day, day_prev_close  # noqa: E402
from scripts.backtest_screener import PROD_CONFIG  # noqa: E402

DATA_DIR = os.environ.get('TP_1M_DIR', 'F:/keyfactor_data/1m')

# ============ 过滤标准（阶段3.1） ============
MIN_DAYS = 30            # 交易日数
MIN_AMOUNT = 2e7         # 日均成交额 ≥ 2000万
MIN_PRICE, MAX_PRICE = 3.0, 100.0
MAX_ONEBAR = 0.30        # 一字bar占比 ≤ 30%
MAX_LIMIT_UP_DAYS = 0.20 # 涨停日占比 ≤ 20%

# ============ 标签 ============
N_FWD = 20               # 前向窗口 bar（主 N=20）
N_SENS = [10, 20, 30, 60]  # N 敏感性对照

# ============ 特征列 ============
FEAT_BASE = ['vwap_dev', 'atr_pct', 'dif', 'dea', 'hist', 'hist_pct', 'trend',
             'trend_strong', 'rsi', 'vol_ratio', 'temp', 'chg', 'pos_in_day',
             'bar_idx_frac']
FEAT_EXT = ['macd5_dif', 'macd5_hist', 'macd15_dif', 'macd15_hist',
            'macd30_dif', 'macd30_hist', 'macd60_dif', 'macd60_hist',
            'rsi_dist_30', 'rsi_dist_70', 'kdj_k', 'kdj_d', 'kdj_j',
            'atr_chan_up1', 'atr_chan_dn1', 'mom_1', 'mom_5', 'mom_15',
            'is_morning', 'is_noon', 'is_tail']
FEAT_CTX = ['g_factor', 'v_factor', 'm_factor', 'resonance']
FEAT_ALL = FEAT_BASE + FEAT_EXT + FEAT_CTX
LABEL_COLS = ['label_' + str(n) for n in N_SENS]

META_COLS = ['symbol', 'date', 'idx', 'sig_type', 'price', 'sector']


def sector_of(code):
    if code.startswith(('600', '601', '603', '605')):
        return 'sh_main'
    if code.startswith(('000', '001', '002', '003')):
        return 'sz_main'
    if code.startswith(('300', '301')):
        return 'chinext'
    if code.startswith(('688', '689')):
        return 'star'
    if code.startswith('920'):
        return 'bse'
    if code.startswith(('1', '5')):
        return 'etf_lof'
    return 'other'


def is_stock(code):
    """个股（含科创/创业），排除 ETF/LOF/北交所（费率特殊或流动性差）"""
    return (code.startswith(('600', '601', '603', '605', '000', '001', '002', '003',
                             '300', '301', '688', '689')))


def build_features_for_day(sub, pc):
    """对单日 1m 数据计算全部特征矩阵（含多周期 MACD/KDJ/动量/时段）。
    返回 (data_dict, feat_df) 或 (None, None)。
    """
    o = sub['open'].values.astype(float)
    h = sub['high'].values.astype(float)
    lo = sub['low'].values.astype(float)
    c = sub['close'].values.astype(float)
    v = sub['volume'].values.astype(float)
    n = len(c)
    if n < 30 or pc is None or pc <= 0:
        return None, None
    # 整日 nan（停牌/数据缺失标记：OHLC 全空但 volume 有值）→ 跳过
    if np.isnan(c).all() or np.isnan(h).all() or np.isnan(lo).all() or np.isnan(o).all():
        return None, None
    # 部分 nan（个别 bar 缺口）→ 前向填充避免特征断裂
    for arr in (o, h, lo, c, v):
        if np.isnan(arr).any():
            idx = np.arange(n)
            valid = ~np.isnan(arr)
            if valid.sum() == 0:
                return None, None
            arr[:] = np.interp(idx, idx[valid], arr[valid])

    data = compute_miji_indicators(o, h, lo, c, v, pc)
    atr = data['atr']
    vwap = data['vwap']
    dif, dea, hist = data['dif'], data['dea'], data['hist']
    trend = data.get('trend')
    trend_strong = data.get('trend_strong')
    rsi = data.get('rsi')
    vol_ratio = data.get('vol_ratio')
    temp = data.get('temp')
    if trend is None:
        trend = compute_trend(c)
    if trend_strong is None:
        trend_strong = compute_trend_strength(c)

    # ---- 多周期 MACD（5/15/30/60 分钟重构：对 close 重采样后前向填充） ----
    macd_feats = {}
    for p in (5, 15, 30, 60):
        if n >= p * 2:
            idx = np.arange(n)
            resampled_idx = idx - (idx % p)  # 每 p 根取最后一根作为该周期收盘
            rc = np.array([c[resampled_idx == k].max() if k in resampled_idx
                           else c[resampled_idx[resampled_idx <= k].max()]
                           for k in range(n)])
            # 简化：按周期末值 forward-fill（等价于重采样到 p 分钟）
            p_dif, p_dea, p_hist = compute_macd(rc, fast=12, slow=26, signal=9)
            macd_feats[f'macd{p}_dif'] = p_dif
            macd_feats[f'macd{p}_hist'] = p_hist
        else:
            macd_feats[f'macd{p}_dif'] = np.zeros(n)
            macd_feats[f'macd{p}_hist'] = np.zeros(n)

    # ---- KDJ (9,3,3) ----
    k, d, j = np.zeros(n), np.zeros(n), np.zeros(n)
    k_prev, d_prev = 50.0, 50.0
    for i in range(n):
        lo9 = lo[max(0, i - 8):i + 1].min()
        hi9 = h[max(0, i - 8):i + 1].max()
        rsv = 50.0 if hi9 == lo9 else (c[i] - lo9) / (hi9 - lo9) * 100
        k[i] = k_prev * 2 / 3 + rsv / 3
        d[i] = d_prev * 2 / 3 + k[i] / 3
        j[i] = 3 * k[i] - 2 * d[i]
        k_prev, d_prev = k[i], d[i]

    # ---- 动量 ----
    mom_1 = np.zeros(n); mom_5 = np.zeros(n); mom_15 = np.zeros(n)
    mom_1[1:] = c[1:] / c[:-1] - 1
    mom_5[5:] = c[5:] / c[:-5] - 1
    mom_15[15:] = c[15:] / c[:-15] - 1

    # ---- 时段 ----
    times = sub['trade_time'].astype(str) if 'trade_time' in sub.columns else None
    is_morning = np.zeros(n, dtype=int)  # 09:30-10:00
    is_noon = np.zeros(n, dtype=int)     # 11:30-13:00 前后
    is_tail = np.zeros(n, dtype=int)     # 14:30 后
    if times is not None:
        hhmm = times.str[11:16].astype(str)
        is_morning = (hhmm >= '09:30') & (hhmm < '10:00')
        is_noon = (hhmm >= '11:25') & (hhmm <= '13:05')
        is_tail = (hhmm >= '14:30')
        is_morning = is_morning.astype(int).values
        is_noon = is_noon.astype(int).values
        is_tail = is_tail.astype(int).values

    vwap_safe = np.where(vwap > 0, vwap, 1e-9)
    atr_safe = np.where(atr > 0, atr, np.nan)

    feat = pd.DataFrame({
        'vwap_dev': (c - vwap) / vwap_safe * 100,
        'atr_pct': atr / c * 100,
        'dif': dif, 'dea': dea, 'hist': hist,
        'hist_pct': hist / vwap_safe * 100,
        'trend': trend, 'trend_strong': trend_strong,
        'rsi': rsi if rsi is not None else np.full(n, 50.0),
        'vol_ratio': vol_ratio if vol_ratio is not None else np.ones(n),
        'temp': temp if temp is not None else np.full(n, 50.0),
        'chg': (c / pc - 1) * 100,
        'pos_in_day': np.arange(n) / max(n - 1, 1),
        'bar_idx_frac': np.arange(n) / max(n - 1, 1),
        'rsi_dist_30': (30 - (rsi if rsi is not None else np.full(n, 50.0))) / 30,
        'rsi_dist_70': ((rsi if rsi is not None else np.full(n, 50.0)) - 70) / 30,
        'kdj_k': k, 'kdj_d': d, 'kdj_j': j,
        'atr_chan_up1': (c + 1.0 * atr_safe) / c - 1,
        'atr_chan_dn1': (c - 1.0 * atr_safe) / c - 1,
        'mom_1': mom_1, 'mom_5': mom_5, 'mom_15': mom_15,
        'is_morning': is_morning, 'is_noon': is_noon, 'is_tail': is_tail,
    })
    for kk, vv in macd_feats.items():
        feat[kk] = vv

    return data, feat


def extract_samples(sym, mhd=0.0):
    """对单标的：构建特征 → 检测信号 → 信号点取样 + 标签。返回 DataFrame。"""
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.isfile(path):
        return None
    try:
        df = load_1m_csv(path)
    except Exception:
        return None
    cost = cost_for_symbol(sym)
    buy_cost, sell_cost = cost
    cost_round = buy_cost + sell_cost  # 双边总成本 %

    days = group_by_day(df)
    rows = []
    for date, sub in days:
        pc = day_prev_close(df, date)
        if pc is None or pc <= 0:
            continue
        data, feat = build_features_for_day(sub, pc)
        if data is None:
            continue
        sigs = detect_miji_signals(data, pc, enable=(True, False, True),
                                   macd_gate_mode='floor', macd_min_hist_diff=mhd)
        c = data['c']
        n = data['n']
        for s in sigs:
            i = s['idx']
            if i + max(N_SENS) >= n:
                continue  # 前向窗口不足，丢弃
            fwd = {}
            for N in N_SENS:
                if i + N < n:
                    ret = (c[i + N] - s['price']) / s['price'] * 100 - cost_round
                    fwd['label_' + str(N)] = int(ret > 0)
                else:
                    fwd['label_' + str(N)] = None
            if any(v is None for v in fwd.values()):
                continue
            feat_row = feat.iloc[i]
            row = {
                'symbol': sym, 'date': date, 'idx': i,
                'sig_type': s['type'], 'price': s['price'],
                'sector': sector_of(sym),
                'g_factor': s['factors']['gravity'],
                'v_factor': s['factors']['vol_div'],
                'm_factor': s['factors']['macd_div'],
                'resonance': s['resonance_score'],
                'detail': s.get('detail', ''),
            }
            for fcol in FEAT_ALL:
                if fcol in feat_row:
                    val = feat_row[fcol]
                    row[fcol] = float(val) if np.isscalar(val) or np.isreal(val) else float(val)
            for kk, vv in fwd.items():
                row[kk] = vv
            rows.append(row)
    if not rows:
        return None
    return pd.DataFrame(rows)


def check_filter(sym):
    """标的过滤：快速扫描日K级特征。返回 (pass, reason)。"""
    code = sym.split('.')[0]
    if not is_stock(code):
        return False, '非股票'
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.isfile(path):
        return False, '无数据'
    try:
        df = load_1m_csv(path)
    except Exception:
        return False, '读取失败'
    if len(df) < 100:
        return False, '数据量不足'
    days = group_by_day(df)
    if len(days) < MIN_DAYS:
        return False, f'交易日{len(days)}<{MIN_DAYS}'
    # 流动性/一字/涨停/价格：用日K聚合（close vs prev close）
    prev_close = None
    onebar_days = 0
    limit_up_days = 0
    amounts = []
    prices = []
    for date, sub in days:
        c = sub['close'].values.astype(float)
        o = sub['open'].values.astype(float)
        h = sub['high'].values.astype(float)
        lo = sub['low'].values.astype(float)
        amount = sub['amount'].values.astype(float).sum() if 'amount' in sub.columns else 0
        amounts.append(amount)
        prices.append(c[-1])
        if prev_close is not None and prev_close > 0:
            chg = (c[-1] / prev_close - 1) * 100
            if abs(chg) < 0.1 and (h.max() == lo.min()):
                onebar_days += 1
            limit = 9.5 if code.startswith(('600', '601', '603', '605', '000', '001', '002', '003')) else 19.5
            if chg >= limit:
                limit_up_days += 1
        prev_close = c[-1]
    if len(amounts) == 0:
        return False, '无成交额'
    avg_amount = np.mean(amounts)
    if avg_amount < MIN_AMOUNT:
        return False, f'日均额{avg_amount/1e8:.2f}亿<{MIN_AMOUNT/1e8:.1f}亿'
    prices_valid = [p for p in prices if p == p]  # 过滤 nan
    if len(prices_valid) > 0 and not (MIN_PRICE <= np.median(prices_valid) <= MAX_PRICE):
        return False, f'价格中位{np.median(prices_valid):.1f}超出区间'
    if onebar_days / len(days) > MAX_ONEBAR:
        return False, f'一字占比{onebar_days/len(days):.0%}'
    if len(days) > 0 and limit_up_days / len(days) > MAX_LIMIT_UP_DAYS:
        return False, f'涨停占比{limit_up_days/len(days):.0%}'
    return True, 'ok'


def worker_extract(args):
    sym, mhd, out_dir = args
    try:
        t0 = time.time()
        df = extract_samples(sym, mhd)
        dt = time.time() - t0
        if df is None or len(df) == 0:
            return sym, 0, dt, '无样本'
        df.to_csv(f'{out_dir}/part_{sym.replace(".", "_")}.csv', index=False)
        return sym, len(df), dt, 'ok'
    except Exception as e:
        return sym, 0, 0, f'err:{e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 只（调试用）')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--out', default='output/ml_dataset')
    ap.add_argument('--mhd', type=float, default=0.0, help='macd_min_hist_diff（特征基线用生产值0.0）')
    args = ap.parse_args()

    out_dir = os.path.join(BASE, args.out)
    os.makedirs(out_dir, exist_ok=True)

    files = [f for f in glob.glob(f'{DATA_DIR}/*_1m.csv') if not f.endswith('.bad')]
    syms = [os.path.basename(f).replace('_1m.csv', '') for f in files]

    # 先过滤（可并行，但过滤本身快，单进程即可）
    print(f'共 {len(syms)} 只，开始过滤...', flush=True)
    passed = []
    for s in syms:
        ok, reason = check_filter(s)
        if ok:
            passed.append(s)
    print(f'过滤后剩 {len(passed)} 只', flush=True)
    with open(f'{out_dir}/filter_result.json', 'w', encoding='utf-8') as fh:
        json.dump({'total': len(syms), 'passed': passed, 'n_passed': len(passed)}, fh, ensure_ascii=False, indent=1)

    if args.limit:
        passed = passed[:args.limit]

    # 并行提取
    pool = mp.Pool(args.workers)
    tasks = [(s, args.mhd, out_dir) for s in passed]
    n_ok = n_samp = 0
    t0 = time.time()
    for res in pool.imap_unordered(worker_extract, tasks, chunksize=1):
        sym, cnt, dt, status = res
        n_ok += 1
        n_samp += cnt
        if n_ok % 50 == 0 or status != 'ok':
            print(f'  [{n_ok}/{len(tasks)}] {sym} {status} {cnt}样本 {dt:.1f}s', flush=True)
    pool.close()
    pool.join()
    print(f'完成: {n_ok} 只成功, 共 {n_samp} 样本, 耗时 {time.time()-t0:.0f}s', flush=True)

    # 合并
    parts = [f for f in os.listdir(out_dir) if f.startswith('part_') and f.endswith('.csv')]
    if parts:
        frames = [pd.read_csv(f'{out_dir}/{p}') for p in parts]
        all_df = pd.concat(frames, ignore_index=True)
        try:
            all_df.to_parquet(f'{out_dir}/dataset.parquet', index=False)
        except Exception as e:
            print(f'  parquet 写入失败（无 pyarrow），仅存 CSV: {e}', flush=True)
        all_df.to_csv(f'{out_dir}/dataset.csv', index=False)
        print(f'合并完成: {len(all_df)} 样本 → {out_dir}/dataset.csv', flush=True)
        print(f'B={len(all_df[all_df.sig_type=="B"])} S={len(all_df[all_df.sig_type=="S"])} '
              f'label20正={all_df.label_20.mean():.2%}', flush=True)


if __name__ == '__main__':
    mp.freeze_support()
    main()
