"""口径追查: 四条配对管道对比 688347 全量数据"""
import sys, os
sys.path.insert(0, 'backtest/keyfactor')
sys.path.insert(0, 'core')
import pandas as pd, numpy as np
import miji_engine as ME
from _paths import KEYFACTOR_1M_DIR

DATA = KEYFACTOR_1M_DIR
COST = 0.02
fn = os.path.join(DATA, '688347.SH_1m.csv')
df = pd.read_csv(fn)
df = df.sort_values('trade_time').reset_index(drop=True)

results = {'A': [], 'B': [], 'C': [], 'D': []}
for date, day in df.groupby('trade_date'):
    if len(day) < 5:
        continue
    o = day['open'].values.astype(float); h = day['high'].values.astype(float)
    lo = day['low'].values.astype(float); c = day['close'].values.astype(float)
    v = day['volume'].values.astype(float) if 'volume' in day.columns else None
    pc = float(c[0]); n = len(c)
    try:
        data = ME.compute_miji_indicators(o, h, lo, c, v, pc)
    except Exception:
        continue
    sigs = ME.detect_miji_signals(data, pc, macd_gate_mode='floor', enable=(True, True, True))
    ns = len(sigs)
    if ns == 0:
        continue

    # A: compare口径 (次根K+相邻非重叠+双向)
    netA = 0.0
    k = 0
    while k + 1 < len(sigs):
        a, b = sigs[k], sigs[k + 1]
        ai, bi = a['idx'], b['idx']
        if ai + 1 >= n or bi + 1 >= n:
            k += 1
            continue
        ef = float(c[ai + 1]); xf = float(c[bi + 1])
        if a['type'] == 'B' and b['type'] == 'S':
            netA += (xf - ef) / ef * 100 - 2 * COST
        elif a['type'] == 'S' and b['type'] == 'B':
            netA += (ef - xf) / ef * 100 - 2 * COST
        k += 2
    results['A'].append((ns, netA))

    # B: scan口径 (信号价+位置B->S+EOD)
    netB = 0.0; pos = None
    for s in sorted(sigs, key=lambda x: x['idx']):
        if s['type'] == 'B' and pos is None:
            pos = {'p': s.get('entry_price', s['price'])}
        elif s['type'] == 'S' and pos is not None:
            netB += (s['price'] - pos['p']) / pos['p'] * 100 - 2 * COST
            pos = None
    if pos is not None:
        netB += (float(c[-1]) - pos['p']) / pos['p'] * 100 - 2 * COST
    results['B'].append((ns, netB))

    # C: 次根K成交 + B->S覆盖 (修复版配对)
    netC = 0.0; pos = None
    for s in sigs:
        if s['type'] == 'B' and pos is None:
            ai = s['idx']
            if ai + 1 < n:
                pos = {'p': float(c[ai + 1])}
        elif s['type'] == 'S' and pos is not None:
            bi = s['idx']
            if bi + 1 < n:
                netC += (float(c[bi + 1]) - pos['p']) / pos['p'] * 100 - 2 * COST
                pos = None
    results['C'].append((ns, netC))

    # D: 信号价成交 + 相邻非重叠 (仅改配对)
    netD = 0.0
    k = 0
    while k + 1 < len(sigs):
        a, b = sigs[k], sigs[k + 1]
        ai, bi = a['idx'], b['idx']
        ep = a.get('entry_price', a['price']); xp = b['price']
        if a['type'] == 'B' and b['type'] == 'S':
            netD += (xp - ep) / ep * 100 - 2 * COST
        elif a['type'] == 'S' and b['type'] == 'B':
            netD += (ep - xp) / ep * 100 - 2 * COST
        k += 2
    results['D'].append((ns, netD))

fmt = "{:<8} {:<12} {:<12} {:<12} {:>8}"
desc = {
    'A': 'compare原始 (次根K+非重叠+双向) = 07-20沙箱基准',
    'B': 'scan原始 (信号价+位置锁定+EOD) = 今晚扫描(配对BUG)',
    'C': '修复版 (次根K+B->S覆盖) = 修正配对',
    'D': '仅改配对 (信号价+相邻非重叠) = 配对修正, 成交价未改',
}
print(fmt.format("管道", "总信号", "总净T%", "per_sig", "配对"))
print("-" * 55)
for name in ['A', 'B', 'C', 'D']:
    sigs_total = sum(r[0] for r in results[name])
    net_total = sum(r[1] for r in results[name])
    per_sig = net_total / sigs_total if sigs_total else 0
    n_pairs = sum(1 for r in results[name] if abs(r[1]) > 0.0001)
    print(fmt.format(name, str(sigs_total), f"{net_total:.2f}%",
                     f"{per_sig:.4f}%", str(n_pairs)))

print("\n=== 各管道说明 ===")
for name in ['A', 'B', 'C', 'D']:
    print(f" {name}: {desc[name]}")
a_per = sum(r[1] for r in results['A']) / sum(r[0] for r in results['A'])
b_per = sum(r[1] for r in results['B']) / sum(r[0] for r in results['B'])
print(f"\n偏差: 管道B per_sig = {b_per:.4f}% vs 管道A = {a_per:.4f}%")
print(f"差值 = {b_per - a_per:.4f}% ({'低估' if b_per < a_per else '高估'})")
print(f"配对策略错误导致 per_sig 偏离 {(b_per - a_per) / a_per * 100:.0f}% 基准")
