# -*- coding: utf-8 -*-
r"""ml_build_dataset.py — ML 训练数据集重建（2026-09-28，模块2.2 重建第二步）

## 定位
v10.0.0 灾难恢复中丢失的 scripts/ml_build_dataset.py 的重建版。
按 2026-08-02「ML vs 生产不匹配 5 维排查报告」（output/ml_vs_prod_mismatch_report_2026-08-02.html）
的 P0 根因修复：**训练样本 = 生产候选信号集**，特征 train-serve 同源。

## 三条同源铁律（与生产严格同函数）
1. 信号层：detect_signals_general + monitor._general_cfg_for(sym)（生产 config 链：
   data/monitor_config.json 的 _global.general_algorithm + per-symbol 覆盖，
   与 monitor 实盘消费路径完全一致）。**不再是 2026-08-01 旧版的无过滤 miji 裸信号**。
2. 特征层：core/ml_features.build_feature_row（服务侧 monitor.score_signal 同一函数）。
3. ctx：check_miji_trigger snapshot → monitor._ml_ctx_from_snapshot（同一映射函数；
   macd_gate_mode='floor' + min_hist_diff=0.15 与 monitor B/S 两侧调用一致）。

## 标签口径（docs/t0_optimization_code_guide.md 有据）
  label_N = 信号点后 N 根 1m bar 净收益（扣双边成本 via exit_manager.cost_for_symbol）> 0
  N ∈ {10,20,30,60}，主标签 label_20。
  B：net = (c[i+N]-c[i])/c[i]*100 - (buy+sell)；S：net = (c[i]-c[i+N])/c[i]*100 - (buy+sell)
  入场价 = 信号 bar close（口径铁律，非推送价）。
  仅收录 i+60 <= n-1 的信号（四标签全可得；与原数据集 pos_in_day max=0.750 互洽）。
  ⚠️ 本标签是「固定持有 N 根」静态口径，与生产出场管理 round-trip 不同；
     round-trip 口径的 OOS 评估在 B3（ml_train_evaluate + simulate_position_sm）完成。

## 输出（分块 parquet，断点续跑）
  <out>/part_<sym>.parquet   每标的一块（已完成自动跳过）
  <out>/manifest.json        构建清单（版本/池/生产 config 快照/逐标的统计/口径声明）

## 用法
  venv\Scripts\python.exe scripts\ml_build_dataset.py                 # 全量（tune_pool_40）
  venv\Scripts\python.exe scripts\ml_build_dataset.py --self-test     # 1 标的 3 天自检
  venv\Scripts\python.exe scripts\ml_build_dataset.py --max-symbols 3 # 限量试跑
"""
import argparse
import csv
import datetime
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import ml_features  # noqa: E402
from miji_alpha import check_miji_trigger  # noqa: E402
from exit_manager import cost_for_symbol  # noqa: E402
from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from daily_signal_review import build_data  # noqa: E402
# monitor 同源：生产 config 链 + ctx 映射（import 安全，模块级初始化只读 config）
import monitor as M  # noqa: E402

DATA_DIR = r'F:/keyfactor_data/1m_clean'
# 个股兜底目录（2026-09-28 实测：1m_clean 仅 192 文件，tune_pool_40 的 20 只个股
# 全部缺失、20 只 ETF/LOF 全在位；raw 1m 目录 4153 文件同 schema，可作为个股源。
# 两目录列名一致：symbol,name,timestamp,trade_date,trade_time,open..volume,amount。
# 注意 raw 目录 timestamp 为 ms 级、1m_clean 为秒级——本脚本不消费 timestamp 列，无影响。）
DATA_DIR_FALLBACK = r'F:/keyfactor_data/1m'
DEFAULT_OUT = r'F:/keyfactor_data/ml_dataset_v2'
POOL_FILE = os.path.join(ROOT, 'data', 'tune_pool_40.json')
MONITOR_CFG = os.path.join(ROOT, 'data', 'monitor_config.json')

LABEL_HORIZONS = (10, 20, 30, 60)
MAIN_LABEL = 20
MHD = 0.15  # 生产 TP_MHD_THRESHOLD 默认（monitor B/S 两侧 ML ctx 调用口径）

META_COLS = ['symbol', 'date', 'idx', 'trade_time', 'type', 'price', 'pc',
             'score', 'strength', 'reason']
LABEL_COLS = [f'label_{n}' for n in LABEL_HORIZONS] + [f'net_{n}' for n in LABEL_HORIZONS]


def _load_pool(path):
    with open(path, encoding='utf-8') as fh:
        obj = json.load(fh)
    return [e['symbol'] for e in obj['pool']]


def _per_symbol_trigger_params(cfg_obj, sym):
    """check_miji_trigger 的 per-symbol 闸门（atr_min_pct/mpr），与 monitor run() 透传一致。
    当前 monitor_config.json 无 mpr/atr_min 键 → 全默认（None）；有键时同名读取。"""
    ent = (cfg_obj.get(sym) or {})
    mpr = ent.get('mpr_b60') or ent.get('mpr_enable')
    return {
        'atr_min_pct': ent.get('atr_min_pct'),
        'mpr_enable': ('B' if mpr is True else mpr) if mpr else None,
        'mpr_periods': (60,) if mpr else None,
    }


def _ctx_for(data, i, trig_params):
    """与 monitor B/S 两侧 ML ctx 调用完全同口径（floor 门控 + mhd=0.15）。"""
    try:
        kw = dict(macd_gate_mode='floor', min_hist_diff=MHD)
        if trig_params.get('atr_min_pct') is not None:
            kw['atr_min_pct'] = trig_params['atr_min_pct']
        if trig_params.get('mpr_enable'):
            kw['mpr_enable'] = trig_params['mpr_enable']
            kw['mpr_periods'] = trig_params['mpr_periods'] or (60,)
        snap = check_miji_trigger(data, i, **kw)[4]
    except Exception:
        snap = None
    return M._ml_ctx_from_snapshot(snap)


def _labels(sig_type, c, i, buy_cost, sell_cost):
    """固定持有 N 根净收益标签（成本口径见模块 docstring）。四标签全可得才收录。"""
    n = len(c)
    out = {}
    px = float(c[i])
    if px <= 0:
        return None
    for nn in LABEL_HORIZONS:
        j = i + nn
        if j > n - 1:
            return None
        if sig_type == 'B':
            net = (float(c[j]) - px) / px * 100.0 - (buy_cost + sell_cost)
        else:
            net = (px - float(c[j])) / px * 100.0 - (buy_cost + sell_cost)
        out[f'net_{nn}'] = round(net, 6)
        out[f'label_{nn}'] = int(net > 0)
    return out


def build_symbol(sym, cfg_obj, out_dir, max_days=None, verbose=True):
    """单标的全历史构建 → part parquet。返回统计 dict（None=跳过）。"""
    path = os.path.join(DATA_DIR, f'{sym}_1m.csv')
    src = '1m_clean'
    if not os.path.isfile(path):
        path = os.path.join(DATA_DIR_FALLBACK, f'{sym}_1m.csv')
        src = '1m_raw'
    if not os.path.isfile(path):
        if verbose:
            print(f'  ⚠️ {sym} 数据文件缺失（1m_clean/1m 均无），跳过')
        return None

    days = {}
    with open(path, encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            days.setdefault(row['trade_date'], []).append(row)
    dates = sorted(days)
    if max_days:
        dates = dates[-max_days:]

    try:
        cfg = M._general_cfg_for(sym) or GENERAL_DEFAULT
    except Exception:
        cfg = GENERAL_DEFAULT
    trig_params = _per_symbol_trigger_params(cfg_obj, sym)
    buy_cost, sell_cost = cost_for_symbol(sym)

    recs = []
    prev_close = None
    n_days_used = 0
    for d in dates:
        rs = sorted(days[d], key=lambda x: x['trade_time'])
        if len(rs) < 65:  # 最短也要信号(>=2)+label_60 窗口
            prev_close = float(rs[-1]['close']) if rs else prev_close
            continue
        # PC 口径铁律：前一交易日收盘；文件首日无 prev → 跳过（禁首 bar 近似）
        if prev_close is None:
            prev_close = float(rs[-1]['close'])
            continue
        o = np.array([float(x['open']) for x in rs])
        h = np.array([float(x['high']) for x in rs])
        lo = np.array([float(x['low']) for x in rs])
        c = np.array([float(x['close']) for x in rs])
        v = np.array([float(x['volume']) for x in rs])
        tt = [x['trade_time'] for x in rs]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c,
                           'volume': v, 'trade_time': tt})
        data = build_data(df, prev_close)
        if data is None:
            prev_close = float(c[-1])
            continue
        n_days_used += 1

        try:
            sigs = detect_signals_general(data, prev_close, cfg)
        except Exception as e:
            print(f'  ⚠️ {sym} {d} 信号检测异常: {e}')
            sigs = []

        for s in sigs:
            i = int(s['idx'])
            lab = _labels(s['type'], c, i, buy_cost, sell_cost)
            if lab is None:
                continue  # 四标签不全（i+60 越界）→ 不收录
            ctx = _ctx_for(data, i, trig_params)
            row = ml_features.build_feature_row(data, prev_close, i, df, ctx)
            if row is None:
                continue
            rec = {'symbol': sym, 'date': d, 'idx': i,
                   'trade_time': tt[i] if i < len(tt) else '',
                   'type': s['type'], 'price': round(float(c[i]), 4),
                   'pc': round(float(prev_close), 4),
                   'score': s.get('score'), 'strength': s.get('strength'),
                   'reason': s.get('reason', '')}
            rec.update({name: float(row[k]) for k, name in enumerate(ml_features.FEAT_ALL)})
            rec.update(lab)
            recs.append(rec)
        prev_close = float(c[-1])

    if not recs:
        return {'symbol': sym, 'days': n_days_used, 'signals': 0}

    part = pd.DataFrame(recs)
    part_path = os.path.join(out_dir, f'part_{sym}.parquet')
    part.to_parquet(part_path, index=False)
    nb = int((part['type'] == 'B').sum())
    ns = int((part['type'] == 'S').sum())
    stat = {
        'symbol': sym, 'source': src, 'days': n_days_used, 'signals': len(part),
        'B': nb, 'S': ns,
        'label20_pos_rate_B': round(float(part.loc[part['type'] == 'B', f'label_{MAIN_LABEL}'].mean()), 4) if nb else None,
        'label20_pos_rate_S': round(float(part.loc[part['type'] == 'S', f'label_{MAIN_LABEL}'].mean()), 4) if ns else None,
    }
    if verbose:
        print(f'  ✅ {sym}: {n_days_used} 天 {len(part)} 信号 (B{nb}/S{ns}) '
              f'label20+ B={stat["label20_pos_rate_B"]} S={stat["label20_pos_rate_S"]}')
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool', default=POOL_FILE)
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--max-symbols', type=int, default=0)
    ap.add_argument('--max-days', type=int, default=0, help='每标的只用最近 N 天（试跑）')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    with open(MONITOR_CFG, encoding='utf-8') as fh:
        cfg_obj = json.load(fh)

    if args.self_test:
        sym = _load_pool(args.pool)[0]
        print(f'== self-test: {sym} 最近 3 天 ==')
        tmp = os.path.join(args.out, '_selftest')
        os.makedirs(tmp, exist_ok=True)
        stat = build_symbol(sym, cfg_obj, tmp, max_days=3)
        assert stat and stat['signals'] > 0, f'self-test 无信号: {stat}'
        part = pd.read_parquet(os.path.join(tmp, f'part_{sym}.parquet'))
        feat_cols = [c for c in part.columns if c in ml_features.FEAT_ALL]
        assert feat_cols == ml_features.FEAT_ALL, '特征列顺序/集合与 FEAT_ALL 不符'
        assert not part[ml_features.FEAT_ALL].isna().any().any(), '特征列含 NaN'
        assert set(part[f'label_{MAIN_LABEL}'].unique()) <= {0, 1}, '标签非二值'
        assert set(part['type'].unique()) <= {'B', 'S'}, '信号类型异常'
        print(f'== self-test PASS: {len(part)} 行, {len(part.columns)} 列 '
              f'(meta {len(META_COLS)} + feat 39 + label {len(LABEL_COLS)}) ==')
        return

    symbols = _load_pool(args.pool)
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    print(f'== ML 数据集重建: {len(symbols)} 标的 → {args.out} ==')
    print(f'   信号层: detect_signals_general + monitor._general_cfg_for（生产 config 链）')
    print(f'   特征层: ml_features.build_feature_row（train-serve 同源）')
    print(f'   标签: label_N N∈{LABEL_HORIZONS} 固定持有扣双边成本, 主标签 label_{MAIN_LABEL}')

    t0 = time.time()
    stats = []
    for k, sym in enumerate(symbols, 1):
        part_path = os.path.join(args.out, f'part_{sym}.parquet')
        if os.path.isfile(part_path):
            print(f'[{k}/{len(symbols)}] {sym} 已存在，跳过（断点续跑）')
            continue
        print(f'[{k}/{len(symbols)}] {sym} ...')
        st = build_symbol(sym, cfg_obj, args.out,
                          max_days=args.max_days or None)
        if st:
            stats.append(st)

    manifest = {
        'dataset_version': 'ml_dataset_v2',
        'built_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'elapsed_min': round((time.time() - t0) / 60, 1),
        'pool_file': os.path.relpath(args.pool, ROOT),
        'data_dir': DATA_DIR,
        'feature_names': ml_features.FEAT_ALL,
        'label_caliber': (f'label_N=信号bar close入场, 持有N根(10/20/30/60)净收益(扣双边成本 '
                          f'cost_for_symbol)>0; 主标签 label_{MAIN_LABEL}; 仅收 i+60<=n-1 信号'),
        'signal_layer': 'detect_signals_general + monitor._general_cfg_for（生产 config 链）',
        'feature_layer': 'core/ml_features.build_feature_row（train-serve 同源）',
        'ctx_caliber': f'check_miji_trigger(floor, mhd={MHD}) snapshot → monitor._ml_ctx_from_snapshot',
        'production_general_algorithm': (cfg_obj.get('_global') or {}).get('general_algorithm'),
        'symbols': stats,
        'total_signals': sum(s['signals'] for s in stats),
    }
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    print(f'\n== 完成: {len(stats)} 标的, {manifest["total_signals"]} 信号, '
          f'{manifest["elapsed_min"]} 分钟 ==')
    print(f'   manifest: {os.path.join(args.out, "manifest.json")}')


if __name__ == '__main__':
    main()
