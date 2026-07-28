# -*- coding: utf-8 -*-
"""VWAP 均值回归做T 信号 -> 复用 miji 盲 holdout 框架 (T+0 ETF/LOF + T+1 个股隔夜)。

移植自聚宽 ATR/VWAP 分时做T 文档的「VWAP 均值回归做T」:
  - 运行 VWAP (当日累计量价, 因果) = cumsum(close*vol)/cumsum(vol)
  - 偏离 VWAP >= 0.8% 反向做T: 价低于VWAP -> 做多; 价高于VWAP -> 做空
  - 止损 0.5% / 止盈 0.6% (入场价计)
  - 回归 VWAP (close 回穿 vwap) 平仓
  - 冷却 10 根

战场/成本复用 miji:
  - T+0 ETF/LOF (longonly): 多空日内往返, 尾盘强平; 成本 买0.05%/卖0.05%
  - T+1 个股 (bidirectional): 仅做多(买背离), 当日不可卖 -> 次日及以后方可平仓
    (次日起 revert/stop/maxhold(3日) 平仓); 成本 买0.05%/卖0.10%(含印花税)

前视规避(铁律):
  - vwap_t 仅用 [0..t] 累计量价 -> 严格因果
  - 入场用 close[i] 判定偏离, 执行于同根收盘
  - 平仓用 close[i]/high[i]/low[i] (已确认 bar) 判定, 无未来引用
  - T+1 当日禁止平仓(法规), 仅次日起允许 -> 无同日前视

盲 holdout 设计(关键):
  - 评估窗口严格 = miji in-sample 的同一 61 天 (读 output/miji_floord_mtf/metrics.json['common'])
  - T+0 盲池 = 缓存/已抓的所有 ETF/LOF 文件, 排除 in-sample 的 513310/161129
  - T+1 盲池 = 4075 缓存个股中先验抽样(按代码排序等距抽, 排除 in-sample 6 只个股), 覆盖阈值>=55/61天
  - 参数原样不重调, 与 docx 一致
"""
import os
import sys
import json
import glob
import bisect
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from pivot_walkforward_p0 import load_day_for, all_dates, _load_sym, DATA_DIR
from miji_floord_mtf import agg_trips, bucket_agg, BUCKETS, COST

OUT = os.path.join(ROOT, 'output', 'miji_vwap_mr')
os.makedirs(OUT, exist_ok=True)

# ---- 容错加载: 缓存存在交易所后缀错配(如 513310.SZ 存为 513310.SH_1m.csv) ----
_VWAP_CACHE = {}


def _resolve(sym):
    num = sym.split('.')[0]
    exact = os.path.join(DATA_DIR, f'{sym}_1m.csv')
    if os.path.exists(exact):
        return exact
    cand = sorted(glob.glob(os.path.join(DATA_DIR, f'{num}*_1m.csv')))
    return cand[0] if cand else None


def load_day_for_vwap(sym, date):
    f = _resolve(sym)
    if f is None:
        return pd.DataFrame(), 0.0
    if sym not in _VWAP_CACHE:
        _VWAP_CACHE[sym] = pd.read_csv(f, encoding='utf-8-sig')
    df = _VWAP_CACHE[sym]
    df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
    df['trade_date'] = df['trade_date'].astype(str)
    day = df[df['trade_date'] == date].reset_index(drop=True)
    if len(day) == 0:
        return day, 0.0
    prev = df[df['trade_date'] < date]['trade_date'].max()
    pc_row = df[df['trade_date'] == prev]
    pc = float(pc_row['close'].iloc[-1]) if len(pc_row) else float(day['close'].iloc[0])
    return day, pc


def all_dates_vwap(sym):
    f = _resolve(sym)
    if f is None:
        return []
    if sym not in _VWAP_CACHE:
        _VWAP_CACHE[sym] = pd.read_csv(f, encoding='utf-8-sig')
    df = _VWAP_CACHE[sym]
    df['trade_date'] = df['trade_date'].astype(str)
    return sorted(df['trade_date'].unique().tolist())


# 用容错版本覆盖, 使 simulate_vwap / coverage_ok 引用到正确文件
load_day_for = load_day_for_vwap
all_dates = all_dates_vwap

# ---- VWAP 参数(与 docx 一致, 不重调) ----
P = dict(dev_thr=0.008, stop_pct=0.005, tp_pct=0.006,
         cooldown=10, min_day_bar=5, vwap_src='close')
MAX_HOLD_BARS = 720  # T+1 最长 ~3 交易日

IN_SAMPLE = {'600519.SH', '601318.SH', '300750.SZ', '600036.SH',
             '688347.SH', '603659.SH', '513310.SZ', '161129.SZ'}

# 读取 miji in-sample 的同一 61 天窗口 (保证可比)
_META = json.load(open(os.path.join(ROOT, 'output', 'miji_floord_mtf', 'metrics.json')))
COMMON = _META['common']


def classify(code):
    num = code.split('.')[0]
    ex = code.split('.')[1]
    if ex == 'SH' and num[:2] in ('51', '56', '58'):
        return 'etf_lof'
    if ex == 'SZ' and num[:2] in ('15', '16', '17', '18'):
        return 'etf_lof'
    return 'stock'


def build_universe():
    files = glob.glob(os.path.join(DATA_DIR, '*_1m.csv'))
    codes = set()
    for f in files:
        base = os.path.basename(f)
        if base.endswith('_1m.csv'):
            codes.add(base[:-len('_1m.csv')])
    etf_lof, stocks = [], []
    for c in codes:
        (etf_lof if classify(c) == 'etf_lof' else stocks).append(c)
    # T+0 盲池: 排除 in-sample 的 ETF/LOF
    t0_blind = sorted(c for c in etf_lof if c not in IN_SAMPLE)
    # T+1 盲池: 个股, 排除 in-sample 6 只, 等距抽样 ~120
    stock_pool = sorted(c for c in stocks if c not in IN_SAMPLE)
    K = max(1, len(stock_pool) // 120)
    t1_blind = stock_pool[::K]
    return sorted(etf_lof), sorted(stocks), t0_blind, t1_blind


def _close_trip(pos, exit_raw, reason, gi, cost_sell, model):
    if pos['side'] == 'long':
        exit_val = exit_raw * (1 - cost_sell)
        pnl = (exit_val - pos['entry_notional']) / pos['entry_notional'] * 100.0
    else:
        exit_val = exit_raw * (1 + cost_sell)
        pnl = (pos['entry_notional'] - exit_val) / pos['entry_notional'] * 100.0
    return dict(side=pos['side'], entry_bar=pos['entry_bar'], exit_bar=gi,
                hold=gi - pos['entry_bar'], pnl=float(pnl), reason=reason,
                entry_date=pos['entry_day'])


def simulate_vwap(sym, model, cost):
    """VWAP 均值回归日内做T (T+0 多空 / T+1 仅多隔夜). 返回 trips 列表."""
    cost_buy, cost_sell = cost
    trips = []
    pos = None
    cooldown_until = -1
    base = 0
    for date in COMMON:
        day, _ = load_day_for(sym, date)
        n = len(day)
        if n < P['min_day_bar'] + 2:
            base += n
            continue
        c = day['close'].values.astype(float)
        h = day['high'].values.astype(float)
        lo = day['low'].values.astype(float)
        o = day['open'].values.astype(float)
        v = day['volume'].values.astype(float)
        if P['vwap_src'] == 'close':
            pv = np.cumsum(c * v)
        else:
            tp = (h + lo + c) / 3.0
            pv = np.cumsum(tp * v)
        cv = np.cumsum(v)
        vwap = np.where(cv > 0, pv / cv, np.nan)
        dev = (c - vwap) / vwap   # 首根无成交量 -> nan, 不触发入场
        for i in range(n):
            gi = base + i
            # 1) 管理持仓
            if pos is not None and (model == 'longonly' or date != pos['entry_day']):
                P0 = pos['entry_raw']
                if pos['side'] == 'long':
                    if lo[i] <= P0 * (1 - P['stop_pct']):
                        trips.append(_close_trip(pos, P0 * (1 - P['stop_pct']), 'stop', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                    if h[i] >= P0 * (1 + P['tp_pct']):
                        trips.append(_close_trip(pos, P0 * (1 + P['tp_pct']), 'tp', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                    if c[i] >= vwap[i]:   # 价回升至 VWAP -> 均值回归平仓
                        trips.append(_close_trip(pos, c[i], 'revert', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                else:  # short (仅 T+0)
                    if h[i] >= P0 * (1 + P['stop_pct']):
                        trips.append(_close_trip(pos, P0 * (1 + P['stop_pct']), 'stop', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                    if lo[i] <= P0 * (1 - P['tp_pct']):
                        trips.append(_close_trip(pos, P0 * (1 - P['tp_pct']), 'tp', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                    if c[i] <= vwap[i]:   # 价回落至 VWAP -> 均值回归平仓
                        trips.append(_close_trip(pos, c[i], 'revert', gi, cost_sell, model))
                        pos, cooldown_until = None, gi + P['cooldown']; continue
                # T+1 最长持有强平
                if model == 'bidirectional' and (gi - pos['entry_bar']) >= MAX_HOLD_BARS:
                    trips.append(_close_trip(pos, c[i], 'max_hold', gi, cost_sell, model))
                    pos, cooldown_until = None, gi + P['cooldown']; continue
                # T+0 尾盘强平
                if model == 'longonly' and i == n - 1:
                    trips.append(_close_trip(pos, c[i], 'eod', gi, cost_sell, model))
                    pos, cooldown_until = None, gi + P['cooldown']; continue
            # 2) 入场
            if pos is None and gi > cooldown_until and i >= P['min_day_bar']:
                d = dev[i]
                if model == 'bidirectional':   # 个股 T+1 仅做多(买背离)
                    if d <= -P['dev_thr']:
                        pos = dict(side='long', entry_raw=c[i],
                                   entry_notional=c[i] * (1 + cost_buy),
                                   entry_bar=gi, entry_day=date)
                else:  # ETF/LOF T+0 多空
                    if d <= -P['dev_thr']:
                        pos = dict(side='long', entry_raw=c[i],
                                   entry_notional=c[i] * (1 + cost_buy),
                                   entry_bar=gi, entry_day=date)
                    elif d >= P['dev_thr']:
                        pos = dict(side='short', entry_raw=c[i],
                                   entry_notional=c[i] * (1 - cost_buy),
                                   entry_bar=gi, entry_day=date)
        base += n
    # 残留持仓: 忽略 (不计入, 避免端点偏差)
    return trips


def coverage_ok(sym):
    if _resolve(sym) is None:
        return False
    ds = set(all_dates(sym))
    return len(ds & set(COMMON)) >= 55


def eval_sym(sym, name, model, cost):
    if _resolve(sym) is None:
        return None
    if not coverage_ok(sym):
        return None
    trips = simulate_vwap(sym, model, cost)
    a = agg_trips(trips)
    mid = len(COMMON) // 2
    early = set(COMMON[:mid]); late = set(COMMON[mid:])
    is_t = [t for t in trips if t['entry_date'] in early]
    oos_t = [t for t in trips if t['entry_date'] in late]
    return {'name': name, 'model': model, 'agg': a,
            'is': agg_trips(is_t), 'oos': agg_trips(oos_t), 'trips': trips}


def _pf_str(pf):
    if pf == float('inf'):
        return 'inf'
    return f"{pf:.2f}"


def main():
    etf_all, stock_all, t0_blind, t1_blind = build_universe()
    print(f"T+0 全 ETF/LOF 文件: {len(etf_all)} | T+1 个股文件: {len(stock_all)}")
    print(f"T+0 盲池(排除 in-sample ETF/LOF): {len(t0_blind)}")
    print(f"T+1 盲池抽样(等距, 排除 in-sample 个股): {len(t1_blind)}")
    print(f"窗口: {len(COMMON)} 天 ({COMMON[0]}..{COMMON[-1]})")

    in_res, t0_res, t1_res = {}, {}, {}
    t0_missing = t1_missing = 0

    # in-sample 参考
    for sym in sorted(IN_SAMPLE):
        model = 'longonly' if classify(sym) == 'etf_lof' else 'bidirectional'
        if _resolve(sym) is None:
            print(f"  [in-sample] {sym:<10} 跳过(无缓存文件)")
            continue
        r = eval_sym(sym, sym, model, COST[model])
        if r is None:
            print(f"  [in-sample] {sym:<10} 跳过(覆盖不足)")
            continue
        in_res[sym] = r
        a = r['agg']
        wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
        print(f"  [in-sample] {sym:<10} {model:<11} n={a['trades']:>4} "
              f"PF={_pf_str(a['pf'])} net={a['net_pct']:+.1f}% WR={wr}")

    # T+0 盲池
    for sym in t0_blind:
        model = 'longonly'
        r = eval_sym(sym, sym, model, COST[model])
        if r is None:
            t0_missing += 1; continue
        t0_res[sym] = r
        a = r['agg']
        wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
        print(f"  [T0-blind]  {sym:<10} n={a['trades']:>4} "
              f"PF={_pf_str(a['pf'])} net={a['net_pct']:+.1f}% WR={wr}")

    # T+1 盲池
    for sym in t1_blind:
        model = 'bidirectional'
        r = eval_sym(sym, sym, model, COST[model])
        if r is None:
            t1_missing += 1; continue
        t1_res[sym] = r
        a = r['agg']
        wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
        print(f"  [T1-blind]  {sym:<10} n={a['trades']:>4} "
              f"PF={_pf_str(a['pf'])} net={a['net_pct']:+.1f}% WR={wr}")

    # 池化 PF (盈亏额加权)
    def pooled_pf(res_map):
        gw = gl = 0.0
        for r in res_map.values():
            for t in r['trips']:
                if t['pnl'] > 0:
                    gw += t['pnl']
                elif t['pnl'] < 0:
                    gl += -t['pnl']
        return (gw / gl) if gl > 0 else (float('inf') if gw > 0 else 0.0)

    n_t0 = len(t0_res); n_t0_gt1 = sum(1 for r in t0_res.values() if r['agg']['pf'] > 1.0)
    n_t1 = len(t1_res); n_t1_gt1 = sum(1 for r in t1_res.values() if r['agg']['pf'] > 1.0)
    n_in = len(in_res); n_in_gt1 = sum(1 for r in in_res.values() if r['agg']['pf'] > 1.0)
    pf_t0 = pooled_pf(t0_res); pf_t1 = pooled_pf(t1_res); pf_in = pooled_pf(in_res)

    summary = dict(
        n_t0_blind=n_t0, n_t0_missing=t0_missing, n_t0_pf_gt1=n_t0_gt1,
        pooled_pf_t0=(float(pf_t0) if pf_t0 != float('inf') else 99.0),
        n_t1_blind=n_t1, n_t1_missing=t1_missing, n_t1_pf_gt1=n_t1_gt1,
        pooled_pf_t1=(float(pf_t1) if pf_t1 != float('inf') else 99.0),
        n_in=n_in, n_in_pf_gt1=n_in_gt1, pooled_pf_in=(float(pf_in) if pf_in != float('inf') else 99.0),
        window=COMMON, params=P,
    )

    # trades.csv (盲池全部成交, 供自检)
    import csv as _csv
    trades_rows = []
    for sym, r in list(t0_res.items()) + list(t1_res.items()):
        for t in r['trips']:
            trades_rows.append({'sym': sym, **t})
    fpath = os.path.join(OUT, 'trades.csv')
    cols = ['sym', 'side', 'entry_bar', 'exit_bar', 'hold', 'pnl', 'reason', 'entry_date']
    with open(fpath, 'w', newline='', encoding='utf-8') as f:
        w = _csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for t in trades_rows:
            w.writerow({c: t.get(c, '') for c in cols})
    print('TRADES ->', fpath, f'({len(trades_rows)} rows)')

    dump = dict(summary=summary,
                in_sample=in_res, t0_blind=t0_res, t1_blind=t1_res)
    with open(os.path.join(OUT, 'metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=2,
                  default=lambda o: float(o) if isinstance(o, float) else o)
    print('METRICS ->', os.path.join(OUT, 'metrics.json'))

    # ---- HTML 报告 ----
    def tbl(res_map, title):
        rows = []
        for sym, r in sorted(res_map.items()):
            a = r['agg']
            wr = f"{a['win_rate']:.0f}%" if a['win_rate'] is not None else '-'
            isp = r['is']; osp = r['oos']
            rows.append(
                f"<tr><td>{sym}</td><td>{r['model']}</td><td>{a['trades']}</td>"
                f"<td>{_pf_str(a['pf'])}</td><td>{a['net_pct']:+.1f}%</td><td>{wr}</td>"
                f"<td>{_pf_str(isp['pf'])}</td><td>{_pf_str(osp['pf'])}</td></tr>")
        return (f"<h2>{title}</h2><table border=1 cellspacing=0 cellpadding=4>"
                f"<tr><th>标的</th><th>模型</th><th>笔数</th><th>PF</th>"
                f"<th>净额%</th><th>胜率</th><th>IS PF</th><th>OOS PF</th></tr>"
                + "".join(rows) + "</table>")

    html = f"""<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<title>VWAP 均值回归 盲 holdout 报告</title></head><body>
<h1>VWAP 均值回归做T — 盲 holdout 报告 (v9.3.0-vwap)</h1>
<p>窗口: {COMMON[0]}..{COMMON[-1]} ({len(COMMON)}天) | 参数: dev={P['dev_thr']*100:.1f}% /
stop={P['stop_pct']*100:.1f}% / tp={P['tp_pct']*100:.1f}% / cool={P['cooldown']} / vwap={P['vwap_src']}</p>
<h2>汇总 (与 miji floord 盲 holdout 对比)</h2>
<table border=1 cellspacing=0 cellpadding=4>
<tr><th>池</th><th>有效数</th><th>缺失</th><th>PF&gt;1 数</th><th>命中率</th><th>池化PF</th></tr>
<tr><td><b>T+0 ETF/LOF 盲</b></td><td>{n_t0}</td><td>{t0_missing}</td>
<td>{n_t0_gt1}/{n_t0}</td><td>{n_t0_gt1/n_t0*100:.0f}%</td><td>{pf_t0 if pf_t0!=float('inf') else 'inf':.3f}</td></tr>
<tr><td><b>T+1 个股 盲</b></td><td>{n_t1}</td><td>{t1_missing}</td>
<td>{n_t1_gt1}/{n_t1}</td><td>{n_t1_gt1/n_t1*100:.0f}%</td><td>{pf_t1 if pf_t1!=float('inf') else 'inf':.3f}</td></tr>
<tr><td>in-sample 参考</td><td>{n_in}</td><td>0</td>
<td>{n_in_gt1}/{n_in}</td><td>{n_in_gt1/n_in*100:.0f}%</td><td>{pf_in if pf_in!=float('inf') else 'inf':.3f}</td></tr>
</table>
<p><b>miji floord 对照 (前轮):</b> T+0 盲池 40 只 PF&gt;1 = 6/40 (15%), 池化PF=0.605。</p>
{tbl(in_res, 'in-sample 参考 (8 只, 含 IS/OOS 拆分)')}
{tbl(t0_res, 'T+0 ETF/LOF 盲池 (非 in-sample)')}
{tbl(t1_res, 'T+1 个股 盲池 (先验等距抽样)')}
</body></html>"""
    with open(os.path.join(OUT, 'report.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print('REPORT ->', os.path.join(OUT, 'report.html'))
    print(f"\n=== 结论速览 ===\nT+0 盲: {n_t0_gt1}/{n_t0} PF>1, 池化PF={pf_t0 if pf_t0!=float('inf') else 'inf':.3f}"
          f"\nT+1 盲: {n_t1_gt1}/{n_t1} PF>1, 池化PF={pf_t1 if pf_t1!=float('inf') else 'inf':.3f}")


if __name__ == '__main__':
    main()
