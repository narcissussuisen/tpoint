# -*- coding: utf-8 -*-
r"""ml_train_evaluate.py — ML B/S 双模型训练 + 修复口径 OOS（2026-09-28 重建，模块2.3）

## 定位
v10.0.0 灾难丢失的 scripts/ml_train_evaluate.py 的重建版。输入为
ml_build_dataset.py（重建版，信号层=生产候选信号集）产出的 ml_dataset_v2。

## 与旧版（2026-08-01/02）的关键差异
1. 标签 label_20 的样本集已是「生产候选信号」（GT 引擎 + 生产 config），
   不再含被生产过滤掉的样本（旧 mismatch 报告根因①已修）。
2. 除静态口径指标（AUC/分箱/top_bin_win）外，新增**修复口径 OOS**：
   OOS 段按「生产信号全量 vs ML 过滤后」两臂跑 simulate_position_sm
   round-trip（底仓模型 + cost_for_symbol），比较净胜率/单笔均净——
   这是 mismatch 报告根因②（固定持有 vs round-trip）的正面回应。
3. 基金/个股分池评估（双轨制铁律，禁止混合均值掩盖）。

## 用法
  venv\Scripts\python.exe scripts\ml_train_evaluate.py \
      --data F:/keyfactor_data/ml_dataset_v2 --ver v2.0.0-ml-20260928

## 产物
  output/ml_models/<ver>_b.json / <ver>_s.json   （XGB，feature_names=FEAT_ALL）
  output/ml_train_results_v2.json                 （指标 + 修复口径 OOS + 分池）
  output/ml_versions.json                         （追加注册条目，schema 兼容旧条目）

## 验收（B4 灰度闸门，脚本退出码）
  PASS = OOS AUC(B,S) 均 >= 0.55
       且 修复口径两臂均: ml_net_wr >= base_net_wr + 1.0pp
       且 保留行程占比 >= 30% 且 n_trips >= 30
"""
import argparse
import datetime
import hashlib
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
from exit_manager import cost_for_symbol  # noqa: E402
from miji_alpha import compute_miji_indicators  # noqa: E402
from simulate_position_sm import simulate_position_sm  # noqa: E402

FEAT = ml_features.FEAT_ALL
MAIN_LABEL = 'label_20'
ML_T_LO = 0.45   # 与 monitor.ML_T_LO 一致（过滤阈值）
DATA_CLEAN = r'F:/keyfactor_data/1m_clean'
DATA_RAW = r'F:/keyfactor_data/1m'

GATE_AUC = 0.55
GATE_WR_LIFT_PP = 1.0
GATE_KEPT_FRAC = 0.30
GATE_MIN_TRIPS = 30


def is_fund(sym):
    code = sym.split('.')[0]
    return code.startswith('5') or code.startswith(('15', '16', '18'))


def _md5_16(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()[:16]


def load_dataset(data_dir):
    parts = sorted(f for f in os.listdir(data_dir)
                   if f.startswith('part_') and f.endswith('.parquet'))
    if not parts:
        raise SystemExit(f'❌ {data_dir} 无 part_*.parquet，先跑 ml_build_dataset.py')
    df = pd.concat([pd.read_parquet(os.path.join(data_dir, p)) for p in parts],
                   ignore_index=True)
    return df, parts


def time_split(df, oos_frac):
    dates = sorted(df['date'].unique())
    k = max(1, int(len(dates) * (1 - oos_frac)))
    split = dates[k - 1]
    tr = df[df['date'] <= split]
    te = df[df['date'] > split]
    return tr, te, split


def _bins(prob, y, n_bins=10):
    """按概率十分箱（rank 1=最低），返回 bins + 单调性 + 顶箱胜率。"""
    order = np.argsort(prob)
    rk = np.empty(len(prob), dtype=int)
    rk[order] = np.arange(len(prob))
    bin_idx = np.minimum(rk * n_bins // max(len(prob), 1), n_bins - 1)
    bins = []
    for b in range(n_bins):
        m = bin_idx == b
        if m.sum() == 0:
            continue
        bins.append({'rank': b + 1, 'n': int(m.sum()),
                     'win_rate': round(float(y[m].mean()), 4),
                     'avg_prob': round(float(prob[m].mean()), 4)})
    wrs = [b['win_rate'] for b in bins]
    mono = all(a <= b + 1e-9 for a, b in zip(wrs, wrs[1:])) if len(wrs) >= 2 else False
    return bins, mono, (bins[-1]['win_rate'] if bins else None)


def train_side(side, tr, te, ver, out_models):
    """训练单侧（B 或 S）XGB 模型 + 静态口径评估。返回 (model_path, metrics)。"""
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score
    from sklearn.model_selection import GroupKFold

    tr_s = tr[tr['type'] == side]
    te_s = te[te['type'] == side]
    if len(tr_s) < 200 or len(te_s) < 50:
        return None, {'error': f'{side} 侧样本不足 train={len(tr_s)} test={len(te_s)}'}

    Xtr, ytr = tr_s[FEAT], tr_s[MAIN_LABEL].values
    Xte, yte = te_s[FEAT], te_s[MAIN_LABEL].values

    t0 = time.time()
    model = xgb.XGBClassifier(
        max_depth=5, n_estimators=300, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
        tree_method='hist', eval_metric='auc', random_state=42, n_jobs=4)
    model.fit(Xtr, ytr)
    train_s = round(time.time() - t0, 1)

    prob = model.predict_proba(Xte)[:, 1]
    pred = (prob >= 0.5).astype(int)
    auc = float(roc_auc_score(yte, prob)) if len(np.unique(yte)) > 1 else None
    bins, mono, top_win = _bins(prob, yte)

    # GroupKFold(by symbol) CV AUC（手动循环，规避 cross_val_score groups 兼容坑）
    cv_aucs = []
    gkf = GroupKFold(n_splits=min(5, tr_s['symbol'].nunique()))
    Xa, ya = tr_s[FEAT].values, ytr
    grp = tr_s['symbol'].values
    for itr, ite in gkf.split(Xa, ya, grp):
        if len(np.unique(ya[ite])) < 2:
            continue
        m = xgb.XGBClassifier(max_depth=5, n_estimators=150, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, tree_method='hist',
                              random_state=42, n_jobs=4)
        m.fit(Xa[itr], ya[itr])
        cv_aucs.append(float(roc_auc_score(ya[ite], m.predict_proba(Xa[ite])[:, 1])))

    # 特征重要度：gain + OOS 排列重要度（采样 ≤2 万行防爆内存）
    from sklearn.inspection import permutation_importance
    gain = model.get_booster().get_score(importance_type='gain')
    tot = sum(gain.values()) or 1.0
    gain_norm = {k: round(v / tot, 6) for k, v in
                 sorted(gain.items(), key=lambda kv: -kv[1])}
    te_sample = te_s if len(te_s) <= 20000 else te_s.sample(20000, random_state=42)
    perm = permutation_importance(model, te_sample[FEAT], te_sample[MAIN_LABEL].values,
                                  n_repeats=5, random_state=42, scoring='roc_auc', n_jobs=4)
    perm_d = {FEAT[k]: round(float(perm.importances_mean[k]), 6)
              for k in np.argsort(-perm.importances_mean)}

    mp = os.path.join(out_models, f'{ver}_{side.lower()}.json')
    model.save_model(mp)

    met = {
        'model': 'xgb', 'auc': round(auc, 4) if auc else None,
        'precision': round(float(precision_score(yte, pred, zero_division=0)), 4),
        'recall': round(float(recall_score(yte, pred, zero_division=0)), 4),
        'f1': round(float(f1_score(yte, pred, zero_division=0)), 4),
        'n_train': len(tr_s), 'n_test': len(te_s),
        'pos_rate_test': round(float(yte.mean()), 4),
        'train_time_s': train_s,
        'bin_monotonic': mono, 'top_bin_win': top_win, 'bins': bins,
        'cv_auc': round(float(np.mean(cv_aucs)), 4) if cv_aucs else None,
        'feature_imp': {'gain': gain_norm, 'perm': perm_d,
                        'top_features': list(gain_norm)[:25]},
    }
    return mp, met


# ========== 修复口径 OOS（round-trip，simulate_position_sm） ==========

_DAY_CACHE = {}


def _load_day_arrays(sym, date):
    """重载单日数据（优先 1m_clean，兜底 raw 1m），返回 (o,h,lo,c,v) 或 None。
    按标的缓存整文件（否则每个 OOS 日全量重解析 CSV，40 标的 × 45 天 ≈ 小时级）。"""
    if sym not in _DAY_CACHE:
        days = None
        for base in (DATA_CLEAN, DATA_RAW):
            path = os.path.join(base, f'{sym}_1m.csv')
            if not os.path.isfile(path):
                continue
            import csv as _csv
            days = {}
            with open(path, encoding='utf-8-sig') as fh:
                for r in _csv.DictReader(fh):
                    days.setdefault(r['trade_date'], []).append(r)
            break
        _DAY_CACHE[sym] = days or {}
    rows = _DAY_CACHE[sym].get(date)
    if not rows:
        return None
    rows = sorted(rows, key=lambda x: x['trade_time'])
    g = lambda k: np.array([float(x[k]) for x in rows])  # noqa
    return g('open'), g('high'), g('low'), g('close'), g('volume')


def roundtrip_oos(te, prob_by_rowkey):
    """修复口径：OOS 段逐标的 全量信号 vs ML 过滤 两臂 round-trip。

    prob_by_rowkey: {(symbol,date,idx,type): p}
    返回 per-side + pooled 对比指标。
    """
    out = {}
    for side in ('B', 'S'):
        te_s = te[te['type'] == side]
        base_trips, ml_trips = [], []
        for sym, g in te_s.groupby('symbol'):
            dates = sorted(g['date'].unique())
            sigs_by_day, prices_by_day = [], []
            prev_close = None
            for d in dates:
                arr = _load_day_arrays(sym, d)
                if arr is None:
                    continue
                o, h, lo, c, v = arr
                # PC 口径：需前一收盘；OOS 段首日用数据集内该日 pc 字段（构建时已按前收）
                day_rows = g[g['date'] == d]
                pc = float(day_rows['pc'].iloc[0]) if len(day_rows) else (prev_close or c[0])
                data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
                sigs_all, sigs_ml = [], []
                for _, r in day_rows.iterrows():
                    s = {'type': r['type'], 'idx': int(r['idx']),
                         'price': float(r['price']), 'reason': r.get('reason', '')}
                    sigs_all.append(s)
                    p = prob_by_rowkey.get((sym, d, int(r['idx']), r['type']))
                    if p is None or p >= ML_T_LO:
                        sigs_ml.append(s)
                prices = {'c': c, 'h': h, 'lo': lo, 'atr': data['atr'], 'n': len(c),
                          'trend': data['trend'], 'pc': pc, 'sym': sym, 'date': d}
                sigs_by_day.append((d, sigs_all))
                prices_by_day.append((d, prices))
                prev_close = float(c[-1])
            if not sigs_by_day:
                continue
            cost = cost_for_symbol(sym)
            rb = simulate_position_sm(sigs_by_day, prices_by_day, cost=cost, has_base=True)
            # ML 臂：重建 sigs_by_day（过滤版：p < ML_T_LO 的信号剔除；p 缺失=保留）
            sigs_ml_by_day = []
            for d, sigs_all_d in sigs_by_day:
                kept = []
                for s in sigs_all_d:
                    p = prob_by_rowkey.get((sym, d, int(s['idx']), s['type']))
                    if p is None or p >= ML_T_LO:
                        kept.append(s)
                sigs_ml_by_day.append((d, kept))
            rm = simulate_position_sm(sigs_ml_by_day, prices_by_day,
                                      cost=cost, has_base=True)
            base_trips.extend(rb['trips'])
            ml_trips.extend(rm['trips'])

        def _agg(trips):
            if not trips:
                return {'n_trips': 0}
            nets = [float(t['ret_pct']) for t in trips]  # ret_pct=净收益（已扣双边成本）
            return {'n_trips': len(nets),
                    'net_wr': round(float(np.mean([1 if x > 0 else 0 for x in nets])), 4),
                    'mean_net': round(float(np.mean(nets)), 4)}
        out[side] = {'baseline': _agg(base_trips), 'ml_filter': _agg(ml_trips)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=r'F:/keyfactor_data/ml_dataset_v2')
    ap.add_argument('--ver', default=None)
    ap.add_argument('--oos-frac', type=float, default=0.30)
    ap.add_argument('--skip-roundtrip', action='store_true')
    args = ap.parse_args()

    ver = args.ver or f"v2.0.0-ml-{datetime.datetime.now():%Y%m%d}"
    out_models = os.path.join(ROOT, 'output', 'ml_models')
    os.makedirs(out_models, exist_ok=True)

    df, parts = load_dataset(args.data)
    print(f'== 数据集: {len(parts)} 块, {len(df)} 行, '
          f'{df["symbol"].nunique()} 标的, {df["date"].nunique()} 天 ==')
    tr, te, split = time_split(df, args.oos_frac)
    print(f'== 时间切分: train ≤ {split} ({len(tr)} 行) | OOS > {split} ({len(te)} 行) ==')

    results = {'label': MAIN_LABEL, 'ver': ver, 'dataset': args.data,
               'n_samples': len(df), 'split_date': split,
               'trained_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'models': {}, 'pool_split': {}, 'oos_roundtrip': {}}

    prob_by_rowkey = {}
    model_files, model_hashes = {}, {}
    for side in ('B', 'S'):
        mp, met = train_side(side, tr, te, ver, out_models)
        results['models'][f'{side}_xgb'] = met
        if mp:
            model_files[side] = mp
            model_hashes[side] = _md5_16(mp)
            # 记录 OOS 概率（修复口径用）
            te_s = te[te['type'] == side].copy()
            import xgboost as xgb
            m = xgb.XGBClassifier()
            m.load_model(mp)
            te_s['prob'] = m.predict_proba(te_s[FEAT])[:, 1]
            for _, r in te_s.iterrows():
                prob_by_rowkey[(r['symbol'], r['date'], int(r['idx']), r['type'])] = \
                    float(r['prob'])
        print(f'  {side}: AUC={met.get("auc")} cv={met.get("cv_auc")} '
              f'top_bin_win={met.get("top_bin_win")} mono={met.get("bin_monotonic")} '
              f'(n_tr={met.get("n_train")} n_te={met.get("n_test")})')

    # 分池（双轨制：基金/个股分开看）
    for side in ('B', 'S'):
        met = results['models'].get(f'{side}_xgb') or {}
        if 'auc' not in met:
            continue
        for pool_name, pred_fn in (('fund', is_fund), ('stock', lambda s: not is_fund(s))):
            sub = te[(te['type'] == side) & te['symbol'].map(pred_fn)]
            if len(sub) >= 50 and sub[MAIN_LABEL].nunique() > 1:
                import xgboost as xgb
                m = xgb.XGBClassifier()
                m.load_model(model_files[side])
                p = m.predict_proba(sub[FEAT])[:, 1]
                from sklearn.metrics import roc_auc_score
                results['pool_split'][f'{side}_{pool_name}'] = {
                    'n': len(sub), 'auc': round(float(roc_auc_score(sub[MAIN_LABEL], p)), 4)}

    # 修复口径 OOS
    if not args.skip_roundtrip:
        print('== 修复口径 OOS（simulate_position_sm round-trip）==')
        results['oos_roundtrip'] = roundtrip_oos(te, prob_by_rowkey)
        for side, arms in results['oos_roundtrip'].items():
            b, m_ = arms['baseline'], arms['ml_filter']
            if b.get('n_trips'):
                lift = round((m_['net_wr'] - b['net_wr']) * 100, 2) if m_.get('n_trips') else None
                print(f'  {side}: base {b["n_trips"]} trips wr={b["net_wr"]} '
                      f'→ ml {m_["n_trips"]} trips wr={m_.get("net_wr")} (Δ{lift}pp)')

    # ===== 验收闸门 =====
    verdicts = []
    for side in ('B', 'S'):
        met = results['models'].get(f'{side}_xgb') or {}
        auc = met.get('auc')
        verdicts.append((f'{side} OOS AUC>={GATE_AUC}', auc is not None and auc >= GATE_AUC,
                         f'auc={auc}'))
        arms = results['oos_roundtrip'].get(side) or {}
        b, m_ = arms.get('baseline') or {}, arms.get('ml_filter') or {}
        if b.get('n_trips', 0) >= GATE_MIN_TRIPS and m_.get('n_trips', 0) > 0:
            lift_pp = (m_['net_wr'] - b['net_wr']) * 100
            kept = m_['n_trips'] / max(b['n_trips'], 1)
            ok = lift_pp >= GATE_WR_LIFT_PP and kept >= GATE_KEPT_FRAC
            verdicts.append((f'{side} round-trip wr lift>={GATE_WR_LIFT_PP}pp '
                             f'且 kept>={GATE_KEPT_FRAC:.0%}', ok,
                             f'lift={lift_pp:.2f}pp kept={kept:.0%}'))
        else:
            verdicts.append((f'{side} round-trip 样本', False,
                             f"base={b.get('n_trips')} ml={m_.get('n_trips')} "
                             f'< {GATE_MIN_TRIPS}（薄样本，只能观察）'))
    all_pass = all(v[1] for v in verdicts)
    results['gate'] = {'pass': all_pass,
                       'checks': [{'name': n, 'pass': ok, 'detail': d}
                                  for n, ok, d in verdicts]}

    # ===== 落盘 =====
    out_json = os.path.join(ROOT, 'output', 'ml_train_results_v2.json')
    with open(out_json, 'w', encoding='utf-8') as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print(f'\n结果: {out_json}')

    if model_files:
        reg_path = os.path.join(ROOT, 'output', 'ml_versions.json')
        with open(reg_path, encoding='utf-8') as fh:
            reg = json.load(fh)
        entry = {
            'ver': ver,
            'trained_date': results['trained_at'],
            'dataset_version': 'ml_dataset_v2',
            'label': MAIN_LABEL,
            'mhd': '0.15',
            'feature_names': FEAT,
            'model_files': {k: v for k, v in model_files.items()},
            'model_hashes': model_hashes,
            'oos_auc': {s: (results['models'].get(f'{s}_xgb') or {}).get('auc')
                        for s in ('B', 'S')},
            'label_pos_rate': {s: (results['models'].get(f'{s}_xgb') or {}).get('pos_rate_test')
                               for s in ('B', 'S')},
            'top_bin_win': {s: (results['models'].get(f'{s}_xgb') or {}).get('top_bin_win')
                            for s in ('B', 'S')},
            'oos_roundtrip': {s: arms for s, arms in results['oos_roundtrip'].items()},
            'gate_pass': all_pass,
        }
        reg = [e for e in reg if e.get('ver') != ver] + [entry]
        with open(reg_path, 'w', encoding='utf-8') as fh:
            json.dump(reg, fh, ensure_ascii=False, indent=1)
        print(f'注册: {reg_path} (ver={ver})')

    print(f'\n===== 闸门: {"PASS" if all_pass else "FAIL"} =====')
    for n, ok, d in verdicts:
        print(f'  {"✅" if ok else "❌"} {n}  ({d})')
    sys.exit(0 if all_pass else 1)


if __name__ == '__main__':
    main()
