# -*- coding: utf-8 -*-
# ===================== SUPERSEDED =====================
# 本脚本用「向前固定 15 根持有」P&L 评估信号质量 -> 含未来信息, 后视镜偏差。
# 干净同口径对比见 d_candidate_backtest.py (forward_backtest)。本文件仅作历史参照保留。
# ======================================================
"""7/24 同标的对比: miji 三因子(resonance) vs WhatIf-D 四条件组合。
输出 output/floor_diagnosis_20260724/compare_3f_D.json
"""
import sys, os, json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 中文字体
for fp in ['C:/Windows/Fonts/simhei.ttf', 'C:/Windows/Fonts/msyh.ttc']:
    if os.path.exists(fp):
        font_manager.FontProperties(fname=fp)
        plt.rcParams['font.family'] = 'SimHei'
        plt.rcParams['axes.unicode_minus'] = False
        break

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'backtest', 'keyfactor'))
import miji_engine as ME

DAY = '2026-07-24'
DATA_DIR = r'F:/keyfactor_data/1m'
OUT = os.path.join(ROOT, 'output', 'floor_diagnosis_20260724')
os.makedirs(OUT, exist_ok=True)

# WhatIf-D 参数(沿用已验证)
K = 2.5
WL = 30
FWD = 15  # 向前校验 bar 数


def load_csv(code):
    f = os.path.join(DATA_DIR, f'{code}_1m.csv')
    df = pd.read_csv(f, encoding='utf-8-sig')
    df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
    return df


def forward_pnl(c, idxs, side):
    out = []
    for i in idxs:
        if i + FWD >= len(c):
            continue
        if side == 'B':
            pct = (c[i + FWD] / c[i] - 1) * 100
        else:
            pct = (c[i] / c[i + FWD] - 1) * 100
        out.append((i, round(c[i], 4), round(pct, 2)))
    return out


def run_resonance(code):
    df = load_csv(code)
    d = df[df['trade_date'] == DAY].reset_index(drop=True)
    o = d['open'].values.astype(float); h = d['high'].astype(float)
    lo = d['low'].astype(float); c = d['close'].astype(float)
    v = d['volume'].astype(float); pc = float(d['close'].iloc[0])
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    sigs = ME.detect_miji_signals(data, pc, start_idx=2,
                                  min_resonance=ME.RESONANCE_THRESHOLD,
                                  macd_gate_mode='resonance', b_trend_filter=False)
    b = [s for s in sigs if s['type'] == 'B']
    s = [s for s in sigs if s['type'] == 'S']
    b_idx = [s['idx'] for s in b]; s_idx = [s['idx'] for s in s]
    tt = d['trade_time'].values
    # 因子分分布
    from collections import Counter
    b_scores = Counter(s['resonance_score'] for s in b)
    s_scores = Counter(s['resonance_score'] for s in s)
    return {
        'buy': len(b), 'sell': len(s),
        'buy_idx': b_idx, 'sell_idx': s_idx,
        'buy_times': [tt[i] for i in b_idx], 'sell_times': [tt[i] for i in s_idx],
        'buy_pnl': forward_pnl(c, b_idx, 'B'),
        'sell_pnl': forward_pnl(c, s_idx, 'S'),
        'b_scores': dict(b_scores), 's_scores': dict(s_scores),
    }


def is_swing_low(lo, i, w):
    if i < 2:
        return False
    win = lo[max(0, i - w):i]
    return len(win) > 0 and float(lo[i]) < float(win.min())


def is_swing_high(h, i, w):
    if i < 2:
        return False
    win = h[max(0, i - w):i]
    return len(win) > 0 and float(h[i]) > float(win.max())


def reversal_confirmed(c, i, side, nbar=5):
    if i + 1 >= len(c):
        return False
    end = min(i + nbar, len(c) - 1)
    fav = tot = 0
    for j in range(i + 1, end + 1):
        tot += 1
        if side == 'B' and c[j] > c[i]:
            fav += 1
        if side == 'S' and c[j] < c[i]:
            fav += 1
    return tot > 0 and fav >= (tot + 1) // 2


def run_D(code):
    df = load_csv(code)
    d = df[df['trade_date'] == DAY].reset_index(drop=True)
    o = d['open'].values.astype(float); h = d['high'].astype(float)
    lo = d['low'].astype(float); c = d['close'].astype(float)
    v = d['volume'].astype(float); pc = float(d['close'].iloc[0])
    data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    vwap = data['vwap']; atr = data['atr']; n = data['n']
    ema_f = pd.Series(c).ewm(span=20, adjust=False).mean().values
    ema_s = pd.Series(c).ewm(span=60, adjust=False).mean().values
    tt = d['trade_time'].values
    causal_b, causal_s, conf_b, conf_s = [], [], [], []
    for i in range(WL, n):
        if atr[i] <= 0:
            continue
        atr_pct = atr[i] / vwap[i] * 100.0
        g_dev = (c[i] - vwap[i]) / vwap[i] * 100.0
        thr = K * atr_pct
        uptrend = ema_f[i] >= ema_s[i]
        is_b = is_swing_low(lo, i, WL) and g_dev <= -thr and uptrend
        is_s = is_swing_high(h, i, WL) and g_dev >= thr and uptrend
        if is_b:
            causal_b.append(i)
            if reversal_confirmed(c, i, 'B'):
                conf_b.append(i)
        if is_s:
            causal_s.append(i)
            if reversal_confirmed(c, i, 'S'):
                conf_s.append(i)
    return {
        'D_causal_buy': len(causal_b), 'D_causal_sell': len(causal_s),
        'D_confirmed_buy': len(conf_b), 'D_confirmed_sell': len(conf_s),
        'D_causal_buy_idx': causal_b, 'D_causal_sell_idx': causal_s,
        'D_confirmed_buy_idx': conf_b, 'D_confirmed_sell_idx': conf_s,
        'D_causal_buy_times': [tt[i] for i in causal_b], 'D_causal_sell_times': [tt[i] for i in causal_s],
        'D_confirmed_buy_times': [tt[i] for i in conf_b], 'D_confirmed_sell_times': [tt[i] for i in conf_s],
        'D_confirmed_buy_pnl': forward_pnl(c, conf_b, 'B'),
        'D_confirmed_sell_pnl': forward_pnl(c, conf_s, 'S'),
        'D_causal_buy_pnl': forward_pnl(c, causal_b, 'B'),
        'D_causal_sell_pnl': forward_pnl(c, causal_s, 'S'),
    }


SYMS = [('161129.SZ', '原油LOF'), ('513310.SH', '中韩半导体ETF')]
result = {}
for code, name in SYMS:
    r3 = run_resonance(code)
    rD = run_D(code)
    avg = lambda L: round(float(np.mean([x[2] for x in L])), 3) if L else None
    result[code] = {
        'name': name,
        'resonance': {
            'buy': r3['buy'], 'sell': r3['sell'],
            'buy_pnl_avg%': avg(r3['buy_pnl']),
            'sell_pnl_avg%': avg(r3['sell_pnl']),
            'b_scores': r3['b_scores'], 's_scores': r3['s_scores'],
        },
        'D': {
            'causal_buy': rD['D_causal_buy'], 'causal_sell': rD['D_causal_sell'],
            'confirmed_buy': rD['D_confirmed_buy'], 'confirmed_sell': rD['D_confirmed_sell'],
            'confirmed_buy_pnl_avg%': avg(rD['D_confirmed_buy_pnl']),
            'confirmed_sell_pnl_avg%': avg(rD['D_confirmed_sell_pnl']),
            'causal_buy_pnl_avg%': avg(rD['D_causal_buy_pnl']),
            'causal_sell_pnl_avg%': avg(rD['D_causal_sell_pnl']),
        },
    }
    print(f"\n===== {code} {name} =====")
    print(f"三因子 resonance: 买{r3['buy']} 卖{r3['sell']} | 买P&L均{result[code]['resonance']['buy_pnl_avg%']} 卖P&L均{result[code]['resonance']['sell_pnl_avg%']}")
    print(f"   B因子分分布={r3['b_scores']}  S因子分分布={r3['s_scores']}")
    print(f"WhatIf-D 因果(3条件): 买{rD['D_causal_buy']} 卖{rD['D_causal_sell']}")
    print(f"WhatIf-D 确认(4条件): 买{rD['D_confirmed_buy']} 卖{rD['D_confirmed_sell']} | 买P&L均{result[code]['D']['confirmed_buy_pnl_avg%']} 卖P&L均{result[code]['D']['confirmed_sell_pnl_avg%']}")
    result[code]['resonance_raw'] = r3
    result[code]['D_raw'] = rD

with open(os.path.join(OUT, 'compare_3f_D.json'), 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print('\nwrote', os.path.join(OUT, 'compare_3f_D.json'))


# ============ 叠加图: resonance vs D 同路径对比 ============
def plot_overlay(code, name, r3, rD):
    df = load_csv(code)
    d = df[df['trade_date'] == DAY].reset_index(drop=True)
    c = d['close'].values.astype(float)
    vwap_data = ME.compute_miji_indicators(
        d['open'].values.astype(float), d['high'].astype(float),
        d['low'].astype(float), c, d['volume'].values.astype(float),
        float(d['close'].iloc[0]), has_vol=True)['vwap']
    tt = d['trade_time'].values
    n = len(c)
    x = np.arange(n)
    fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
    ax.plot(x, c, color='#555', lw=0.9, zorder=1, label='收盘价')
    ax.plot(x, vwap_data, color='#3498db', lw=0.7, ls='--', alpha=0.6, zorder=1, label='VWAP')
    # D 因果(淡)
    ax.scatter(rD['D_causal_buy_idx'], c[rD['D_causal_buy_idx']], marker='^', s=50, zorder=3,
               facecolors='none', edgecolors='#2ecc71', linewidths=0.7, alpha=0.35,
               label=f"D因果买({rD['D_causal_buy']})")
    ax.scatter(rD['D_causal_sell_idx'], c[rD['D_causal_sell_idx']], marker='v', s=50, zorder=3,
               facecolors='none', edgecolors='#e74c3c', linewidths=0.7, alpha=0.35,
               label=f"D因果卖({rD['D_causal_sell']})")
    # resonance(橙)
    ax.scatter(r3['buy_idx'], c[r3['buy_idx']], marker='o', s=90, zorder=5,
               facecolors='#e67e22', edgecolors='white', linewidths=1.0,
               label=f"三因子买({r3['buy']})")
    ax.scatter(r3['sell_idx'], c[r3['sell_idx']], marker='o', s=90, zorder=5,
               facecolors='#8e44ad', edgecolors='white', linewidths=1.0,
               label=f"三因子卖({r3['sell']})")
    # D 确认(粗实心)
    ax.scatter(rD['D_confirmed_buy_idx'], c[rD['D_confirmed_buy_idx']], marker='^', s=210, zorder=7,
               facecolors='#1e8449', edgecolors='white', linewidths=1.5,
               label=f"D确认买({rD['D_confirmed_buy']})")
    ax.scatter(rD['D_confirmed_sell_idx'], c[rD['D_confirmed_sell_idx']], marker='v', s=210, zorder=7,
               facecolors='#c0392b', edgecolors='white', linewidths=1.5,
               label=f"D确认卖({rD['D_confirmed_sell']})")
    wanted = ['09:31', '10:00', '10:30', '11:00', '11:30', '13:01', '13:30', '14:00', '14:30', '15:00']
    ticks = []
    for w_ in wanted:
        for k, t in enumerate(tt):
            if t >= w_:
                ticks.append(k); break
    ax.set_xticks(ticks); ax.set_xticklabels([tt[k][:5] for k in ticks], fontsize=9)
    ax.set_title(f'{code} {name} · 7/24 · 三因子共振(resonance) vs WhatIf-D 四条件\n'
                 f'橙圆=三因子信号  绿/红实心▲▼=D确认有效点  淡空心=被D因果层剔除的候选', fontsize=13)
    ax.legend(loc='upper left', ncol=3, fontsize=8, framealpha=0.9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    png = os.path.join(OUT, f'compare_{code.split(".")[0]}.png')
    fig.savefig(png, dpi=160); plt.close(fig)
    return png


pngs = []
for code, name in SYMS:
    pngs.append(plot_overlay(code, name, result[code]['resonance_raw'], result[code]['D_raw']))

# ============ HTML 参考报告 ============
def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def row(label, a, b, note=''):
    return f"<tr><td>{esc(label)}</td><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(note)}</td></tr>"


# 从 result 取数
r161 = result['161129.SZ']; r513 = result['513310.SH']
html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>三因子共振 vs WhatIf-D 对比</title>
<style>
body{{background:#0e1116;color:#e6e6e6;font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:0;padding:24px;}}
h1{{font-size:20px;}} h2{{font-size:16px;color:#7fd1ff;margin-top:28px;}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px;}}
th,td{{border:1px solid #2a3340;padding:7px 9px;text-align:left;vertical-align:top;}}
th{{background:#1a2230;color:#9fe0ff;}}
tr:nth-child(even){{background:#151b24;}}
.kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0;}}
.card{{background:#151b24;border:1px solid #2a3340;border-radius:8px;padding:12px 16px;min-width:150px;}}
.card .v{{font-size:22px;font-weight:700;}}
.card .l{{font-size:12px;color:#9fb0c0;}}
img{{max-width:100%;border:1px solid #2a3340;border-radius:6px;margin:10px 0;}}
.note{{color:#ffb86c;font-size:12px;}}
.dis{{color:#8895a5;font-size:12px;margin-top:24px;}}
</style></head><body>
<h1>miji 三因子共振 (resonance) vs WhatIf-D 四条件组合 — 对比参考</h1>
<p class="note">数据基准：2026-07-24 单交易日，标的 161129.SZ 原油LOF / 513310.SH 中韩半导体ETF（1m K线）。
仅 1 日 2 标的，<b>不构成统计显著结论</b>，参数未做 OOS 定参。</p>

<h2>一、异同对照表</h2>
<table>
<tr><th>维度</th><th>三因子共振 (resonance)</th><th>WhatIf-D 四条件</th><th>说明</th></tr>
{row('因子/条件构成','引力 gravity + MACD背离 macd_div + 量价背离 vol_div（三因子）','极值(HIGH/LOW取) + k×ATR偏离 + 5根反转确认 + EMA(20,60)趋势regime','构成完全不同：前者是三因子投票，后者是四层过滤器')}
{row('触发逻辑','OR-within-majority：≥2 个因子同向即放行','AND 交集：四条件全满足才放行','共振=宽松放行；D=严格交集')}
{row('极值判定','收盘价 c[i] 比前窗最高/低价（结构性漏顶/底）','用 BAR 自身 HIGH/LOW 取极值','D 修正了漏顶缺陷，能捕真实拐点')}
{row('偏离度量','引力用固定 FLOOR_DEV_PCT=1.5%（波动率错配）','k×ATR% 波动率归一（k=2.5）','D 阈值随波动自适应，拐点"够得到"')}
{row('趋势处理','b_trend_filter=False，无趋势过滤','EMA20≥EMA60 才允许交易（regime 门控）','共振在下跌段也接飞刀；D 自动规避崩盘段')}
{row('背离定义','单 bar 绿/红柱比上根"短一点"（假背离）','真实 DIF 拐点 + 极值后 5 根多数反向确认','D 的背离是确认式，避免 1 根假反弹')}
{row('出场机制','内置移动止损(trail 0.4/0.6) + 反T闭环 s_signal_exit','仅标注候选点，无内置出场（待定）','共振是完整策略；D 目前只有信号层')}
{row('7/24 信号数(161129)','买{r161["resonance"]["buy"]} 卖{r161["resonance"]["sell"]}','D确认 买{r161["D"]["confirmed_buy"]} 卖{r161["D"]["confirmed_sell"]}','共振更多；D 更少更精')}
{row('7/24 信号数(513310)','买{r513["resonance"]["buy"]} 卖{r513["resonance"]["sell"]}','D确认 买{r513["D"]["confirmed_buy"]} 卖{r513["D"]["confirmed_sell"]}','513310 共振 5 笔全亏；D 0 买 3 卖全赚')}
{row('冗余/互补','三因子部分冗余（引力↔MACD常共触发，vol_div常=0）','四条件互补（各滤不同失效模式）','共振≈2 独立条件；D=分层 AND')}
{row('趋势市适应','弱（无趋势过滤，强趋势中反复接飞刀）','强（regime 门控，只做顺势回撤/衰竭）','D 在趋势市更稳')}
{row('震荡市适应','强（均值回归天然适配区间波动）','弱（无 regime 时几乎不触发）','共振在震荡市更密')}
</table>

<h2>二、7/24 同日绩效快照（向前 15 根 P&amp;L，仅作信号质量参考）</h2>
<div class="kpi">
<div class="card"><div class="v" style="color:#2ecc71">+{r161['resonance']['buy_pnl_avg%']}%</div><div class="l">161129 共振 买均P&L</div></div>
<div class="card"><div class="v" style="color:#e74c3c">{r161['resonance']['sell_pnl_avg%']}%</div><div class="l">161129 共振 卖均P&L</div></div>
<div class="card"><div class="v" style="color:#2ecc71">+{r161['D']['confirmed_buy_pnl_avg%']}%</div><div class="l">161129 D确认 买均P&L</div></div>
<div class="card"><div class="v" style="color:#2ecc71">+{r161['D']['confirmed_sell_pnl_avg%']}%</div><div class="l">161129 D确认 卖均P&L</div></div>
<div class="card"><div class="v" style="color:#e74c3c">{r513['resonance']['buy_pnl_avg%']}%</div><div class="l">513310 共振 买均P&L</div></div>
<div class="card"><div class="v" style="color:#e74c3c">{r513['resonance']['sell_pnl_avg%']}%</div><div class="l">513310 共振 卖均P&L</div></div>
<div class="card"><div class="v" style="color:#2ecc71">+{r513['D']['confirmed_sell_pnl_avg%']}%</div><div class="l">513310 D确认 卖均P&L</div></div>
</div>
<p class="note">注：P&amp;L 为固定 15 根持有，非实际出场；D 的"确认"用到未来 5 根（实盘有 5 根≈5分钟 延迟，且为审计口径，存在前视偏差）。共振的卖在 7/24 平均为负，主因逆趋势与假背离。</p>

<h2>三、信号叠加图</h2>
<img src="compare_161129.png">
<img src="compare_513310.png">

<p class="dis">⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
html_path = os.path.join(OUT, 'compare_3f_D.html')
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)
print('wrote', html_path)
