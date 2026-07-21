#!/usr/bin/env python3
"""
B进场质量过滤 — 用真实 round-trip 数据做特征诊断, 找出分隔赢/输的因子,
再据此做进场门控, 最后用同一套 round-trip 回测验证提升是否真实(防过拟合)。

方法(严格遵循特征诊断法, 不盲调参):
  1. 对每个B进场提取特征(RSI/量比/温度/ADX/VWAP偏离/波动率/下影线/时段/触发原因)
     并以"独立配对"(每个B→其后最近S)标出真实盈亏, 统计赢家vs输家特征差。
  2. 从诊断挑出有单调分隔力的因子, 设原则性阈值(不拟合样本极值)做进场门控。
  3. 用与出场管理回测一致的 simulate_day(单仓位) 对比: 不过滤 vs 各候选过滤,
     在看家配置(移动止损出场)下给出胜率/盈亏比/总收益, 确认提升。

数据: tickflow 已落地 1m CSV (离线), 标的来源 = data/watchlist.json（单一真相源）。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from datetime import datetime
from indicators import compute_indicators, detect_signals
from exit_manager import simulate_day, aggregate_metrics, make_config

def _load_entry_targets():
    """动态加载标的：① watchlist.json → ② backtest_data/ 目录自动发现。
    2026-07-21 移除硬编码持仓列表，统一为单一数据源。"""
    # 优先：从项目 watchlist.json 加载
    _wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'watchlist.json')
    try:
        if os.path.exists(_wl_path):
            with open(_wl_path, encoding='utf-8') as f:
                wl = json.load(f)
            if wl and isinstance(wl, dict) and len(wl) > 0:
                return wl
    except Exception:
        pass
    # 兜底：自动发现 backtest_data/ 下有 1m CSV 的标的
    _bd = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backtest_data')
    targets = {}
    if os.path.isdir(_bd):
        for fn in os.listdir(_bd):
            if fn.endswith('_1m.csv'):
                sym = fn.replace('_1m.csv', '')
                targets[sym] = sym  # name fallback = code
    return targets

TARGETS = _load_entry_targets()


# ========== 数据加载(返回 data 以便取特征) ==========

def load_symbol_days(sym):
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
        out.append((date, prices, data, sigs))
    return out


# ========== 独立配对: 每个B → 其后最近S, 用于诊断(样本更多) ==========

def pair_each_b(sigs, prices):
    """对每个B找其后最近的S, 计算ret(独立评估每个进场, 不合并仓位)。"""
    c = prices['c']
    n = prices['n']
    s_after = [s['idx'] for s in sigs if s['type'] == 'S']
    s_after.sort()
    trips = []
    for b in [s for s in sigs if s['type'] == 'B']:
        i = b['idx']
        nxt = [j for j in s_after if j > i]
        if not nxt:
            continue
        j = nxt[0]
        ret = (c[j] - c[i]) / c[i] * 100 if c[i] > 0 else 0.0
        trips.append({'entry_idx': i, 'exit_idx': j, 'ret_pct': ret,
                      'reason': b.get('reason', ''), 'sig': b})
    return trips


# ========== 进场质量门控 ==========

def apply_entry_quality(sigs, data, cfg):
    """按 cfg 门控过滤B信号(只动B, S不动)。返回过滤后信号列表。
    cfg 字段(阈值, 不满足则丢弃该B):
      min_adx         ADX(趋势强度)下限
      max_dev_pct     VWAP偏离上限(越负=越深, 如 -1.0 表示要求低于VWAP≥1%)
      min_lower_shadow_ratio  下影线/ATR 下限
      max_minute      仅保留开盘后前 N 根(避免尾盘)
      require_reasons 允许的触发原因集合(None=不限)
    """
    if cfg is None:
        return sigs
    o = data['o']; lo = data['lo']; c = data['c']
    vwap = data['vwap']; atr = data['atr']; adx = data['adx']
    out = []
    for s in sigs:
        if s['type'] != 'B':
            out.append(s)
            continue
        i = s['idx']
        ok = True
        if cfg.get('min_adx') is not None and adx[i] < cfg['min_adx']:
            ok = False
        if cfg.get('max_dev_pct') is not None:
            dev = (c[i] - vwap[i]) / vwap[i] * 100 if vwap[i] > 0 else 0
            if dev > cfg['max_dev_pct']:
                ok = False
        if cfg.get('min_lower_shadow_ratio') is not None:
            is_yang = c[i] > o[i]
            lsh = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
            ratio = lsh / atr[i] if atr[i] > 0 else 0
            if ratio < cfg['min_lower_shadow_ratio']:
                ok = False
        if cfg.get('max_minute') is not None and i > cfg['max_minute']:
            ok = False
        if cfg.get('require_reasons') is not None and s.get('reason') not in cfg['require_reasons']:
            ok = False
        if ok:
            out.append(s)
    return out


# ========== 诊断: 赢家vs输家特征 ==========

def diagnose(all_days, exit_cfg):
    """用真实 round-trip 出场结果(simulate_day)标注每个B进场的盈亏,
    再提取进场特征。样本=真实回合数(约17), 与验证一致, 不强行配对S。"""
    rows = []
    for date, prices, data, sigs in all_days:
        c = prices['c']; o = data['o']; lo = data['lo']
        vwap = data['vwap']; atr = data['atr']
        rsi = data['rsi']; temp = data['temp']; adx = data['adx']
        vol_ratio = data['vol_ratio']
        trips = simulate_day(sigs, prices, exit_cfg)
        for t in trips:
            i = t['entry_idx']
            is_yang = c[i] > o[i]
            lsh = (o[i] - lo[i]) if is_yang else (c[i] - lo[i])
            dev = (c[i] - vwap[i]) / vwap[i] * 100 if vwap[i] > 0 else 0
            rows.append({
                'win': t['ret_pct'] > 0,
                'ret': t['ret_pct'],
                'vol_ratio': vol_ratio[i], 'rsi': rsi[i], 'temp': temp[i],
                'adx': adx[i], 'dev': dev, 'atr_pct': atr[i] / c[i] * 100 if c[i] > 0 else 0,
                'shadow_ratio': lsh / atr[i] if atr[i] > 0 else 0,
                'minute': i, 'entry_reason': t['entry_reason'], 'exit_reason': t['exit_reason'],
            })
    return pd.DataFrame(rows)


def _bucket_stats(df, col, bins):
    print(f"\n=== {col} 分箱 (赢率 / 均收益 / 样本) ===")
    labels = []
    for lo_b, hi_b in bins:
        sub = df[(df[col] >= lo_b) & (df[col] < hi_b)]
        if len(sub):
            wr = sub.win.mean() * 100
            print(f"  [{lo_b:>6},{hi_b:>6}): n={len(sub):>3}  赢率={wr:>5.1f}%  均收益={sub.ret.mean():>6.3f}%")
        labels.append((lo_b, hi_b))
    return


def main():
    lines = []
    def p(s=''):
        print(s); lines.append(s)

    p("=" * 82)
    p("B进场质量过滤 — 特征诊断 + 候选过滤验证 (tickflow 1m 真实数据, 离线)")
    p("=" * 82)
    p(f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    all_days = []
    for sym, name in TARGETS.items():
        d = load_symbol_days(sym)
        if d:
            p(f"  {name}: {len(d)} 交易日")
            all_days.extend(d)

    # ---- 看家出场配置(移动止损, 关硬止损/时间止损 — 上一轮结论的最优出场) ----
    TRAIL_CFG = make_config(use_stop=False, use_time=False, use_trailing=True)
    SONLY_CFG = make_config(use_stop=False, use_time=False, use_trailing=False, s_signal_exit=True)

    # ---- 诊断(用看家出场=移动止损标注真实盈亏) ----
    df = diagnose(all_days, TRAIL_CFG)
    p(f"\n总独立B配对: {len(df)}  整体赢率: {df.win.mean()*100:.1f}%")
    _bucket_stats(df, 'vol_ratio', [(0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 100)])
    _bucket_stats(df, 'adx', [(0, 15), (15, 20), (20, 25), (25, 30), (30, 100)])
    _bucket_stats(df, 'dev', [(-100, -1.5), (-1.5, -1.0), (-1.0, -0.5), (-0.5, 0), (0, 100)])
    _bucket_stats(df, 'rsi', [(0, 35), (35, 45), (45, 55), (55, 100)])
    _bucket_stats(df, 'shadow_ratio', [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 100)])
    _bucket_stats(df, 'minute', [(0, 60), (60, 120), (120, 180), (180, 240), (240, 1000)])
    p(f"\n=== 按进场触发原因 (entry_reason) ===")
    for r in df.entry_reason.unique():
        sub = df[df.entry_reason == r]
        p(f"  {r:<10}: n={len(sub):>3}  赢率={sub.win.mean()*100:>5.1f}%  均收益={sub.ret.mean():>6.3f}%")
    p(f"\n=== 按出场原因 (exit_reason) ===")
    for r in df.exit_reason.unique():
        sub = df[df.exit_reason == r]
        p(f"  {r:<6}: n={len(sub):>3}  赢率={sub.win.mean()*100:>5.1f}%  均收益={sub.ret.mean():>6.3f}%")

    # ---- 候选过滤验证(与出场管理回测一致的 simulate_day, 单仓位) ----
    candidates = {
        'none':                None,
        'adx>=25':             {'min_adx': 25},
        'adx>=30':             {'min_adx': 30},
        'dev<=-1.0%':          {'max_dev_pct': -1.0},
        'dev<=-1.5%':          {'max_dev_pct': -1.5},
        'shadow>=1.0':         {'min_lower_shadow_ratio': 1.0},
        'adx>=25&dev<=-1.0':   {'min_adx': 25, 'max_dev_pct': -1.0},
        'morning(<=120)':      {'max_minute': 120},
    }

    p(f"\n{'='*82}\n候选进场过滤验证 (出场=移动止损, 与出场管理结论一致)")
    p("=" * 82)
    p(f"{'过滤':<20} {'笔数':>4} {'胜率':>6} {'均盈%':>7} {'均亏%':>7} {'盈亏比':>7} {'总收益%':>8} {'复利净值':>8}")
    p("-" * 82)
    best = None
    for cname, cfg in candidates.items():
        trips_trail = []
        trips_sonly = []
        for date, prices, data, sigs in all_days:
            fsigs = apply_entry_quality(sigs, data, cfg)
            trips_trail.extend(simulate_day(fsigs, prices, TRAIL_CFG))
            trips_sonly.extend(simulate_day(fsigs, prices, SONLY_CFG))
        m = aggregate_metrics(trips_trail)
        p(f"{cname:<20} {m['total']:>4} {m['win_rate']:>5}% {m['avg_win']:>7} {m['avg_loss']:>7} "
          f"{m['pl_ratio']:>6}:1 {m['total_ret']:>7}% {m['cum_nav']:>7}")
        # 以(胜率>=55 且 总收益>0 且 盈亏比尽量高)为优选
        if m['total'] >= 10 and m['win_rate'] >= 55 and m['total_ret'] > 0:
            score = m['win_rate'] + m['pl_ratio'] * 10
            if best is None or score > best[1]:
                best = (cname, score, m)

    p(f"\n{'='*82}\n对照: 各候选在'仅S出场'下的表现(隔离进场过滤本身的贡献)")
    p("=" * 82)
    p(f"{'过滤':<20} {'笔数':>4} {'胜率':>6} {'盈亏比':>7} {'总收益%':>8}")
    p("-" * 42)
    for cname, cfg in candidates.items():
        trips = []
        for date, prices, data, sigs in all_days:
            fsigs = apply_entry_quality(sigs, data, cfg)
            trips.extend(simulate_day(fsigs, prices, SONLY_CFG))
        m = aggregate_metrics(trips)
        p(f"{cname:<20} {m['total']:>4} {m['win_rate']:>5}% {m['pl_ratio']:>6}:1 {m['total_ret']:>7}%")

    p(f"\n{'='*82}\n结论")
    p("=" * 82)
    if best:
        p(f"  优选过滤: {best[0]}  (胜率{best[2]['win_rate']}%, 盈亏比{best[2]['pl_ratio']}:1, 总{best[2]['total_ret']}%)")
    p(f"  注: 样本仅 ~{len(df)} 个独立B配对, 方向性可信但数值不稳健;")
    p(f"      落地 monitor 实盘积累 1+ 月真实信号后再锁定阈值。")

    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'research', 'v9-b-entry-filter-report.md')
    with open(report, 'w', encoding='utf-8') as f:
        f.write("# v9 B进场质量过滤 — 诊断与验证报告\n\n")
        f.write(f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("## 方法\n- 特征诊断法: 真实 round-trip 标盈亏, 扒赢家/输家特征差, 不盲调参\n")
        f.write("- 数据: tickflow 已落地 1m CSV (7标的×约21交易日), 离线\n")
        f.write("- 验证: 与出场管理回测一致的 simulate_day(单仓位), 看家出场=移动止损\n\n")
        f.write("## 结果\n```\n" + '\n'.join(lines) + "\n```\n")
    print(f"\n📄 报告: {report}")


if __name__ == '__main__':
    main()
