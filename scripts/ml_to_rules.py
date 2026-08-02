# -*- coding: utf-8 -*-
"""ML→规则落地：特征分箱净收益表 → 规则参数推荐

核心思想（与"因子研究工具"定位一致）：
  对每个高重要度特征按值分箱，统计各箱的前向净收益均值/胜率，
  直接产出"该特征在哪个区间信号质量最好"的规则建议——零模型依赖。

  对候选规则参数（min_hist_diff / VWAP_DEV / RSI阈值 / KDJ / 时段），
  用分箱表验证"当前值是否在最优区间"，并给出可落地参数推荐。

用法：
  python scripts/ml_to_rules.py --data output/ml_dataset_full/dataset.csv --out output/ml_rules.json
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from scripts.ml_build_dataset import FEAT_ALL  # noqa: E402

# 需要分箱分析的特征（含候选规则参数对应量）
BIN_FEATURES = {
    # 特征名: (分箱数, 说明)
    'hist_pct': (12, 'MACD柱强度（%）— 对应 min_hist_diff 阈值选择'),
    'vwap_dev': (12, 'VWAP偏离（%）— 对应 VWAP_DEV 引力带'),
    'atr_pct': (10, 'ATR/价格（%）— 波动率特征'),
    'rsi': (10, 'RSI(14) — 超买超卖阈值选择'),
    'kdj_k': (10, 'KDJ K值 — 超买超卖阈值选择'),
    'kdj_j': (10, 'KDJ J值（更敏感）'),
    'mom_5': (12, '5bar动量（%）— 动量反转特征'),
    'chg': (12, '当日涨跌幅（%）— 日内趋势状态'),
    'pos_in_day': (8, '日内位置 — 时段特征'),
    'is_tail': (2, '是否尾盘(14:30后) — 尾盘风控验证'),
    'is_morning': (2, '是否早盘(9:30-10:00)'),
    'resonance': (4, '共振分数'),
    'trend': (3, '趋势状态'),
    'trend_strong': (3, '强趋势状态'),
}

# 候选规则参数：从分箱结果推导推荐值
RULE_CANDIDATES = [
    {'param': 'min_hist_diff', 'feature': 'hist_pct', 'note': 'MACD背离强度阈值（0=全放行）'},
    {'param': 'VWAP_DEV', 'feature': 'vwap_dev', 'note': 'VWAP引力带倍数×ATR'},
    {'param': 'RSI_oversold', 'feature': 'rsi', 'note': 'RSI超卖阈值'},
    {'param': 'RSI_overbought', 'feature': 'rsi', 'note': 'RSI超买阈值'},
    {'param': 'KDJ_K_buy', 'feature': 'kdj_k', 'note': 'KDJ K超卖阈值'},
    {'param': 'KDJ_K_sell', 'feature': 'kdj_k', 'note': 'KDJ K超买阈值'},
    {'param': 'tail_gate', 'feature': 'is_tail', 'note': '尾盘(14:30后)是否禁新仓'},
]


def bin_analysis(df, sig_type, feature, n_bins, label_col='label_20'):
    """对某信号类型的某特征分箱，统计前向净收益（用 label 均值近似胜率）。

    连续特征用分位数分箱；低基数离散特征（is_tail/is_morning/pos_in_day等）
    直接用原始取值分组，避免 qcut 退化。"""
    sub = df[df['sig_type'] == sig_type].dropna(subset=[feature, label_col])
    if len(sub) < 50:
        return None
    n_unique = sub[feature].nunique()
    # 低基数 → 直接按取值分组
    if n_unique <= n_bins:
        grp = sub.groupby(feature).agg(
            n=('price', 'size'),
            win_rate=(label_col, 'mean'),
            fwd_ret_approx=(label_col, 'mean'),
        ).reset_index()
        grp = grp.rename(columns={feature: '_bin'})
        grp['feat_range'] = grp['_bin'].map(lambda v: f'={v}')
    else:
        try:
            qs = pd.qcut(sub[feature], n_bins, duplicates='drop', labels=False)
        except Exception:
            return None
        sub = sub.assign(_bin=qs)
        grp = sub.groupby('_bin').agg(
            n=('price', 'size'),
            win_rate=(label_col, 'mean'),
            fwd_ret_approx=(label_col, 'mean'),  # label 均值 = 净收益>0比例（近似）
        ).reset_index()
        # 加特征区间
        edges = pd.qcut(sub[feature], n_bins, duplicates='drop', retbins=True)[1]
        grp['feat_range'] = [f'[{edges[i]:.3f},{edges[i+1]:.3f}]' for i in range(len(edges) - 1)]
    grp = grp.sort_values('_bin')
    return grp


def recommend_from_bins(grp, feature, param, sig_type, base_win):
    """从分箱表推导规则推荐（找 win_rate 显著高于基线的箱区间）。"""
    if grp is None or len(grp) < 3:
        return None
    g = grp.copy()
    g['lift'] = g['win_rate'] - base_win
    g = g[g['n'] >= 30]
    if len(g) < 2:
        return None
    # 最高收益箱 + 样本量门槛
    best = g.sort_values('win_rate', ascending=False).iloc[0]
    worst = g.sort_values('win_rate').iloc[0]
    best_range = best['feat_range']
    # 单调性
    wr = g.sort_values('_bin')['win_rate'].values
    monotonic_up = all(wr[i] >= wr[i - 1] - 0.02 for i in range(1, len(wr)))
    monotonic_dn = all(wr[i] <= wr[i - 1] + 0.02 for i in range(1, len(wr)))
    direction = '升' if monotonic_up else ('降' if monotonic_dn else '非单调')
    return {
        'param': param, 'feature': feature, 'sig_type': sig_type,
        'base_win': round(base_win, 4),
        'best_bin': best_range, 'best_win': round(float(best['win_rate']), 4),
        'worst_bin': worst['feat_range'], 'worst_win': round(float(worst['win_rate']), 4),
        'lift': round(float(best['win_rate'] - base_win), 4),
        'monotonic': direction,
        'n': int(g['n'].sum()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='output/ml_dataset_full/dataset.csv')
    ap.add_argument('--out', default='output/ml_rules.json')
    ap.add_argument('--label', default='label_20')
    args = ap.parse_args()

    path = os.path.join(BASE, args.data)
    df = pd.read_csv(path)
    print(f'数据集: {len(df)} 样本, B={len(df[df.sig_type=="B"])} S={len(df[df.sig_type=="S"])}', flush=True)

    out = {'bins': {}, 'recommendations': []}
    for sig in ['B', 'S']:
        base_win = df[df['sig_type'] == sig][args.label].mean()
        print(f'\n===== {sig} 信号（基线胜率 {base_win:.2%}）=====', flush=True)
        for feat, (n_bins, note) in BIN_FEATURES.items():
            grp = bin_analysis(df, sig, feat, n_bins, args.label)
            if grp is None:
                continue
            out['bins'][f'{sig}_{feat}'] = {
                'note': note,
                'rows': [{'_bin': int(r['_bin']), 'n': int(r['n']),
                          'win_rate': round(float(r['win_rate']), 4),
                          'range': r['feat_range']}
                         for _, r in grp.iterrows()],
            }
            # 打印 top/bottom 箱
            if len(grp) >= 4:
                g = grp[grp['n'] >= 30]
                if len(g) >= 2:
                    best = g.sort_values('win_rate', ascending=False).iloc[0]
                    worst = g.sort_values('win_rate').iloc[0]
                    print(f'  {feat:14s} [{note[:18]}]: 最好箱={best["feat_range"]} '
                          f'胜率{best["win_rate"]:.1%} 最差箱={worst["feat_range"]} '
                          f'胜率{worst["win_rate"]:.1%}', flush=True)

    # 规则推荐
    print('\n===== 规则参数推荐 =====', flush=True)
    for rc in RULE_CANDIDATES:
        for sig in ['B', 'S']:
            grp = bin_analysis(df, sig, rc['feature'], BIN_FEATURES[rc['feature']][0], args.label)
            base_win = df[df['sig_type'] == sig][args.label].mean()
            rec = recommend_from_bins(grp, rc['feature'], rc['param'], sig, base_win)
            if rec:
                out['recommendations'].append(rec)
                print(f"  [{rc['param']}] {sig}: 基线{rec['base_win']:.1%} → "
                      f"最优箱{rec['best_bin']} 胜率{rec['best_win']:.1%} "
                      f"(lift {rec['lift']:+.1%}, {rec['monotonic']})", flush=True)

    with open(os.path.join(BASE, args.out), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    print(f'\n已保存 → {os.path.join(BASE, args.out)}')


if __name__ == '__main__':
    main()
