# -*- coding: utf-8 -*-
"""scripts/topbot_replay.py — GT-TopBottom v1 三标的今日实盘回放（1m 分时 + 推送标注）

GT-TopBottom (GT-TB) = v10.9.0 策略层名（GT v1.0 引擎 + TopBottom 顶底 ML 过滤）。
回放流程：当日 1m → detect_signals_general (B/S) → ML 过滤(p<0.5 剔除) →
          simulate_day 正T(B→S/X) + simulate_bidirectional 反T(S→B/X) → matplotlib 出图。

数据：F:/keyfactor_data/1m/<sym>_1m.csv（同生产对账口径）；tick 特征在当前 watchlist 无覆盖，
      中性 0.5 填充（P10 同样口径，fail-open）。
输出：output/topbot_2026-08-26/{sym}.png × 3 + summary.json
"""
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from exit_manager import make_config, simulate_day, _mk_trip  # noqa: E402
from simulate_bidirectional import simulate_bidirectional  # noqa: E402
from daily_signal_review import build_data  # noqa: E402

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

DATE = '2026-08-26'
POOL = [('588170.SH', '科创半导体ETF'),
        ('300759.SZ', '康龙化成'),
        ('600721.SH', '百花医药')]
F_DATA_DIR = r'F:/keyfactor_data/1m'
MODEL_PATH = os.path.join(ROOT, 'data', 'ml', 'topbottom_xgb.json')
OUT_DIR = os.path.join(ROOT, 'output', f'topbot_{DATE}')

EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                       use_fixed_stop=True, fixed_stop_pct=1.5)
EXIT_CFG_SHORT = make_config()
ML_FEATURES = ['vwap_dev', 'rsi', 'trend', 'atr_pct',
               'tick_trade_count', 'tick_buy_ratio', 'tick_large_tape_count', 'tick_vwap_dev',
               'tick_hilo_range_pct', 'tick_direction_flow', 'tick_same_price_tape']

# 中文字体（防止方框）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def _load_ml():
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        import xgboost as xgb
        m = xgb.XGBClassifier()
        m.load_model(MODEL_PATH)
        return m
    except Exception:
        return None


def _score(ml, sigs, data):
    """ML 过滤：返回 [(sig, p)]。tick 特征中性 0.5（P10 同口径）。"""
    if ml is None or not sigs:
        return [(s, 1.0) for s in sigs]
    n = data['n']; c = data['c']
    out = []
    for s in sigs:
        i = s['idx']
        row = {
            'vwap_dev': float((c[i] - data['vwap'][i]) / data['vwap'][i] * 100.0) if data['vwap'][i] > 0 else 0.0,
            'rsi': float(data['rsi'][i]),
            'trend': int(data['trend'][i]),
            'atr_pct': float(data['atr'][i] / s['price'] * 100.0),
        }
        for col in ML_FEATURES[4:]:
            row[col] = 0.5
        df1 = pd.DataFrame([row])
        p = float(ml.predict_proba(df1[ML_FEATURES])[0, 1])
        out.append((s, p))
    return out


def _replay(sym, name, ml, thr=0.5):
    """单标的当日回放 → (b_marks, s_marks, x_marks, summary)。"""
    fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(fp, encoding='utf-8-sig')
    df['trade_date'] = df['trade_date'].astype(str)
    d = df[df['trade_date'] == DATE].sort_values('trade_time').reset_index(drop=True)
    if len(d) < 60:
        return None
    pc = float(d['close'].iloc[0]) * 0.999
    data = build_data(d, pc)
    sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
    scored = _score(ml, sigs, data)
    kept = [s for s, p in scored if p >= thr]
    filtered_n = len(scored) - len(kept)

    # 模拟 round-trip（用 kept 信号）
    long_trips = simulate_day(kept, data, EXIT_CFG, cost=None)
    short_trips = simulate_bidirectional(kept, data, config=EXIT_CFG_SHORT, cost=None)

    # 收集标注
    b_marks = []   # (idx_in_day, hhmm, price, score)
    s_marks = []
    x_marks = []   # (idx_in_day, hhmm, price, reason)
    sig_map = {s['idx']: s for s in kept}
    scored_map = {s['idx']: p for s, p in scored}

    # 入场标注（按侧）
    for s in kept:
        ts = str(d['trade_time'].iloc[s['idx']])[11:16]
        if s['type'] == 'B':
            b_marks.append((s['idx'], ts, s['price'], round(scored_map[s['idx']], 2)))
        else:
            s_marks.append((s['idx'], ts, s['price'], round(scored_map[s['idx']], 2)))

    # 出场标注（来自 trips：entry_idx/exit_idx/exit_reason）
    for t in long_trips:
        et = t['exit_idx']
        if et < len(d):
            ts = str(d['trade_time'].iloc[et])[11:16]
            x_marks.append((et, ts, t['exit_price'], t['exit_reason']))
    for t in short_trips:
        et = t['exit_idx']
        if et < len(d):
            ts = str(d['trade_time'].iloc[et])[11:16]
            x_marks.append((et, ts, t['exit_price'], t['exit_reason']))

    # 按 idx 排序
    b_marks.sort(); s_marks.sort(); x_marks.sort()

    long_pnl = sum(t['ret_pct'] for t in long_trips)
    short_pnl = sum(t['ret_pct'] for t in short_trips)
    summary = {
        'sym': sym, 'name': name, 'date': DATE,
        'n_signals_raw': len(scored), 'n_signals_kept': len(kept),
        'filtered_out': filtered_n, 'threshold': thr,
        'b_n': len(b_marks), 's_n': len(s_marks), 'x_n': len(x_marks),
        'long_trips': len(long_trips), 'short_trips': len(short_trips),
        'long_pnl_pct': round(long_pnl, 3),
        'short_pnl_pct': round(short_pnl, 3),
    }
    return {'df': d, 'b_marks': b_marks, 's_marks': s_marks, 'x_marks': x_marks,
            'summary': summary, 'scored': scored, 'kept': kept}


def _plot(result, out_path):
    df = result['df']
    s = result['summary']
    b, s_m, x_m = result['b_marks'], result['s_marks'], result['x_marks']
    fig, ax = plt.subplots(figsize=(13.5, 6.5), dpi=110)
    ax.plot(range(len(df)), df['close'].values, color='#1f77b4', linewidth=1.1, alpha=0.9)
    # 入场：B 红三角、S 绿倒三角
    for idx, hhmm, price, p in b:
        ax.scatter([idx], [price], marker='^', s=130, c='#d62728', zorder=5, edgecolors='white', linewidths=0.7)
        ax.annotate(f'B\n{hhmm}', (idx, price), xytext=(4, 12), textcoords='offset points',
                    fontsize=8, color='#d62728', fontweight='bold')
    for idx, hhmm, price, p in s_m:
        ax.scatter([idx], [price], marker='v', s=110, c='#2ca02c', zorder=5, edgecolors='white', linewidths=0.7)
        ax.annotate(f'S\n{hhmm}', (idx, price), xytext=(4, -16), textcoords='offset points',
                    fontsize=8, color='#2ca02c', fontweight='bold')
    # 出场：X 橙色叉
    for idx, hhmm, price, reason in x_m:
        ax.scatter([idx], [price], marker='X', s=130, c='#ff7f0e', zorder=6, edgecolors='white', linewidths=0.7)
        ax.annotate(f'X\n{hhmm}', (idx, price), xytext=(4, -16), textcoords='offset points',
                    fontsize=8, color='#ff7f0e', fontweight='bold')
    # X 轴：按 30 分钟打时间标签
    step = 30
    ticks = list(range(0, len(df), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(df['trade_time'].iloc[i])[11:16] for i in ticks], rotation=0, fontsize=9)
    title = f"{s['sym']} {s['name']}  {s['date']}  1m 分时 + GT-TopBottom 实盘推送标注  [B{s['b_n']}/S{s['s_n']}/X{s['x_n']}]"
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.set_ylabel('价格', fontsize=10)
    ax.set_xlabel('时间', fontsize=10)
    ax.grid(alpha=0.25, linewidth=0.5)
    ax.set_xlim(-2, len(df) + 2)
    # 图例
    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker='^', color='w', markerfacecolor='#d62728', markersize=10, label='B 买入'),
        Line2D([0], [0], marker='v', color='w', markerfacecolor='#2ca02c', markersize=10, label='S 卖出/反T'),
        Line2D([0], [0], marker='X', color='w', markerfacecolor='#ff7f0e', markersize=10, label='X 出场'),
    ]
    ax.legend(handles=legend, loc='upper right', fontsize=9, framealpha=0.9)
    # 副标题：净值
    pnl = s['long_pnl_pct'] + s['short_pnl_pct']
    fig.text(0.01, 0.01,
             f"GT-TB thr≥{s['threshold']} | raw={s['n_signals_raw']} kept={s['n_signals_kept']} filtered={s['filtered_out']} | "
             f"正T:{s['long_trips']}笔 {s['long_pnl_pct']:+.2f}%  反T:{s['short_trips']}笔 {s['short_pnl_pct']:+.2f}%  双向:{pnl:+.2f}%",
             fontsize=8, color='#444')
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches='tight')
    plt.close(fig)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ml = _load_ml()
    print(f'GT-TopBottom v1 回放 {DATE} ｜ ML loaded={ml is not None}｜ tick 特征中性填充（watchlist 无 tick 数据）')
    summary = []
    for sym, name in POOL:
        try:
            r = _replay(sym, name, ml, thr=0.5)
        except Exception as e:
            print(f'  {sym}: 失败 {e!r}')
            continue
        if r is None:
            print(f'  {sym}: 当日数据不足')
            continue
        out_png = os.path.join(OUT_DIR, f'{sym.replace(".", "_")}_topbot.png')
        _plot(r, out_png)
        s = r['summary']
        summary.append(s)
        print(f"  {sym} {name}: kept={s['n_signals_kept']}/{s['n_signals_raw']}  "
              f"B={s['b_n']} S={s['s_n']} X={s['x_n']}  正T{s['long_pnl_pct']:+.2f}% 反T{s['short_pnl_pct']:+.2f}%  → {out_png}")
    with open(os.path.join(OUT_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'date': DATE, 'pool': [{'sym': s['sym'], 'name': s['name']} for s in summary],
                   'per_sym': summary, 'algorithm': 'GT-TopBottom v1'}, f, ensure_ascii=False, indent=2)
    print(f'\n汇总：output/{os.path.basename(OUT_DIR)}/summary.json + 3 PNG')


if __name__ == '__main__':
    main()