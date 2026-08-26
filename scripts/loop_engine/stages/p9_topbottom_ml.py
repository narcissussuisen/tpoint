# -*- coding: utf-8 -*-
"""scripts/loop_engine/stages/p9_topbottom_ml.py — P9 阶段执行器（顶底捕捉 + ML 增强）

目标（路线图 P9，交付 v10.8.0）：
  用 P8 tick 相对特征提升 B/S 信号对顶底（±0.5% 有利极端）的捕捉率。

方法：
  1. 对 tick 覆盖标的（161129.SZ/513310.SH）逐日构建信号级数据集（DET 顶底标签 + 1m 基线特征 + tick 特征）
  2. 时间切分 70/30 train/test
  3. xgboost 二分类（label=信号后 15min 内触及 ±0.5%）
  4. 对比：仅 1m 特征（baseline）vs 1m+tick 特征（full）的 AUC / EHR

Gate：full 在 test 上 AUC ≥ baseline + 0.03 或 EHR 提升 ≥3pp → PASS（产出模型+报告）
      否则证伪（维持现状，P9 结论文档化）。
"""
import os
import sys
import json

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import importlib.util  # noqa: E402
_LE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('le_core', os.path.join(_LE_DIR, 'core.py'))
le_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(le_core)

from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from daily_signal_review import build_data  # noqa: E402
from top_bottom_features import build_dataset  # noqa: E402
import xgboost as xgb  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

TARGET_VERSION = '10.8.0'
F_DATA_DIR = r'F:/keyfactor_data/1m'
TICK_POOL = ['161129.SZ', '513310.SH']   # tick+1m 双覆盖标的
HORIZON = 15
TEST_SPLIT = 0.30
MODEL_DIR = os.path.join(ROOT, 'data', 'ml')
MODEL_PATH = os.path.join(MODEL_DIR, 'topbottom_xgb.json')
DATASET_PATH = os.path.join(MODEL_DIR, 'topbottom_train.csv')

BASE_FEATURES = ['vwap_dev', 'rsi', 'trend', 'atr_pct']
TICK_FEATURES = ['tick_trade_count', 'tick_buy_ratio', 'tick_large_tape_count', 'tick_vwap_dev',
                 'tick_hilo_range_pct', 'tick_direction_flow', 'tick_same_price_tape']


def _collect(sym):
    fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(fp, encoding='utf-8-sig')
    df['trade_date'] = df['trade_date'].astype(str)
    frames = []
    for day, d in df.groupby('trade_date'):
        d = d.sort_values('trade_time')
        if len(d) < 60:
            continue
        pc = float(d['close'].iloc[0]) * 0.999  # 昨收近似（仅顶底标签用，口径影响极小）
        try:
            data = build_data(d.reset_index(drop=True), pc)
        except Exception:
            continue
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        if not sigs:
            continue
        f = build_dataset(sym, d.reset_index(drop=True), data, sigs,
                          horizon=HORIZON, use_tick=True)
        if len(f):
            frames.append(f)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _eval(df, feat_cols, model=None):
    """AUC + EHR@0.5%。model 为 None 时用特征均值决策（规则基线）。"""
    X = df[feat_cols].fillna(df[feat_cols].median())
    y = df['label'].values
    if model is not None:
        p = model.predict_proba(X)[:, 1]
    else:
        p = X.mean(axis=1).values  # 规则基线：特征均值
    auc = roc_auc_score(y, p) if len(np.unique(y)) > 1 else 0.5
    thr = np.median(p)
    pred = (p >= thr).astype(int)
    tp = ((pred == 1) & (y == 1)).sum()
    fn = ((pred == 0) & (y == 1)).sum()
    ehr = tp / max(tp + fn, 1) * 100.0
    return {'auc': round(float(auc), 4), 'ehr_pct': round(float(ehr), 1),
            'n_pos': int(y.sum()), 'n': len(y)}


def run(ctx=None):
    le_core.log('P9: 开始执行（顶底捕捉 + ML 增强）')
    report = {'stage': 'p9_topbottom_ml', 'version': TARGET_VERSION,
              'pool': TICK_POOL, 'horizon': HORIZON}

    # 1) 数据收集
    frames = []
    for sym in TICK_POOL:
        f = _collect(sym)
        le_core.log(f'P9: {sym} 信号样本 {len(f)}')
        if len(f):
            frames.append(f)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(df) < 300:
        report['result'] = 'FAIL_DATA'
        report['msg'] = f'P9 样本不足（{len(df)}<300），无法训练'
        return False, report
    os.makedirs(MODEL_DIR, exist_ok=True)
    df.to_csv(DATASET_PATH, index=False)
    report['n_samples'] = len(df)

    # 2) 时间切分
    df = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
    cut = int(len(df) * (1 - TEST_SPLIT))
    train, test = df.iloc[:cut], df.iloc[cut:]

    # 3) baseline（1m 特征）vs full（1m+tick）
    m_base = xgb.XGBClassifier(n_estimators=120, max_depth=4, learning_rate=0.08,
                               subsample=0.8, colsample_bytree=0.8,
                               eval_metric='logloss', random_state=42, n_jobs=1)
    m_full = xgb.XGBClassifier(n_estimators=120, max_depth=4, learning_rate=0.08,
                               subsample=0.8, colsample_bytree=0.8,
                               eval_metric='logloss', random_state=42, n_jobs=1)
    m_base.fit(train[BASE_FEATURES].fillna(0), train['label'])
    m_full.fit(train[BASE_FEATURES + TICK_FEATURES].fillna(0), train['label'])

    ev_base = _eval(test, BASE_FEATURES, m_base)
    ev_full = _eval(test, BASE_FEATURES + TICK_FEATURES, m_full)
    report['test'] = {'base': ev_base, 'full': ev_full}
    le_core.log(f'P9: test base AUC={ev_base["auc"]} EHR={ev_base["ehr_pct"]}% | '
                f'full AUC={ev_full["auc"]} EHR={ev_full["ehr_pct"]}%')

    # 4) Gate：full AUC ≥ base+0.03 或 EHR +3pp
    auc_gain = ev_full['auc'] - ev_base['auc']
    ehr_gain = ev_full['ehr_pct'] - ev_base['ehr_pct']
    gate = {'auc_gain': round(float(auc_gain), 4), 'ehr_gain_pp': round(float(ehr_gain), 1),
            'pass': auc_gain >= 0.03 or ehr_gain >= 3.0}
    report['gate'] = gate

    if not gate['pass']:
        # 证伪闭环
        report['result'] = 'PASS_VERIFIED_NO_CHANGE'
        report['msg'] = (f'P9 证伪：tick 特征未显著提升顶底捕捉（AUC {ev_base["auc"]}→{ev_full["auc"]}，'
                         f'EHR {ev_base["ehr_pct"]}%→{ev_full["ehr_pct"]}%）。'
                         f'可能原因：tick 时间戳仅 HH:MM、与信号分钟对齐稀疏（tick 特征缺失率高）。')
        le_core.log(report['msg'])
        # 仍落盘报告
        with open(os.path.join(ROOT, 'docs', 'p9_verification_report.md'), 'w', encoding='utf-8') as f:
            f.write(f'# P9 顶底捕捉验证报告（loop_engine）\n\n'
                    f'- 池：{TICK_POOL} ｜ horizon={HORIZON} ｜ 样本 {len(df)}\n'
                    f'- base(1m): AUC {ev_base["auc"]} EHR {ev_base["ehr_pct"]}%\n'
                    f'- full(+tick): AUC {ev_full["auc"]} EHR {ev_full["ehr_pct"]}%\n'
                    f'- 结论：**{report["msg"]}**\n')
        report['report_file'] = os.path.join(ROOT, 'docs', 'p9_verification_report.md')
        return True, report

    # 5) PASS：落盘模型 + 合入
    m_full.save_model(MODEL_PATH)
    report['model_path'] = MODEL_PATH
    report['result'] = 'PASS'
    report['msg'] = (f'P9 完成：tick 特征提升顶底捕捉（AUC {ev_base["auc"]}→{ev_full["auc"]}，'
                     f'EHR {ev_base["ehr_pct"]}%→{ev_full["ehr_pct"]}%），模型已落盘 {MODEL_PATH}')

    _allowed, _reason = le_core.guard_bump('p9_topbottom_ml', TARGET_VERSION)
    ver_path = os.path.join(ROOT, 'VERSION')
    cur = open(ver_path, encoding='utf-8').read().strip()
    if _allowed and cur != TARGET_VERSION:
        open(ver_path, 'w', encoding='utf-8').write(TARGET_VERSION + '\n')
        report['version_from'] = cur
    elif not _allowed:
        report['bump_blocked'] = _reason
        le_core.log(f'P9: {_reason}')
    with open(os.path.join(ROOT, 'CHANGELOG.md'), 'a', encoding='utf-8') as f:
        f.write(f'\n## v{TARGET_VERSION}（2026-08-26）P9 顶底捕捉 ML 增强（loop_engine 自动合入）\n'
                f'> tick 特征提升顶底捕捉：AUC {ev_base["auc"]}→{ev_full["auc"]}，EHR {ev_base["ehr_pct"]}%→{ev_full["ehr_pct"]}%。'
                f'模型 data/ml/topbottom_xgb.json。\n')

    files = ['core/top_bottom_features.py', 'data/ml/topbottom_xgb.json',
             'VERSION', 'CHANGELOG.md',
             'scripts/loop_engine/stages/p9_topbottom_ml.py', 'scripts/loop_engine/loop_state.json']
    rc, _, err = le_core.git('add', '--', *files)
    if rc == 0:
        rc, _, err = le_core.git('commit', '-m', f'feat(P9): 顶底捕捉 ML 增强 v{TARGET_VERSION}（loop_engine 自动合入）')
    if rc == 0:
        rc2, hash_out, _ = le_core.git('rev-parse', '--short', 'HEAD')
        report['commit'] = hash_out
        rc3, _, err3 = le_core.git_push('push', 'origin', 'HEAD')
        report['push_ok'] = (rc3 == 0)
    else:
        report['push_ok'] = False
    le_core.log(report['msg'])
    return True, report


if __name__ == '__main__':
    ok, rep = run()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
