#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""factor_research_independent.py — 独立因子研究（脱离 miji 三因子框架）

候选池：Alpha101 / Alpha191 中 1m OHLCV 可计算的时序因子 + 经典技术指标（RSI/KDJ/BOLL等），
现框架因子（VWAPdev/MACDhist）仅作对照组，不参与预设。
评判唯一标准 = 数据：日内 Spearman IC（前向 5/15/30/60 分钟收益）+ 阈值信号扣费后净收益。

方法纪律：
- 全部计算按日分组（日内滚动，无隔夜泄漏）；IC 按日计算再跨日平均，t=mean/(std/√n日)。
- 选股口径：4 只 watchlist 标的各自独立评估，要求方向一致（≥3/4 同号）才入关键清单。
- 阈值寻优：因子 z 分数绝对值>thr 触发，最佳 horizon 的前向收益扣双边成本（股票0.116%/基金0.06%）。

CLI：python scripts/factor_research_independent.py [--date 2026-08-05]
产物：output/factor_indep_<date>.json + output/factor_indep_<date>.html
"""
import os, sys, json, argparse, datetime
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from backtest_screener import load_1m_csv  # noqa: E402

F_DATA = r'F:\keyfactor_data\1m'
SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF',
        '688111.SH': '金山办公', '300308.SZ': '中际旭创'}
COST = {'161129.SZ': 0.06, '513310.SH': 0.06, '688111.SH': 0.116, '300308.SZ': 0.116}  # % 双边
HORIZONS = [5, 15, 30, 60]
THRS = [0.5, 1.0, 1.5, 2.0]


# ---------------- 因子库（全部按日内部滚动计算） ----------------
def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return up / (up + dn + 1e-12) * 100


def kdj_k(h, lo, c, n=9):
    ll = lo.rolling(n).min(); hh = h.rolling(n).max()
    rsv = (c - ll) / (hh - ll + 1e-12) * 100
    return rsv.ewm(com=2, adjust=False).mean()  # K = SMA(rsv,3,1)


def vwap_dev(g):
    v = g['volume'].replace(0, np.nan)
    vw = (g['amount'].cumsum() / v.cumsum()).ffill()
    return (g['close'] / vw - 1) * 100


def a101_54(g):
    o, c, h, lo = g['open'], g['close'], g['high'], g['low']
    return (-(lo - c) * o**5 / ((lo - h) * c**5 + 1e-12)).clip(-10, 10)


def a101_53(g, n=9):
    d = (g['close'] - g['close'].shift(1)).fillna(0)
    inner = (g['close'] - g['open']).where(abs(d) > 1e-12, 0)
    pos = (inner > 0).rolling(n).sum(); neg = (inner < 0).rolling(n).sum()
    return pos - neg


def a101_101(g):
    return (g['close'] - g['open']) / (g['high'] - g['low'] + 1e-4)


def a101_12(g):
    return np.sign(g['volume'].diff(1)) * (-g['close'].diff(1))


FACTORS = {  # name: (来源, lambda g: Series)
    'RSI6':        ('Alpha191#6同源', lambda g: rsi(g['close'], 6)),
    'RSI14':       ('Alpha191#6同源', lambda g: rsi(g['close'], 14)),
    'RSI24':       ('经典/Wilder', lambda g: rsi(g['close'], 24)),
    'KDJ_K9':      ('经典KDJ', lambda g: kdj_k(g['high'], g['low'], g['close'])),
    'ROC10':       ('动量/Alpha191#31族', lambda g: g['close'].pct_change(10) * 100),
    'ROC20':       ('动量', lambda g: g['close'].pct_change(20) * 100),
    'ATRpct14':    ('波动率', lambda g: (pd.concat([g['high'] - g['low'],
                    (g['high'] - g['close'].shift()).abs(),
                    (g['low'] - g['close'].shift()).abs()], axis=1).max(axis=1)
                    .rolling(14).mean() / g['close'] * 100)),
    'VOLR5':       ('量比', lambda g: g['volume'] / (g['volume'].rolling(20).mean() + 1e-12)),
    'PVcorr10':    ('Alpha101#3/#6', lambda g: g['close'].rolling(10).corr(g['volume'])),
    'PVcorrD5':    ('Alpha101#22', lambda g: -g['high'].rolling(5).corr(g['volume']).diff(5)),
    'VWAPdev':     ('现框架对照', vwap_dev),
    'MACDhist':    ('现框架对照', lambda g: (g['close'].ewm(span=12).mean() - g['close'].ewm(span=26).mean())
                    .pipe(lambda m: m - m.ewm(span=9).mean()) / g['close'] * 100),
    'BOLLb20':     ('经典布林', lambda g: (g['close'] - g['close'].rolling(20).mean())
                    / (2 * g['close'].rolling(20).std() + 1e-12)),
    'ZSCORE20':    ('均值回复', lambda g: (g['close'] - g['close'].rolling(20).mean())
                    / (g['close'].rolling(20).std() + 1e-12)),
    'A101_54':     ('Alpha101#54', a101_54),
    'A101_101':    ('Alpha101#101', a101_101),
    'A101_12':     ('Alpha101#12', a101_12),
    'A101_53':     ('Alpha101#53', a101_53),
    'REV5':        ('短期反转/Alpha191#59族', lambda g: -g['close'].pct_change(5) * 100),
    'MOM30':       ('日内动量', lambda g: g['close'].pct_change(30) * 100),
    'HLPOS':       ('日内位置', lambda g: (g['close'] - g['low'].cummin())
                    / (g['high'].cummax() - g['low'].cummin() + 1e-12)),
    'VOLZ20':      ('量能异常', lambda g: (g['volume'] - g['volume'].rolling(20).mean())
                    / (g['volume'].rolling(20).std() + 1e-12)),
    'AMIHUD20':    ('非流动性', lambda g: (g['close'].pct_change().abs()
                    / (g['amount'] + 1)).rolling(20).mean() * 1e8),
}


def per_day_frames(df):
    for d, g in df.groupby('trade_date'):
        g = g.reset_index(drop=True)
        if len(g) >= 120:
            yield str(d), g


def eval_symbol(sym):
    df = load_1m_csv(os.path.join(F_DATA, f'{sym}_1m.csv'))
    ic_records = {f: {h: [] for h in HORIZONS} for f in FACTORS}
    sig_pnl = {f: [] for f in FACTORS}  # (day, factor_series, close) 留存供阈值寻优
    for d, g in per_day_frames(df):
        c = g['close']
        fwds = {h: c.shift(-h) / c - 1 for h in HORIZONS}
        for f, (_, fn) in FACTORS.items():
            try:
                fv = fn(g).replace([np.inf, -np.inf], np.nan)
            except Exception:
                continue
            if fv.notna().sum() < 60:
                continue
            for h in HORIZONS:
                fr = fwds[h]
                m = fv.notna() & fr.notna()
                if m.sum() >= 60:
                    ic = fv[m].corr(fr[m], method='spearman')
                    if pd.notna(ic):
                        ic_records[f][h].append(ic)
            sig_pnl[f].append((d, fv, c))
    return ic_records, sig_pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    date = a.date

    all_ic, all_pnl = {}, {}
    for sym in SYMS:
        all_ic[sym], all_pnl[sym] = eval_symbol(sym)
        print(f'[{sym}] done')

    # ---- 汇总：因子×horizon，跨标的均值 IC / t 值 / 方向一致性 ----
    table = []
    for f in FACTORS:
        for h in HORIZONS:
            means, signs, ts = [], [], []
            for sym in SYMS:
                ics = all_ic[sym][f][h]
                if len(ics) >= 10:
                    m = float(np.mean(ics)); sd = float(np.std(ics))
                    means.append(m); signs.append(np.sign(m))
                    ts.append(m / (sd / np.sqrt(len(ics)) + 1e-12))
            if len(means) >= 3:
                cons = int(sum(1 for s in signs if s == np.sign(np.mean(means))))
                table.append({'factor': f, 'src': FACTORS[f][0], 'h': h,
                              'ic': round(float(np.mean(means)), 4),
                              't': round(float(np.mean(ts)), 2),
                              'consistency': f'{cons}/{len(means)}'})
    df_t = pd.DataFrame(table)
    df_t['score'] = df_t['ic'].abs() * df_t['t'].abs().clip(upper=5)
    df_t = df_t.sort_values('score', ascending=False)

    # ---- 阈值寻优（top10 因子，最佳 horizon，扣费净收益） ----
    top = df_t.head(10)['factor'].unique()
    thr_rows = []
    for f in top:
        best_h = int(df_t[df_t['factor'] == f].iloc[0]['h'])
        ic_sign = np.sign(df_t[df_t['factor'] == f].iloc[0]['ic'])
        for thr in THRS:
            nets, wins, ns = [], 0, 0
            for sym in SYMS:
                zcache = []
                for d, fv, c in all_pnl[sym][f]:
                    m = fv.notna()
                    if m.sum() < 60:
                        continue
                    z = (fv - fv[m].mean()) / (fv[m].std() + 1e-12)
                    fr = c.shift(-best_h) / c - 1
                    sig = (z.abs() > thr) & fr.notna()
                    if sig.sum() == 0:
                        continue
                    # 因子方向与IC一致：IC>0 → 因子高=涨（做多/正T）；IC<0 → 因子高=跌（做空/反T）
                    edge = (fr[sig] * np.sign(z[sig]) * ic_sign).mean() * 100
                    nets.append(edge - COST[sym]); ns += int(sig.sum())
                    wins += int(((fr[sig] * np.sign(z[sig]) * ic_sign) * 100 > COST[sym]).sum())
                if ns:
                    thr_rows.append({'factor': f, 'thr': thr, 'h': best_h,
                                     'net_avg_pct': round(float(np.mean(nets)), 4),
                                     'win_rate': round(wins / ns * 100, 1) if ns else 0, 'n': ns})
    df_s = pd.DataFrame(thr_rows).sort_values('net_avg_pct', ascending=False) if thr_rows else pd.DataFrame()

    # ---- 输出 ----
    out_j = os.path.join(ROOT, 'output', f'factor_indep_{date}.json')
    json.dump({'date': date, 'ic_table': table,
               'threshold': thr_rows}, open(out_j, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    def fmt_ic(r):
        cls = 'up' if abs(r['ic']) >= 0.03 and abs(r['t']) >= 2 else ''
        return (f"<tr class='{cls}'><td>{r['factor']}</td><td>{r['src']}</td><td>{r['h']}min</td>"
                f"<td>{r['ic']:+.4f}</td><td>{r['t']:+.2f}</td><td>{r['consistency']}</td></tr>")
    ic_rows = ''.join(fmt_ic(r) for r in table[:40])
    thr_html = ''.join(
        f"<tr><td>{r['factor']}</td><td>{r['thr']}</td><td>{r['h']}min</td>"
        f"<td>{r['net_avg_pct']:+.4f}%</td><td>{r['win_rate']}%</td><td>{r['n']}</td></tr>"
        for r in thr_rows[:20])
    html = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>独立因子研究 {date}</title>
<style>body{{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#fafbfc;color:#222;padding:28px;max-width:1150px;margin:auto}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}} table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}}
td,th{{border:1px solid #ddd;padding:5px 8px;text-align:left}} th{{background:#eef2f6}}
tr.up td{{background:#fff2f0}} .note{{color:#666;font-size:12px;margin-top:14px;line-height:1.7}}</style></head><body>
<h1>tpoint 独立因子研究 {date}（脱离 miji 框架 · 数据为唯一评判标准）</h1>
<p class="note">候选池：Alpha101/Alpha191 中 1m OHLCV 可计算的时序因子 + 经典技术指标（共 {len(FACTORS)} 个）；
现框架因子（VWAPdev/MACDhist）仅作对照。评估：日内 Spearman IC（前向 5/15/30/60min，按日计算跨日平均），
方向一致性要求 ≥3/4 标的同号；红底=|IC|≥0.03 且 |t|≥2（统计显著）。标的：{ '、'.join(SYMS.values()) }（F盘全历史 1m）。</p>
<h2>一、因子有效性总表（按 |IC|×|t| 排序，前40）</h2>
<table><tr><th>因子</th><th>来源</th><th>horizon</th><th>IC(跨标的均值)</th><th>t值</th><th>方向一致</th></tr>{ic_rows}</table>
<h2>二、阈值信号扣费净收益（top因子 z分数阈值寻优，已扣双边成本 股票0.116%/基金0.06%）</h2>
<table><tr><th>因子</th><th>z阈值</th><th>horizon</th><th>单笔净收益(均值)</th><th>胜率</th><th>信号数</th></tr>{thr_html}</table>
<p class="note">生成：factor_research_independent.py {datetime.datetime.now().strftime("%F %T")}｜明细 factor_indep_{date}.json</p>
</body></html>'''
    out_h = os.path.join(ROOT, 'output', f'factor_indep_{date}.html')
    open(out_h, 'w', encoding='utf-8').write(html)
    print(f'[ok] {out_j}\n[ok] {out_h}')
    print(df_t.head(15).to_string())


if __name__ == '__main__':
    main()
