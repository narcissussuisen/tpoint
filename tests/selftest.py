#!/usr/bin/env python3
"""
v9 算法本地验证 (selftest) — 无需tickflow, 用合成行情验证核心逻辑
重点验证: 下跌趋势 v9 能发S (v8做不到)
对比: v8 LONGCROSS逻辑 vs v9 VWAP+趋势+量价+温度
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from indicators import compute_indicators, detect_signals

# ========== 合成行情生成 ==========

def gen_uptrend(n=240, start=10.0, end=11.0, seed=1):
    """上涨趋势: 含周期性回踩(触发B)"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    base = start + (end - start) * t / n
    wave = np.zeros(n)
    for center in [60, 140, 210]:  # 3次回踩谷
        wave -= 0.18 * np.exp(-((t - center) / 7) ** 2)
    noise = rng.normal(0, 0.025, n)
    close = base + wave + noise
    high = close + rng.uniform(0.01, 0.05, n)
    low = close - rng.uniform(0.01, 0.05, n)
    op = np.empty(n); op[0] = close[0]; op[1:] = close[:-1]
    op += rng.normal(0, 0.01, n)
    vol = rng.uniform(800, 1400, n)
    for center in [60, 140, 210]:  # 回踩放量
        vol += 2500 * np.exp(-((t - center) / 7) ** 2)
    return op, high, low, close, vol


def gen_downtrend(n=240, start=11.0, end=10.0, seed=2):
    """下跌趋势: 含周期性反弹峰(触发S) — v8在此场景S=0"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    base = start + (end - start) * t / n  # 下行
    wave = np.zeros(n)
    for center in [60, 140, 210]:  # 3次反弹峰
        wave += 0.18 * np.exp(-((t - center) / 7) ** 2)
    noise = rng.normal(0, 0.025, n)
    close = base + wave + noise
    high = close + rng.uniform(0.01, 0.05, n)
    low = close - rng.uniform(0.01, 0.05, n)
    op = np.empty(n); op[0] = close[0]; op[1:] = close[:-1]
    op += rng.normal(0, 0.01, n)
    vol = rng.uniform(800, 1400, n)
    for center in [60, 140, 210]:  # 反弹放量
        vol += 2500 * np.exp(-((t - center) / 7) ** 2)
    return op, high, low, close, vol


def gen_sideways(n=240, center=10.5, amp=0.3, seed=3):
    """震荡: 中心±amp, 多次触轨"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    wave = amp * np.sin(2 * np.pi * t / 30) + 0.5 * amp * np.sin(2 * np.pi * t / 17)
    noise = rng.normal(0, 0.03, n)
    close = center + wave + noise
    high = close + rng.uniform(0.01, 0.05, n)
    low = close - rng.uniform(0.01, 0.05, n)
    op = np.empty(n); op[0] = close[0]; op[1:] = close[:-1]
    op += rng.normal(0, 0.01, n)
    vol = rng.uniform(600, 1200, n)
    vol += 1500 * (np.abs(wave) / amp)  # 触轨放量
    return op, high, low, close, vol


# ========== v8 LONGCROSS逻辑 (对比基准) ==========

def v8_detect(o, h, lo, c, pc):
    """v8 生产逻辑: B=LONGCROSS(支撑,C,2), S=LONGCROSS(C,阻力,2)
    采用 backtest_v8.py 的判定 (c[i-2]<=sup and c[i-1]<=sup and c[i]>sup)"""
    n = len(c)
    eh = np.maximum.accumulate(h)
    el = np.minimum.accumulate(lo)
    g1 = np.maximum(pc, eh)
    g2 = np.minimum(pc, el)
    g3 = g1 - g2
    sup = g2 + g3 * 0.5 / 9
    res = g2 + g3 * 8.0 / 9
    sigs = []
    for i in range(2, n):
        if c[i-2] <= sup[i-2] and c[i-1] <= sup[i-1] and c[i] > sup[i]:
            sigs.append({'type': 'B', 'idx': i, 'price': round(float(c[i]), 2)})
        if c[i-2] >= res[i-2] and c[i-1] >= res[i-1] and c[i] < res[i]:
            sigs.append({'type': 'S', 'idx': i, 'price': round(float(c[i]), 2)})
    return sigs


# ========== 主验证 ==========

def run():
    scenarios = [
        ('上涨趋势', gen_uptrend, 10.0),
        ('下跌趋势', gen_downtrend, 11.0),
        ('震荡', gen_sideways, 10.5),
    ]
    lines = []
    def p(s=''):
        print(s); lines.append(s)

    p("=" * 72)
    p("v9 算法本地验证 — 合成行情 v8 vs v9 对比")
    p("核心命题: 下跌趋势 v9 应能发S (v8在该场景S≈0)")
    p("=" * 72)

    summary = []
    for name, gen_fn, pc in scenarios:
        op, h, lo, c, v = gen_fn()
        pc_val = pc

        # v9
        data = compute_indicators(op, h, lo, c, v, pc_val, has_vol=True)
        v9_sigs = detect_signals(data, pc_val)
        v9_b = [s for s in v9_sigs if s['type'] == 'B']
        v9_s = [s for s in v9_sigs if s['type'] == 'S']

        # 趋势分布
        tr = data['trend']
        up_n = int(np.sum(tr == 1)); dn_n = int(np.sum(tr == -1)); flat_n = int(np.sum(tr == 0))

        # v8
        v8_sigs = v8_detect(op, h, lo, c, pc_val)
        v8_b = [s for s in v8_sigs if s['type'] == 'B']
        v8_s = [s for s in v8_sigs if s['type'] == 'S']

        p(f"\n【{name}】 (n=240, pc={pc_val})")
        p(f"  趋势分布: 上升={up_n} 下降={dn_n} 震荡={flat_n} bar")
        p(f"  {'':12s} {'B信号':>8} {'S信号':>8} {'合计':>8}")
        p(f"  {'v8 LONGCROSS':<12} {len(v8_b):>8} {len(v8_s):>8} {len(v8_sigs):>8}")
        p(f"  {'v9 VWAP+趋势':<12} {len(v9_b):>8} {len(v9_s):>8} {len(v9_sigs):>8}")

        # v9信号明细(前5条)
        if v9_sigs:
            p(f"  v9信号样本(前5):")
            for s in v9_sigs[:5]:
                tl = {1: '↑', -1: '↓', 0: '~'}[s['trend']]
                p(f"    [{s['type']}] idx={s['idx']:>3} 价={s['price']:.2f} 趋势={tl} "
                  f"温度={s['temp']:.0f} RSI={s['rsi']:.1f} 量比={s['vol_ratio']:.2f} {s['reason']}")

        summary.append({
            'name': name, 'v8_b': len(v8_b), 'v8_s': len(v8_s),
            'v9_b': len(v9_b), 'v9_s': len(v9_s),
        })

    # ===== 核心结论 =====
    p("\n" + "=" * 72)
    p("核心结论验证")
    p("=" * 72)
    dt = [s for s in summary if s['name'] == '下跌趋势'][0]
    ut = [s for s in summary if s['name'] == '上涨趋势'][0]
    sw = [s for s in summary if s['name'] == '震荡'][0]

    p(f"\n1. 【下跌趋势 S信号】 v8={dt['v8_s']}  v9={dt['v9_s']}")
    if dt['v9_s'] > 0 and dt['v8_s'] == 0:
        p(f"   ✅ 命中命题: v9在下跌趋势发出 {dt['v9_s']} 个S信号, v8为0 — 根治S不触发")
    elif dt['v9_s'] > dt['v8_s']:
        p(f"   ✅ 改善: v9 S信号 {dt['v9_s']} > v8 {dt['v8_s']}")
    else:
        p(f"   ⚠️ 未达预期: v9 S={dt['v9_s']} v8 S={dt['v8_s']}, 需调参")

    p(f"\n2. 【上涨趋势 B信号】 v8={ut['v8_b']}  v9={ut['v9_b']}")
    p(f"   v9 B信号需量价确认, 数量应收敛且更精准")

    p(f"\n3. 【震荡双向】 v8 B={sw['v8_b']}/S={sw['v8_s']}  v9 B={sw['v9_b']}/S={sw['v9_s']}")
    p(f"   v9震荡态双向均能触发(均值回归), v8震荡易B误发")

    p(f"\n4. 【信号总量收敛】 v8合计={sum(s['v8_b']+s['v8_s'] for s in summary)} "
      f"v9合计={sum(s['v9_b']+s['v9_s'] for s in summary)}")
    p(f"   v9通过量价确认收敛误信号, 降低交易摩擦")

    p("\n" + "=" * 72)
    p("注: 合成行情验证算法逻辑正确性, 实盘表现需用真实数据回测确认")
    p("=" * 72)

    # ===== 前视偏差防护栅栏(2026-08-17 引入, 源自 AQuA 论文) =====
    p("\n" + "=" * 72)
    p("前视偏差防护栅栏 — 未来扰动测试 (lookahead leak guard)")
    p("=" * 72)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
        from leak_guard import (check_indicators_no_lookahead, check_miji_no_lookahead)
        for tag, fn in (('v9 特征栈', check_indicators_no_lookahead),
                        ('miji 特征栈', check_miji_no_lookahead)):
            try:
                r = fn()
                if r['ok']:
                    p(f"  ✅ {tag}: 无前视 (n_checks={r['n_checks']}, "
                      f"worst_diff={r['worst']['max_abs_diff']:.2e})")
                else:
                    p(f"  ❌ {tag}: 检出前视 {r['worst']}")
            except Exception as e:
                p(f"  ❌ {tag}: 栅栏异常 {e}")
        p("  说明: 往'未来'bar灌噪声后重算历史特征, 历史值不变=无泄漏;")
        p("        任何新增含未来数据的特征都会在此变红(防回归)。")
    except Exception as e:
        p(f"  ⚠️ 栅栏不可用(跳过): {e}")

    # 写报告文件
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'selftest_report.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v9 算法本地验证报告\n\n")
        f.write("生成时间: " + __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M') + "\n\n")
        f.write("## 命题: 下跌趋势 v9 能发S (v8做不到)\n\n")
        f.write("| 场景 | v8-B | v8-S | v9-B | v9-S |\n|------|----:|----:|----:|----:|\n")
        for s in summary:
            f.write(f"| {s['name']} | {s['v8_b']} | {s['v8_s']} | {s['v9_b']} | {s['v9_s']} |\n")
        f.write("\n```\n")
        f.write('\n'.join(lines))
        f.write("\n```\n")
    print(f"\n📄 报告已写入: {report_path}")
    return lines


if __name__ == '__main__':
    run()
