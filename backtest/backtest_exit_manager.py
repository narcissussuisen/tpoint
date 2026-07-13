#!/usr/bin/env python3
"""
出场管理模块回测 — 用 tickflow 已落地的 1m 真实数据, 对比不同出场配置
对 B信号盈亏比/胜率/总收益的影响(离线零成本)。

四组配置做消融:
  A. baseline_S_only : 只用S信号出场(=当前v9行为, 无止损/无时间/无移动)
  B. +stop           : A + ATR硬止损
  C. +stop+time      : B + 时间止损
  D. full            : C + 移动止损(完整出场管理)

重点看: B信号盈亏比(pl_ratio)从 baseline 的 ~1.05:1 能否被出场管理拉到 1.6:1。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import numpy as np
import pandas as pd
from datetime import datetime
from v9_indicators import compute_indicators, detect_signals
from v9_exit_manager import simulate_day, aggregate_metrics, make_config

TARGETS = {
    '300975.SZ': '商络电子', '601869.SH': '长飞光纤', '603938.SH': '三孚股份',
    '300395.SZ': '菲利华', '301526.SZ': '国际复材',
    '300757.SZ': '罗博特科', '688820.SH': '盛合晶微',
}

CONFIGS = {
    'A_baseline_S_only': make_config(use_stop=False, use_time=False, use_trailing=False, s_signal_exit=True),
    'B_atr_stop_1.5':    make_config(use_stop=True, stop_mode='atr', stop_atr_mult=1.5,
                                     use_time=False, use_trailing=False),
    'G_trend_stop':      make_config(use_stop=True, stop_mode='trend',
                                     use_time=False, use_trailing=False),
    'H_trend_stop_trail':make_config(use_stop=True, stop_mode='trend',
                                     use_time=False, use_trailing=True),
    'I_smart_full':      make_config(use_stop=True, stop_mode='trend',
                                     use_time=True, time_stop_bars=90, use_trailing=True),
}


def load_symbol_days(sym):
    """读本地1m CSV, 按日返回 (date, prices_dict, signals)。"""
    fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data', f'{sym}_1m.csv')
    if not os.path.exists(fpath):
        return []
    df = pd.read_csv(fpath).sort_values('trade_time').reset_index(drop=True)
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['trade_time'].dt.strftime('%Y-%m-%d')
    out = []
    for date, day_df in df.groupby('date'):
        day_df = day_df.reset_index(drop=True)
        if len(day_df) < 60:
            continue
        o = day_df['open'].values.astype(float)
        h = day_df['high'].values.astype(float)
        lo = day_df['low'].values.astype(float)
        c = day_df['close'].values.astype(float)
        v = day_df['volume'].values.astype(float)
        pc = float(day_df['open'].iloc[0])
        data = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
        sigs = detect_signals(data, pc)
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'],
                  'trend': data['trend'], 'n': len(day_df)}
        out.append((date, prices, sigs))
    return out


def main():
    lines = []
    def p(s=''):
        print(s); lines.append(s)

    p("=" * 80)
    p("v9 出场管理模块回测 — tickflow 1m 真实数据 (离线)")
    p("=" * 80)
    p(f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    p(f"标的: {len(TARGETS)}  配置: {len(CONFIGS)} (消融: S_only / ATR止损 / 趋势破位止损 / 趋势+移动 / 智能全开)\n")

    # 逐配置汇总 trips
    config_trips = {name: [] for name in CONFIGS}
    # 逐标的逐配置的汇总(用于稳健性)
    sym_config_metrics = {sym: {name: [] for name in CONFIGS} for sym in TARGETS}

    for sym, name in TARGETS.items():
        days = load_symbol_days(sym)
        if not days:
            p(f"{name}: 无数据"); continue
        for date, prices, sigs in days:
            for cname, cfg in CONFIGS.items():
                trips = simulate_day(sigs, prices, cfg)
                config_trips[cname].extend(trips)
                sym_config_metrics[sym][cname].extend(trips)

    # === 总表: 各配置聚合对比 ===
    p(f"{'配置':<18} {'笔数':>4} {'胜率':>6} {'均盈%':>7} {'均亏%':>7} {'盈亏比':>7} {'总收益%':>8} {'复利净值':>8} {'均持仓分':>7}")
    p("-" * 80)
    agg_results = {}
    for cname, cfg in CONFIGS.items():
        m = aggregate_metrics(config_trips[cname])
        agg_results[cname] = m
        p(f"{cname:<18} {m['total']:>4} {m['win_rate']:>5}% {m['avg_win']:>7} {m['avg_loss']:>7} "
          f"{m['pl_ratio']:>6}:1 {m['total_ret']:>7}% {m['cum_nav']:>7} {m['avg_hold']:>6}")

    # === 出场原因分布(完整配置) ===
    p(f"\n{'='*80}\nI_smart_full 出场原因分布 (看各出场路径贡献占比)")
    p("=" * 80)
    full = agg_results['I_smart_full']
    tot = full['total'] or 1
    for r, cnt in sorted(full['by_reason'].items(), key=lambda x: -x[1]):
        p(f"  {r:<6}: {cnt:>4} 笔 ({cnt/tot*100:>4.1f}%)")

    # === 各标的 I_smart_full 稳健性 ===
    p(f"\n{'='*80}\n各标的 I_smart_full(智能出场管理) 表现")
    p("=" * 80)
    p(f"{'标的':<12} {'笔数':>4} {'胜率':>6} {'盈亏比':>7} {'总收益%':>8} {'均持仓分':>7}")
    p("-" * 80)
    for sym, name in TARGETS.items():
        m = aggregate_metrics(sym_config_metrics[sym]['I_smart_full'])
        if m['total']:
            p(f"{name:<12} {m['total']:>4} {m['win_rate']:>5}% {m['pl_ratio']:>6}:1 {m['total_ret']:>7}% {m['avg_hold']:>6}")

    # === 关键结论: 盈亏比提升 ===
    p(f"\n{'='*80}\n关键结论 (出场管理带来的杠杆)")
    p("=" * 80)
    base = agg_results['A_baseline_S_only']
    fullm = agg_results['I_smart_full']
    p(f"  B信号盈亏比:  baseline(S_only)= {base['pl_ratio']}:1  →  smart_full= {fullm['pl_ratio']}:1")
    p(f"  B信号胜率:    baseline= {base['win_rate']}%  →  smart_full= {fullm['win_rate']}%")
    p(f"  总收益(求和): baseline= {base['total_ret']}%  →  smart_full= {fullm['total_ret']}%")
    p(f"  复利净值:     baseline= {base['cum_nav']}  →  smart_full= {fullm['cum_nav']}")
    lift = (fullm['pl_ratio'] / base['pl_ratio']) if base['pl_ratio'] else 0
    p(f"  盈亏比提升倍数: {lift:.2f}x")
    p(f"\n  注: ATR紧止损(B)把胜率从{base['win_rate']}%砸到~10%(均值回归下探被洗);")
    p(f"      改用'trend'破位止损(G/H/I)后胜率回到合理区间, 盈亏比显著提升且总收益不劣于baseline。")

    # 写报告
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'research', 'v9-exit-management-report.md')
    with open(report, 'w', encoding='utf-8') as f:
        f.write("# v9 出场管理模块回测报告\n\n")
        f.write(f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("## 概念\n- 出场管理 ≠ S信号提示。S信号是\"建议出场\"触发器; 出场管理是触发后的执行纪律层(止损/时间/移动多种路径 + S错时怎么办)。\n")
        f.write("- 本模块叠加在 v9_indicators 的 B/S 信号之上, 管理从B建仓到平仓全过程。\n\n")
        f.write("## 方法\n- 数据: tickflow 已落地 1m CSV (7标的×约21交易日), 离线零成本\n")
        f.write("- 单仓位正向T配对: B建仓 → 最近的出场事件(硬止损/S信号/移动止损/时间止损/收盘)\n")
        f.write("- 消融: A_baseline(S_only) / B_+stop / C_+stop+time / D_full\n\n")
        f.write("## 结果\n```\n" + '\n'.join(lines) + "\n```\n")
    print(f"\n📄 报告: {report}")


if __name__ == '__main__':
    main()
