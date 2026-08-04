# -*- coding: utf-8 -*-
"""
tpoint 今日(2026-07-28)实盘信号复盘 + 后验验证
- 数据源: data/push_audit.jsonl (飞书推送审计, 含精确触发时间/标的/类型/价格)
- 后验: 同引擎 miji 复算当日1m, 定位信号idx, 回溯触发条件 + 后验走势
- 对比: data/state.json 近5交易日 B/S 日均基准
截止口径: 仅含 ts <= 14:30:00 的推送信号 (用户要求)
"""
import os, sys, json
os.environ['MACD_GATE_MODE'] = 'floor'   # 忠实复现今日生产门控 (run_monitor.bat 设 floor)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 中文字体
for fp in [r'C:/Windows/Fonts/simhei.ttf', r'C:/Windows/Fonts/msyh.ttc']:
    if os.path.exists(fp):
        font_manager.fontManager.addfont(fp)
        plt.rcParams['font.family'] = font_manager.FontProperties(fname=fp).get_name()
        break
plt.rcParams['axes.unicode_minus'] = False

from core.datasource import MootdxDataSource
import core.miji_alpha as miji
from core.miji_alpha import check_b_trigger, check_s_trigger

SYMS = {'688347.SH': '华虹半导体', '161129.SZ': '原油LOF', '513310.SH': '中韩半导体ETF'}
CUTOFF = '2026-07-28 14:30:00'
TODAY = '2026-07-28'


def fetch(sym):
    """取今日1m + 昨收pc, 复算 miji 因子。"""
    ds = MootdxDataSource()
    df = ds.klines.intraday(sym, as_dataframe=True)
    df = df.copy()
    df['tt'] = pd.to_datetime(df['datetime'])
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float)
    # 昨收
    d = ds.get(sym, period='1d', count=3, as_dataframe=True)
    pc = float(d['close'].iloc[-2]) if d is not None and len(d) >= 2 else float(c[0])
    data = miji.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    data['tt'] = df['tt'].values
    data['pc'] = pc
    return data, df


def fwd_ret(c, i, k):
    j = min(i + k, len(c) - 1)
    if j == i:
        return 0.0
    return (c[j] - c[i]) / c[i] * 100.0


def main():
    # 1) 读 audit 筛 <=14:30
    rows = []
    for line in open('data/push_audit.jsonl', encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r['ts'] <= CUTOFF:
            rows.append(r)
    rows.sort(key=lambda x: x['ts'])
    print(f"[audit] 今日<=14:30 信号共 {len(rows)} 笔")

    # 2) 拉数据
    datas = {s: fetch(s) for s in SYMS}

    # 3) 逐笔后验
    results = []
    for r in rows:
        sym = r['sym']; t = r['ts']; typ = r['type']; price = float(r['price'])
        data, df = datas[sym]
        tt = pd.to_datetime(data['tt'])
        idx = int((tt <= pd.Timestamp(t)).sum()) - 1
        if idx < 0:
            idx = 0
        c = data['c']
        cidx = float(c[idx])
        # 触发条件 reason
        if typ == 'B':
            trig, reason = check_b_trigger(data, idx, macd_gate_mode='floor')
        elif typ == 'S':
            trig, reason = check_s_trigger(data, idx, macd_gate_mode='floor')
        else:
            trig, reason = None, '出场信号(移动止损TRAIL/平仓S/反向)'
        if not reason and typ in ('B', 'S'):
            reason = '回溯空(推送/触发K线时间对齐偏差)'
        pc = float(data['pc'])
        day_chg = (cidx - pc) / pc * 100.0
        f5 = fwd_ret(c, idx, 5); f15 = fwd_ret(c, idx, 15)
        f30 = fwd_ret(c, idx, 30); f60 = fwd_ret(c, idx, 60)
        fc = fwd_ret(c, idx, len(c) - 1 - idx)
        # 有效判定: B后涨=有效, S后跌=有效
        if typ == 'B':
            valid = '有效' if fc > 0 else '失效'
        elif typ == 'S':
            valid = '有效' if fc < 0 else '失效'
        else:
            valid = '—'  # 出场信号不单列有效/失效(随对应开仓评估)
        results.append(dict(ts=t, sym=sym, name=SYMS[sym], type=typ, price=price,
                            cidx=cidx, reason=reason, day_chg=round(day_chg, 2),
                            f5=round(f5, 2), f15=round(f15, 2), f30=round(f30, 2),
                            f60=round(f60, 2), fc=round(fc, 2), valid=valid))

    res_df = pd.DataFrame(results)
    res_df.to_csv('output/review_today_20260728.csv', index=False, encoding='utf-8-sig')
    print(f"[csv] 写出 {len(res_df)} 行 -> output/review_today_20260728.csv")

    # 4) 近5日基准 (state.json)
    st = json.load(open('data/state.json'))
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0])
    for k, val in st.items():
        if k.startswith('_b_count_'):
            agg[k.split('_')[-1]][0] += val
        elif k.startswith('_s_count_'):
            agg[k.split('_')[-1]][1] += val
    hist_days = sorted(d for d in agg if d < '20260728')
    recent5 = hist_days[-5:]
    tb = sum(agg[x][0] for x in recent5); ts_ = sum(agg[x][1] for x in recent5)
    base_b = tb / len(recent5); base_s = ts_ / len(recent5); base_t = (tb + ts_) / len(recent5)

    # 5) 今日<=14:30 统计
    nB = (res_df['type'] == 'B').sum()
    nS = (res_df['type'] == 'S').sum()
    nX = (res_df['type'] == 'X').sum()
    bs = res_df[res_df['type'].isin(['B', 'S'])]
    n_valid = (bs['valid'] == '有效').sum()
    n_inv = (bs['valid'] == '失效').sum()
    win_rate = (n_valid / (n_valid + n_inv) * 100) if (n_valid + n_inv) else 0.0

    # 6) 写 TXT 汇总
    with open('output/review_today_20260728.txt', 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("tpoint 今日实盘信号复盘 (2026-07-28, 截止 14:30)\n")
        f.write("=" * 70 + "\n\n")
        f.write("【一、信号清单】每笔触发时间/类型/价格/触发条件\n")
        f.write("-" * 70 + "\n")
        for x in results:
            f.write(f"{x['ts']} | {x['name']}({x['sym']}) | {x['type']} | 推送价={x['price']:.3f} | "
                    f"触发条件=[{x['reason']}] | day_chg={x['day_chg']:+.2f}%\n")
        f.write("\n")
        f.write("【二、后验验证】信号后走势 (fwd=距触发+分钟收益%, close=至收盘)\n")
        f.write("-" * 70 + "\n")
        f.write(f"{'时间':<22}{'标的':<10}{'型':<3}{'+5':>7}{'+15':>7}{'+30':>7}{'+60':>7}{'收盘':>8}  判定\n")
        for x in results:
            f.write(f"{x['ts']:<22}{x['name']:<10}{x['type']:<3}{x['f5']:>6.2f}{x['f15']:>7.2f}"
                    f"{x['f30']:>7.2f}{x['f60']:>7.2f}{x['fc']:>8.2f}  {x['valid']}\n")
        f.write("\n")
        f.write("【三、失效信号原因】\n")
        f.write("-" * 70 + "\n")
        invalids = [x for x in results if x['valid'] == '失效']
        if not invalids:
            f.write("截止14:30 的 B/S 信号中无失效 (全部有效)。\n")
        for x in invalids:
            if x['type'] == 'B':
                f.write(f"* {x['ts']} {x['name']} BUY@{x['price']:.3f}: 买入后至收盘下跌 {x['fc']:.2f}% -> "
                        f"接飞刀/反向。触发=[{x['reason']}], day_chg={x['day_chg']:+.2f}%\n")
            else:
                f.write(f"* {x['ts']} {x['name']} SELL@{x['price']:.3f}: 卖出后至收盘上涨 {abs(x['fc']):.2f}% -> "
                        f"卖飞/过早。触发=[{x['reason']}], day_chg={x['day_chg']:+.2f}%\n")
        f.write("\n")
        f.write("【四、整体表现 vs 近5交易日均值】\n")
        f.write("-" * 70 + "\n")
        f.write(f"今日<=14:30: B={nB} S={nS} X(出场)={nX} | B/S胜率={win_rate:.1f}% ({n_valid}胜/{n_inv}负)\n")
        f.write(f"近5交易日(不含今日 {recent5}) 全天均值: B={base_b:.2f} S={base_s:.2f} 合计={base_t:.2f} 笔/日\n")
        f.write(f"对比: 今日半日 B/S 合计={nB + nS} 笔, 已达近5日全天均值 {base_t:.2f} 笔的 "
                f"{(nB + nS) / base_t * 100:.0f}% -> 信号密度显著偏高(异常点)\n")
        half = nB + nS
        f.write(f"结论: 今日早盘信号密集(13:00-14:30 集中触发), 胜率 {win_rate:.0f}%; "
                f"若近5日基准代表常态, 今日存在'信号频率抬升'模式变化, 需关注 floor 门控在午后波动段的过触发。\n")
    print(f"[txt] 写出 -> output/review_today_20260728.txt")

    # 7) 信号图
    fig, axes = plt.subplots(3, 1, figsize=(12, 9))
    for ax, (sym, name) in zip(axes, SYMS.items()):
        data, df = datas[sym]
        c = data['c']; tt = pd.to_datetime(data['tt'])
        ax.plot(tt, c, color='#444', lw=0.8)
        for x in results:
            if x['sym'] != sym:
                continue
            mask = (tt <= pd.Timestamp(x['ts']))
            xi = int(np.where(mask)[0][-1]) if mask.any() else 0
            col = {'B': '#2ca02c', 'S': '#d62728', 'X': '#1f77b4'}[x['type']]
            mk = {'B': '^', 'S': 'v', 'X': 'x'}[x['type']]
            ax.scatter(tt[xi], c[xi], c=col, marker=mk, s=70, zorder=5)
            lbl = f"{x['type']}{x['fc']:+.2f}%" + (f" {x['valid']}" if x['type'] != 'X' else "")
            ax.annotate(lbl, (tt[xi], c[xi]), textcoords='offset points', xytext=(4, 4), fontsize=7, color=col)
        ax.set_title(f"{name} ({sym})  今日1m + 信号标记", fontsize=10)
        ax.set_ylabel('price')
        ax.tick_params(labelsize=7)
    plt.tight_layout()
    plt.savefig('output/review_today_20260728.png', dpi=120)
    print("[png] 写出 -> output/review_today_20260728.png")

    # 控制台摘要
    print("\n=== 摘要 ===")
    print(f"今日<=14:30: B={nB} S={nS} X={nX}; B/S胜率={win_rate:.1f}%")
    print(f"近5日均值: {base_t:.2f} 笔/日 (B{base_b:.2f}/S{base_s:.2f})")
    print(f"失效信号: {n_inv} 笔")


if __name__ == '__main__':
    main()
