#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_eval_20260723.py — 三项改进措施的准确性影响评估（消融研究）

复用 diag_20260723 的同引擎回放 machinery，仅对 measure ① 做可开关消融：
  ban_downtrend_b: 空仓+trend==-1 禁止开多。
measure ②(移动止损基准价/ EOD强平) 与 ③(卡片展示) 不改信号逻辑，本脚本不回测，
仅在主回复中作结构性分析（见脚本末尾的常量说明）。

输出：每个标的 + 聚合的「before/after」对比（B数、B命中率、回合P&L、胜率）。
"""
import os
import sys

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 复刻生产门控
os.environ['MACD_GATE_MODE'] = 'floor'

from scripts.diag_20260723 import SYMS, fetch, build_data, simulate  # noqa: E402
import numpy as np  # noqa: E402


def fwd_ret(c, i, k):
    j = min(i + k, len(c) - 1)
    return (c[j] / c[i] - 1) * 100


def metrics(sym, data, events, closed):
    c = data['c']
    b_entries = [e for e in events if e[0] == 'B']
    s_entries = [e for e in events if e[0] == 'S']
    # B 命中率（前向>0）
    hr = {}
    for k in (3, 5, 10, 20):
        bw = sum(1 for e in b_entries if fwd_ret(c, e[2], k) > 0)
        sw = sum(1 for e in s_entries if -fwd_ret(c, e[2], k) > 0)
        hr[f'B{k}'] = f'{bw}/{len(b_entries)}' if b_entries else 'NA'
        hr[f'S{k}'] = f'{sw}/{len(s_entries)}' if s_entries else 'NA'
    # 回合 P&L
    win = loss = 0
    tot = 0.0
    for lp in closed:
        r = (lp['exit_price'] - lp['entry']) / lp['entry'] * 100 if lp['side'] == 'long' \
            else (lp['entry'] - lp['exit_price']) / lp['entry'] * 100
        tot += r
        if r > 0:
            win += 1
        else:
            loss += 1
    n = len(closed)
    winrate = f'{win}/{n}' if n else 'NA'
    return dict(b=len(b_entries), s=len(s_entries), hr=hr, rounds=n, win=win,
                loss=loss, winrate=winrate, pnl=round(tot, 2))


def main():
    print('=' * 78)
    print('tpoint 三项改进措施·准确性影响评估（消融）  |  2026-07-23 同引擎回放')
    print('=' * 78)
    agg_before = dict(b=0, s=0, rounds=0, win=0, loss=0, pnl=0.0)
    agg_after = dict(b=0, s=0, rounds=0, win=0, loss=0, pnl=0.0)
    for sym, name in SYMS:
        print(f'\n########## {sym} {name} ##########')
        try:
            df, pc = fetch(sym)
        except Exception as e:
            print(f'  ⚠️ 数据获取失败: {e}')
            continue
        if df is None or len(df) == 0:
            print('  ⚠️ 无 1m 数据')
            continue
        data = build_data(df, pc)
        ev_b, cl_b, _, _ = simulate(sym, name, data, ban_downtrend_b=False)   # 原策略
        ev_a, cl_a, _, _ = simulate(sym, name, data, ban_downtrend_b=True)    # measure ①
        m_b = metrics(sym, data, ev_b, cl_b)
        m_a = metrics(sym, data, ev_a, cl_a)
        print(f'  [原策略]     B={m_b["b"]} S={m_b["s"]} 回合={m_b["rounds"]} '
              f'胜={m_b["winrate"]} P&L={m_b["pnl"]:+.2f}%  B命中率 +3/+5/+10/+20={m_b["hr"]["B3"]}/{m_b["hr"]["B5"]}/{m_b["hr"]["B10"]}/{m_b["hr"]["B20"]}')
        print(f'  [measure①]  B={m_a["b"]} S={m_a["s"]} 回合={m_a["rounds"]} '
              f'胜={m_a["winrate"]} P&L={m_a["pnl"]:+.2f}%  B命中率 +3/+5/+10/+20={m_a["hr"]["B3"]}/{m_a["hr"]["B5"]}/{m_a["hr"]["B10"]}/{m_a["hr"]["B20"]}')
        delta_b = m_a['b'] - m_b['b']
        delta_pnl = m_a['pnl'] - m_b['pnl']
        print(f'  ⇒ Δ: B开仓 {delta_b:+d} 笔, 回合P&L {delta_pnl:+.2f}pt, 胜率 {m_b["winrate"]}→{m_a["winrate"]}')
        # 聚合（简单求和，仅作量级参考；跨标的不可直接相加准确率）
        for d, m in ((agg_before, m_b), (agg_after, m_a)):
            d['b'] += m['b']; d['s'] += m['s']; d['rounds'] += m['rounds']
            d['win'] += m['win']; d['loss'] += m['loss']; d['pnl'] += m['pnl']
    print('\n========== 聚合（跨标的简单求和，仅量级参考） ==========')
    print(f'  原策略:     回合={agg_before["rounds"]} 胜={agg_before["win"]}/{agg_before["loss"]} '
          f'P&L={agg_before["pnl"]:+.2f}%  (B总={agg_before["b"]})')
    print(f'  measure①:   回合={agg_after["rounds"]} 胜={agg_after["win"]}/{agg_after["loss"]} '
          f'P&L={agg_after["pnl"]:+.2f}%  (B总={agg_after["b"]})')
    print('\n注：measure ②(移动止损基准价→入场价 + EOD强平) 与 ③(卡片改显真实止损线)')
    print('    不改变信号触发逻辑 → 对「方向命中率」理论影响为 0；属风险/展示层改进，')
    print('    其价值在回撤控制与用户决策，不在准确率。详见主回复 C 节逐评。')


if __name__ == '__main__':
    main()
