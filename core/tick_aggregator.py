# -*- coding: utf-8 -*-
"""core/tick_aggregator.py — tick 分钟聚合 + 一致性校验（P8）

由于 tick_cache 时间戳仅 HH:MM（无秒），无法做真正的 3 秒聚合，降级为**分钟级 tick 聚合**：
  - o/h/l/c（由 tick 重建分钟 OHLC）
  - vol_sum / trade_count / buy_vol / sell_vol
  - vwap = Σ(p*v)/Σv
  - large_tape 计数（vol ≥ 该标的全日 95% 分位）
  - buy_ratio = buy_vol / total_vol

一致性校验：同 (sym, date) 用 F 盘 1m 数据交叉核对 OHLC/volume，报告偏差率。
（若 volume 单位差异（手 vs 股），只比对 OHLC 与量比缩放后的一致性。）
"""
import os

import numpy as np
import pandas as pd

from tick_loader import load_tick_day, list_available

F_DATA_DIR = r'F:/keyfactor_data/1m'


def aggregate_minute(df):
    """tick DataFrame → 分钟级聚合 DataFrame（索引 = time 字符串 HH:MM）。

    输入列：time/price/vol/direction/date。返回列：
    o/h/l/c/vwap/vol_sum/trade_count/buy_vol/sell_vol/buy_ratio/large_tape_count
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    g = df.groupby('time')
    out = pd.DataFrame({
        'o': g['price'].first(),
        'h': g['price'].max(),
        'l': g['price'].min(),
        'c': g['price'].last(),
        'vol_sum': g['vol'].sum(),
        'trade_count': g['price'].count(),
        'buy_vol': g.apply(lambda x: float(x.loc[x['direction'] == 0, 'vol'].sum())),
        'sell_vol': g.apply(lambda x: float(x.loc[x['direction'] == 1, 'vol'].sum())),
    })
    out['vwap'] = (df.assign(pv=df['price'] * df['vol'])
                   .groupby('time').apply(lambda x: x['pv'].sum() / max(x['vol'].sum(), 1e-9)))
    # 大单阈值：全日 95% 分位（per-day 自适应）
    thr = float(np.percentile(df['vol'].values, 95))
    out['large_tape_count'] = g['vol'].apply(lambda v: int((v >= thr).sum()))
    out['buy_ratio'] = out['buy_vol'] / out['vol_sum'].replace(0, np.nan)
    return out


def check_consistency(sym, date_yyyymmdd, min_dev_pct=0.03):
    """tick 分钟聚合 vs F 盘 1m 数据一致性校验。

    返回 dict：{matched, n_tick_min, n_1m_min, ohlc_max_dev_pct, vol_ratio_median}。
    对齐口径：按分钟时间字符串对齐（09:30..11:30, 13:00..15:00）。
    """
    tk = load_tick_day(sym, date_yyyymmdd)
    if tk is None:
        return {'matched': False, 'reason': 'no tick'}
    agg = aggregate_minute(tk)
    fp = os.path.join(F_DATA_DIR, f'{sym}_1m.csv')
    if not os.path.exists(fp):
        return {'matched': False, 'reason': 'no F-data'}
    one = pd.read_csv(fp, encoding='utf-8-sig')
    one['trade_time'] = one['trade_time'].astype(str)
    one_day = one[one['trade_date'] == _to_dash(date_yyyymmdd)]
    if len(one_day) == 0:
        return {'matched': False, 'reason': 'F-data no such day'}
    one_day = one_day.copy()
    one_day['hhmm'] = one_day['trade_time'].str[11:16]
    agg = agg.reset_index().rename(columns={'time': 'hhmm'})
    m = agg.merge(one_day[['hhmm', 'open', 'high', 'low', 'close', 'volume']], on='hhmm', how='inner')
    if len(m) == 0:
        return {'matched': False, 'reason': 'no overlapping minutes'}
    dev = np.max([
        np.abs(m['o'] - m['open']) / m['open'].replace(0, np.nan),
        np.abs(m['h'] - m['high']) / m['high'].replace(0, np.nan),
        np.abs(m['l'] - m['low']) / m['low'].replace(0, np.nan),
        np.abs(m['c'] - m['close']) / m['close'].replace(0, np.nan),
    ])
    ohlc_dev = float(np.nanmax(dev)) * 100.0
    # 量比：tick vol_sum / 1m volume 中位数（单位若为手=100股，比值≈100）
    vr = (m['vol_sum'] / m['volume'].replace(0, np.nan)).median()
    matched = ohlc_dev <= min_dev_pct * 100.0 or ohlc_dev <= 1.0  # 允许 1% 容差（单位/四舍五入）
    return {'matched': bool(matched), 'n_tick_min': len(agg), 'n_1m_min': len(one_day),
            'n_overlap': len(m), 'ohlc_max_dev_pct': round(ohlc_dev, 4),
            'vol_ratio_median': round(float(vr) if np.isfinite(vr) else 0.0, 2)}


def _to_dash(yyyymmdd):
    return f'{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}'


def scan_consistency(sym, sample=None):
    """对某标的所有可用日做一致性扫描。sample=只取前 N 日。返回报告。"""
    avail = [d for s, d in list_available(sym=sym) if s == sym]
    if sample:
        avail = avail[:sample]
    res = []
    for d in avail:
        r = check_consistency(sym, d)
        r['date'] = d
        res.append(r)
    ok = sum(1 for r in res if r.get('matched'))
    return {'sym': sym, 'n_days': len(res), 'matched': ok,
            'pass_rate_pct': round(ok / max(len(res), 1) * 100, 1), 'detail': res}


if __name__ == '__main__':
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else '161129.SZ'
    rep = scan_consistency(sym)
    print(f'{sym}: {rep["n_days"]} 日，一致性通过 {rep["matched"]} 日（{rep["pass_rate_pct"]}%）')
    for r in rep['detail'][:6]:
        print(f"  {r['date']}: matched={r.get('matched')} ohlc_dev={r.get('ohlc_max_dev_pct')}% "
              f"vol_ratio={r.get('vol_ratio_median')} overlap={r.get('n_overlap')}")
