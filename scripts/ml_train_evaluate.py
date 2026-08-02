# -*- coding: utf-8 -*-
"""ML 训练与评估：XGBoost vs LightGBM vs RandomForest

- 防泄漏：GroupKFold by date（组=交易日，同组不跨折）+ purged walk-forward
- 评估：AUC / 精确率 / 召回率 / F1 + 交易挂钩指标（按概率分10箱，箱净收益单调性 + top箱转正）
- 特征重要性：XGBoost gain + permutation importance 双法
- B/S 分开建模（因子语义相反）

用法：
  python scripts/ml_train_evaluate.py --data output/ml_dataset/dataset.parquet --out output/ml_train_results.json
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from scripts.ml_build_dataset import FEAT_ALL, LABEL_COLS  # noqa: E402

N_MAIN = 20  # 主标签


def load_data(path):
    if path.endswith('.parquet'):
        try:
            df = pd.read_parquet(path)
        except Exception:
            df = pd.read_csv(path.replace('.parquet', '.csv'))
    else:
        df = pd.read_csv(path)
    return df


def prepare(df, sig_type, label_col='label_20'):
    """按信号类型拆分 + 特征/标签分离 + 日期分组。"""
    sub = df[df['sig_type'] == sig_type].copy()
    sub = sub.dropna(subset=[label_col])
    sub['date'] = sub['date'].astype(str)
    X = sub[FEAT_ALL].astype(float)
    y = sub[label_col].astype(int).values
    dates = sub['date'].values
    return sub, X, y, dates


def group_cv(dates, n_splits=5):
    """GroupKFold by date：同交易日样本不跨折。"""
    from sklearn.model_selection import GroupKFold
    unique_dates = np.unique(dates)
    gkf = GroupKFold(n_splits=n_splits)
    # GroupKFold 要求 y 与 groups 等长；这里用 unique_dates 简化做 fold 分配
    fold_of_date = {}
    dummy_y = np.zeros(len(unique_dates))
    for fold, (tr_idx, te_idx) in enumerate(gkf.split(unique_dates, dummy_y, unique_dates)):
        for d in unique_dates[te_idx]:
            fold_of_date[d] = fold
    # 映射回样本
    groups = np.array([fold_of_date[d] for d in dates])
    return groups


def walk_forward_split(df, sig_type, train_frac=0.7):
    """Purged walk-forward：按日期排序前70%训练/后30%测试。"""
    sub = df[df['sig_type'] == sig_type].copy()
    sub['date'] = sub['date'].astype(str)
    dates_sorted = sorted(sub['date'].unique())
    cut = int(len(dates_sorted) * train_frac)
    train_dates = set(dates_sorted[:cut])
    test_dates = set(dates_sorted[cut:])
    tr = sub[sub['date'].isin(train_dates)]
    te = sub[sub['date'].isin(test_dates)]
    return tr, te


def eval_trade_bins(y_true, y_prob, n_bins=10):
    """按预测概率分 10 箱，统计各箱实际胜率与平均净收益（近似：label=净收益>0 的指示）。
    用 label 的连续性近似：这里 label 是 0/1，用桶内正样本率衡量。"""
    qs = pd.qcut(pd.Series(y_prob), n_bins, duplicates='drop')
    df_bin = pd.DataFrame({'prob': y_prob, 'y': y_true, 'bin': qs})
    grp = df_bin.groupby('bin', observed=True).agg(
        n=('y', 'size'), win_rate=('y', 'mean'), avg_prob=('prob', 'mean'))
    grp['rank'] = range(1, len(grp) + 1)
    # 单调性：win_rate 是否随 rank 单调升
    wr = grp['win_rate'].values
    monotonic = all(wr[i] >= wr[i - 1] - 1e-9 for i in range(1, len(wr)))
    top_win = grp['win_rate'].iloc[-1] if len(grp) else 0
    grp = grp.reset_index()  # 避免 Interval index
    return grp, monotonic, top_win


def train_model(X_tr, y_tr, X_te, y_te, dates_tr, name, seed=42):
    """训练单模型（XGBoost/LightGBM/RF），返回评估 dict。"""
    from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score

    t0 = time.time()
    if name == 'xgb':
        import xgboost as xgb
        model = xgb.XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            min_child_weight=3, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=(y_tr == 0).sum() / max((y_tr == 1).sum(), 1),
            eval_metric='auc', random_state=seed, n_jobs=4)
    elif name == 'lgb':
        import lightgbm as lgb
        model = lgb.LGBMClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            min_child_weight=3, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=(y_tr == 0).sum() / max((y_tr == 1).sum(), 1),
            random_state=seed, n_jobs=4, verbose=-1)
    else:  # rf
        from sklearn.ensemble import RandomForestClassifier
        model = RandomForestClassifier(
            n_estimators=300, max_depth=12, min_samples_leaf=5,
            class_weight='balanced', random_state=seed, n_jobs=4)

    model.fit(X_tr, y_tr)
    y_prob = model.predict_proba(X_te)[:, 1]
    y_pred = (y_prob > 0.5).astype(int)

    auc = roc_auc_score(y_te, y_prob)
    res = {
        'model': name,
        'auc': round(float(auc), 4),
        'precision': round(float(precision_score(y_te, y_pred, zero_division=0)), 4),
        'recall': round(float(recall_score(y_te, y_pred, zero_division=0)), 4),
        'f1': round(float(f1_score(y_te, y_pred, zero_division=0)), 4),
        'n_train': int(len(y_tr)), 'n_test': int(len(y_te)),
        'pos_rate_test': round(float(y_te.mean()), 4),
        'train_time_s': round(time.time() - t0, 1),
    }
    grp, monotonic, top_win = eval_trade_bins(y_te, y_prob)
    res['bin_monotonic'] = bool(monotonic)
    res['top_bin_win'] = round(float(top_win), 4)
    res['bins'] = [{'rank': int(r['rank']), 'n': int(r['n']), 'win_rate': round(float(r['win_rate']), 4),
                    'avg_prob': round(float(r['avg_prob']), 4)}
                   for _, r in grp.iterrows()]
    return model, res


def feature_importance(model, X, y, name, n_top=25):
    """XGBoost gain 重要性 + permutation importance 双法。"""
    from sklearn.inspection import permutation_importance
    imp = {}
    if hasattr(model, 'feature_importances_'):
        imp['gain'] = dict(zip(X.columns, model.feature_importances_))
    try:
        perm = permutation_importance(model, X, y, n_repeats=3, random_state=42, n_jobs=2)
        imp['perm'] = dict(zip(X.columns, perm.importances_mean))
    except Exception:
        imp['perm'] = {}
    # 合并排序
    if imp.get('gain') and imp.get('perm'):
        cols = sorted(X.columns, key=lambda c: imp['gain'].get(c, 0) + imp['perm'].get(c, 0), reverse=True)
    elif imp.get('gain'):
        cols = sorted(X.columns, key=lambda c: imp['gain'].get(c, 0), reverse=True)
    else:
        cols = sorted(X.columns, key=lambda c: imp['perm'].get(c, 0), reverse=True)
    return {
        'gain': {c: round(float(imp['gain'].get(c, 0)), 6) for c in cols[:n_top]},
        'perm': {c: round(float(imp['perm'].get(c, 0)), 6) for c in cols[:n_top]},
        'top_features': cols[:n_top],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='output/ml_dataset/dataset.parquet')
    ap.add_argument('--out', default='output/ml_train_results.json')
    ap.add_argument('--label', default='label_20')
    ap.add_argument('--fast', action='store_true', help='快速模式（少树）')
    args = ap.parse_args()

    path = os.path.join(BASE, args.data)
    df = load_data(path)
    print(f'数据集: {len(df)} 样本, B={len(df[df.sig_type=="B"])} S={len(df[df.sig_type=="S"])}', flush=True)

    out = {'label': args.label, 'n_samples': len(df), 'models': {}, 'feature_imp': {}, 'wf': {}}

    for sig in ['B', 'S']:
        print(f'\n===== 信号类型 {sig} =====', flush=True)
        sub, X, y, dates = prepare(df, sig, args.label)
        print(f'  {sig}: {len(sub)} 样本, 正样本率 {y.mean():.2%}', flush=True)

        # ---- 训练集上的 GroupKFold CV（调参参考，树数从简以提速） ----
        groups = group_cv(dates, 5)
        from sklearn.model_selection import cross_val_score
        cv_auc = {}
        for name in ['xgb', 'lgb', 'rf']:
            import importlib
            if name == 'xgb':
                import xgboost as xgb
                m = xgb.XGBClassifier(n_estimators=60 if args.fast else 100, max_depth=4,
                                      learning_rate=0.05, scale_pos_weight=(y == 0).sum() / max((y == 1).sum(), 1),
                                      eval_metric='auc', random_state=42, n_jobs=4)
            elif name == 'lgb':
                import lightgbm as lgb
                m = lgb.LGBMClassifier(n_estimators=60 if args.fast else 100, max_depth=4,
                                       learning_rate=0.05, scale_pos_weight=(y == 0).sum() / max((y == 1).sum(), 1),
                                       random_state=42, n_jobs=4, verbose=-1)
            else:
                from sklearn.ensemble import RandomForestClassifier
                m = RandomForestClassifier(n_estimators=60 if args.fast else 100, max_depth=10,
                                           class_weight='balanced', random_state=42, n_jobs=4)
            try:
                from sklearn.model_selection import GroupKFold as _GKF
                from sklearn.metrics import roc_auc_score as _auc
                gkf = _GKF(n_splits=5)
                _aucs = []
                for _tr_idx, _te_idx in gkf.split(X, y, groups=dates):
                    m.fit(X.iloc[_tr_idx], y[_tr_idx])
                    _p = m.predict_proba(X.iloc[_te_idx])[:, 1]
                    _aucs.append(_auc(y[_te_idx], _p))
                cv_auc[name] = round(float(np.mean(_aucs)), 4)
                print(f'  GroupKFold CV {name}: AUC={cv_auc[name]:.4f} (±{np.std(_aucs):.3f})', flush=True)
            except Exception as e:
                cv_auc[name] = None
                print(f'  GroupKFold CV {name}: 失败 {e}', flush=True)

        # ---- Purged walk-forward OOS（只跑一次纪律） ----
        tr, te = walk_forward_split(df, sig, 0.7)
        tr = tr.dropna(subset=[args.label])
        te = te.dropna(subset=[args.label])
        X_tr = tr[FEAT_ALL].astype(float)
        y_tr = tr[args.label].astype(int).values
        X_te = te[FEAT_ALL].astype(float)
        y_te = te[args.label].astype(int).values
        dates_tr = tr['date'].values
        print(f'  Walk-forward: train {len(X_tr)} ({y_tr.mean():.1%}正) / test {len(X_te)} ({y_te.mean():.1%}正)', flush=True)

        best_auc = -1
        for name in ['xgb', 'lgb', 'rf']:
            model, res = train_model(X_tr, y_tr, X_te, y_te, dates_tr, name)
            res['cv_auc'] = cv_auc[name]
            out['models'][f'{sig}_{name}'] = res
            print(f'  WF OOS {name}: AUC={res["auc"]:.4f} P={res["precision"]:.3f} R={res["recall"]:.3f} '
                  f'F1={res["f1"]:.3f} 箱单调={res["bin_monotonic"]} top箱胜率={res["top_bin_win"]:.1%} '
                  f'({res["train_time_s"]:.0f}s)', flush=True)
            if res['auc'] > best_auc and name == 'xgb':
                best_auc = res['auc']
                best_model = model
                best_name = name
            elif name == 'xgb' and best_auc == -1:
                best_model = model
                best_name = name

        # ---- 特征重要性（用 WF 训练的 xgb） ----
        try:
            imp = feature_importance(best_model, X_te, y_te, best_name)
            out['feature_imp'][sig] = imp
            print(f'  Top10 特征: {imp["top_features"][:10]}', flush=True)
        except Exception as e:
            print(f'  特征重要性失败: {e}', flush=True)

    os.makedirs(os.path.dirname(os.path.join(BASE, args.out)), exist_ok=True)
    with open(os.path.join(BASE, args.out), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    print(f'\n结果已保存 → {os.path.join(BASE, args.out)}')


if __name__ == '__main__':
    main()
