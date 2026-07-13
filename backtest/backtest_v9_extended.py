#!/usr/bin/env python3
"""
v9 回测扩展 P1 — 实盘模拟 + 分市场统计 + 参数敏感性
日K级回测(mootdx分钟历史仅数天,日K约2年500根)
v9算法频率无关,日K上VWAP=多日均价/ATR=日波动/趋势=日EMA+ADX,核心逻辑成立
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import numpy as np
import pandas as pd
from datetime import datetime
from datasource import MootdxDataSource
import v9_indicators as V9
from v9_indicators import compute_indicators, detect_signals

tf = MootdxDataSource()
TARGETS = {
    '300975.SZ': '商络电子', '601869.SH': '长飞光纤', '603938.SH': '三孚股份',
    '300395.SZ': '菲利华', '301526.SZ': '国际复材',
    '300757.SZ': '罗博特科', '688820.SH': '盛合晶微',  # 已清仓但可回测
}
DAILY_COUNT = 500  # 约2年


def get_daily_history(sym, count=DAILY_COUNT):
    """拉日K历史"""
    df = tf.klines.get(sym, period='1d', count=count)
    if df is None or len(df) < 60:
        return None
    return df


def run_v9_on_daily(sym):
    """日K级跑v9, 返回 df/data/sigs"""
    df = get_daily_history(sym)
    if df is None:
        return None
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    pc = float(df['open'].iloc[0])  # 近似前收(温度计用,不影响信号触发)
    data = compute_indicators(o, h, lo, c, v, pc, has_vol=(v is not None))
    sigs = detect_signals(data, pc)
    return {'df': df, 'data': data, 'sigs': sigs, 'sym': sym}


def simulate_trades(result):
    """配对B/S信号模拟交易. B次日开盘买入, 下一个S次日开盘卖出
    返回 trades 列表(每笔 entry/exit/ret/hold)"""
    sigs = result['sigs']
    df = result['df']
    o = df['open'].values
    c = df['close'].values
    dates = df['trade_date'].values
    n = len(df)
    trades = []
    i = 0
    while i < len(sigs):
        if sigs[i]['type'] == 'B':
            entry_idx = min(sigs[i]['idx'] + 1, n - 1)
            entry_price = o[entry_idx]
            # 找下一个S作为出场
            exit_idx = n - 1
            exit_price = c[-1]
            for j in range(i + 1, len(sigs)):
                if sigs[j]['type'] == 'S':
                    exit_idx = min(sigs[j]['idx'] + 1, n - 1)
                    exit_price = o[exit_idx]
                    break
            ret = (exit_price - entry_price) / entry_price if entry_price > 0 else 0
            hold = exit_idx - entry_idx
            trades.append({
                'entry_date': str(dates[entry_idx])[:10],
                'exit_date': str(dates[exit_idx])[:10],
                'entry': round(entry_price, 2), 'exit': round(exit_price, 2),
                'ret': round(ret * 100, 2), 'hold_days': hold,
                'trend_at_entry': sigs[i]['trend'],
            })
            i = j + 1 if 'j' in dir() and sigs[j]['type'] == 'S' else i + 1
        else:
            i += 1
    return trades


def calc_portfolio_metrics(trades):
    """算组合指标: 累计收益/胜率/盈亏比/夏普/最大回撤"""
    if not trades:
        return {'total': 0, 'win_rate': 0, 'avg_ret': 0, 'pf': 0, 'cum_nav': 1.0, 'sharpe': 0, 'max_dd': 0}
    rets = np.array([t['ret'] / 100 for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    # 等权复利净值
    cum = np.cumprod(1 + rets)
    cum_nav = cum[-1]
    # 最大回撤
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    max_dd = dd.min()
    # 夏普(按平均持仓天数年化)
    avg_hold = np.mean([t['hold_days'] for t in trades]) or 1
    sharpe = (rets.mean() / rets.std() * np.sqrt(252 / avg_hold)) if rets.std() > 0 else 0
    return {
        'total': len(trades),
        'win_rate': round(len(wins) / len(rets) * 100, 1),
        'avg_ret': round(rets.mean() * 100, 2),
        'pf': round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else float('inf'),
        'cum_nav': round(cum_nav, 3),
        'cum_ret': round((cum_nav - 1) * 100, 1),
        'sharpe': round(sharpe, 2),
        'max_dd': round(max_dd * 100, 1),
        'avg_hold': round(avg_hold, 1),
    }


def segment_market_stats(result):
    """分市场统计: 按trend(上升/下降/震荡)统计信号分布"""
    data = result['data']
    sigs = result['sigs']
    tr = data['trend']
    n = len(tr)
    up_n = int((tr == 1).sum()); dn_n = int((tr == -1).sum()); flat_n = int((tr == 0).sum())
    # 信号按触发时trend分布
    b_up = sum(1 for s in sigs if s['type'] == 'B' and s['trend'] == 1)
    b_flat = sum(1 for s in sigs if s['type'] == 'B' and s['trend'] == 0)
    s_dn = sum(1 for s in sigs if s['type'] == 'S' and s['trend'] == -1)
    s_flat = sum(1 for s in sigs if s['type'] == 'S' and s['trend'] == 0)
    return {
        'bars': {'up': up_n, 'down': dn_n, 'flat': flat_n},
        'signals': {'B_up': b_up, 'B_flat': b_flat, 'S_down': s_dn, 'S_flat': s_flat},
    }


def param_sweep(sym):
    """参数敏感性: K1/K2/ADX_THRESHOLD 各3档扫描"""
    base = {'K1': V9.K1, 'K2': V9.K2, 'ADX_THRESHOLD': V9.ADX_THRESHOLD}
    results = []
    df = get_daily_history(sym)
    if df is None:
        return results
    o = df['open'].values.astype(float); h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    pc = float(df['open'].iloc[0])
    params = [
        ('K1', 0.7), ('K1', 1.0), ('K1', 1.3),
        ('K2', 1.5), ('K2', 2.0), ('K2', 2.5),
        ('ADX', 15), ('ADX', 20), ('ADX', 25),
    ]
    for pname, val in params:
        # 设参数
        V9.K1 = base['K1']; V9.K2 = base['K2']; V9.ADX_THRESHOLD = base['ADX_THRESHOLD']
        if pname == 'K1': V9.K1 = val
        elif pname == 'K2': V9.K2 = val
        elif pname == 'ADX': V9.ADX_THRESHOLD = val
        data = compute_indicators(o, h, lo, c, v, pc, has_vol=(v is not None))
        sigs = detect_signals(data, pc)
        result = {'df': df, 'data': data, 'sigs': sigs, 'sym': sym}
        trades = simulate_trades(result)
        m = calc_portfolio_metrics(trades)
        results.append({'param': f'{pname}={val}', 'signals': len(sigs), 'trades': m['total'],
                        'win_rate': m['win_rate'], 'cum_ret': m['cum_ret'], 'sharpe': m['sharpe']})
    # 恢复
    V9.K1 = base['K1']; V9.K2 = base['K2']; V9.ADX_THRESHOLD = base['ADX_THRESHOLD']
    return results


def main():
    lines = []
    def p(s=''):
        print(s); lines.append(s)

    p("=" * 70)
    p("v9 回测扩展 P1 — 实盘模拟 + 分市场 + 参数敏感性 (日K级, 2年历史)")
    p("=" * 70)

    # === 逐标的回测 ===
    all_trades = []
    p(f"\n{'标的':<12} {'信号':>4} {'交易':>4} {'胜率':>6} {'均收%':>6} {'盈亏比':>6} {'累计%':>7} {'夏普':>5} {'回撤%':>6} {'持仓天':>5}")
    p("-" * 70)
    for sym, name in TARGETS.items():
        try:
            result = run_v9_on_daily(sym)
            if result is None:
                p(f"{name:<12} 数据不足")
                continue
            trades = simulate_trades(result)
            m = calc_portfolio_metrics(trades)
            all_trades.extend([(t, name) for t in trades])
            p(f"{name:<12} {len(result['sigs']):>4} {m['total']:>4} {m['win_rate']:>5}% {m['avg_ret']:>6} {m['pf']:>6} {m['cum_ret']:>6}% {m['sharpe']:>5} {m['max_dd']:>5}% {m['avg_hold']:>5}")
        except Exception as e:
            p(f"{name:<12} 异常: {e}")

    # === 汇总组合 ===
    p(f"\n{'='*70}\n汇总组合 ({len(all_trades)}笔交易)")
    p("=" * 70)
    if all_trades:
        all_rets = [t[0]['ret'] for t in all_trades]
        wins = [r for r in all_rets if r > 0]
        losses = [r for r in all_rets if r <= 0]
        cum = np.prod([1 + r / 100 for r in all_rets])
        p(f"总交易笔数: {len(all_rets)}")
        p(f"胜率: {len(wins)}/{len(all_rets)} = {len(wins)/len(all_rets)*100:.1f}%")
        p(f"平均收益: {np.mean(all_rets):.2f}%")
        p(f"盈亏比: {np.mean(wins)/abs(np.mean(losses)):.2f}:1" if losses else "盈亏比: N/A(无亏损)")
        p(f"累计复利: {(cum-1)*100:.1f}%")
        p(f"盈利交易平均: {np.mean(wins):.2f}%" if wins else "")
        p(f"亏损交易平均: {np.mean(losses):.2f}%" if losses else "")

    # === 分市场统计 (取一个代表标的) ===
    p(f"\n{'='*70}\n分市场环境统计 (商络电子)")
    p("=" * 70)
    try:
        result = run_v9_on_daily('300975.SZ')
        seg = segment_market_stats(result)
        p(f"趋势分布: 上升{seg['bars']['up']}天 下降{seg['bars']['down']}天 震荡{seg['bars']['flat']}天")
        p(f"信号分布: B上升{seg['signals']['B_up']} B震荡{seg['signals']['B_flat']} S下降{seg['signals']['S_down']} S震荡{seg['signals']['S_flat']}")
        p(f"解读: v9设计B在上升/震荡发, S在下降/震荡发 → 实际分布是否符合设计")
    except Exception as e:
        p(f"分市场统计异常: {e}")

    # === 参数敏感性 (商络电子) ===
    p(f"\n{'='*70}\n参数敏感性 (商络电子)")
    p("=" * 70)
    p(f"{'参数':<10} {'信号':>4} {'交易':>4} {'胜率':>6} {'累计%':>7} {'夏普':>5}")
    p("-" * 40)
    try:
        sweep = param_sweep('300975.SZ')
        for r in sweep:
            p(f"{r['param']:<10} {r['signals']:>4} {r['trades']:>4} {r['win_rate']:>5}% {r['cum_ret']:>6}% {r['sharpe']:>5}")
    except Exception as e:
        p(f"参数敏感性异常: {e}")

    p(f"\n{'='*70}")
    p("注: 日K级回测, v9分钟级算法降频至日级(VWAP=多日均价/ATR=日波动)")
    p("   实盘分钟级表现需部署monitor_v9后实盘跟踪验证")
    p("=" * 70)

    # 写报告
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'research', 'v9-backtest-p1-report.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v9 回测扩展 P1 报告\n\n")
        f.write(f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("## 方法\n- 日K级回测(mootdx分钟历史仅数天,日K约2年500根)\n- v9算法频率无关,日K上VWAP=多日均价/ATR=日波动\n- B信号次日开盘买入,下一个S次日开盘卖出\n\n")
        f.write("## 结果\n```\n" + '\n'.join(lines) + "\n```\n")
    print(f"\n📄 报告: {report_path}")


if __name__ == '__main__':
    main()
