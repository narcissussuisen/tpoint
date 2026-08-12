# -*- coding: utf-8 -*-
"""08-11 零信号根因判定：生产数据源(mootdx) vs F盘 tickflow 双口径 A/B。

背景（2026-08-12 排查）：
  - 实盘 monitor 08-11 全天 951 轮扫描、数据健康、零异常、**零信号**；
  - 但 F盘 tickflow 离线重放同源 detect_for 得出 300757 有 4 个信号；
  - 二者必有一处失真。mootdx 保留 3-4 天 1m，08-11 今日(08-12)仍可取回 →
    用生产数据源重取 08-11 分钟线跑同一个 detect_for，即可判定：
      * mootdx 也出信号 → 实盘漏检(live bug)
      * mootdx 出 0 信号 → F盘 tickflow 与生产 feed 口径不一致，
        recalc/reconcile 在拿不同数据对账 → 自迭代回灌结论被污染（含 auto_tune 调参）

用法：python scripts/ab_0811_source_diff.py [YYYY-MM-DD]
"""
import sys
import os
import json

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = os.path.join(BASE, 'core')
sys.path.insert(0, CORE)
sys.path.insert(0, os.path.join(BASE, 'venv', 'Lib', 'site-packages'))
os.chdir(CORE)

import pandas as pd
from miji_alpha import compute_miji_indicators
import monitor

# 打桩：严防污染真实 signal.txt / 飞书 / state
monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None
monitor._append_signal_txt = lambda *a, **k: None
monitor.push_batch = lambda *a, **k: None
monitor.save_state = lambda *a, **k: None

DATE = sys.argv[1] if len(sys.argv) > 1 else '2026-08-11'
SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}
FROOT = r'F:\keyfactor_data\1m'

with open(os.path.join(BASE, 'data', 'monitor_config.json'), encoding='utf-8') as f:
    CFG = json.load(f)


def run_detect(sym, name, df, pc):
    """在给定 df 上跑生产同源 detect_for（空仓、无首扫抑制）。"""
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=(v is not None))
    data['df'] = df
    try:
        _tt = df['trade_time'].astype(str)
        _hhmm = _tt.str[11:16]
        data['is_morning'] = ((_hhmm >= '09:30') & (_hhmm < '10:00')).astype(int).values
    except Exception:
        data['is_morning'] = None
    monitor.STATE[sym] = {'PC': pc, 'WARM': None}
    cfg = CFG.get(sym, {})
    sigs = monitor.detect_for(sym, name, data, {},
                              mpr_enable=cfg.get('mpr_enable'),
                              mpr_periods=cfg.get('mpr_periods'),
                              atr_min_pct=cfg.get('atr_min_pct'))
    return sigs, data


def norm(df):
    """统一列名/排序，返回当日 bar。"""
    df = df.copy()
    if 'trade_time' not in df.columns:
        return None
    df['trade_time'] = df['trade_time'].astype(str)
    df = df.sort_values('trade_time').reset_index(drop=True)
    return df


print('=' * 78)
print('08-11 零信号根因 A/B：生产数据源(mootdx) vs F盘 tickflow   DATE=%s' % DATE)
print('=' * 78)

# ---------- 生产数据源 ----------
import datasource as ds
tf = ds.MootdxDataSource()

results = {}
for sym, name in SYMS.items():
    print('\n' + '-' * 78)
    print('### %s(%s)' % (name, sym))
    # 1) F盘 tickflow
    fdf = None
    csv = os.path.join(FROOT, '%s_1m.csv' % sym)
    pc_f = None
    if os.path.exists(csv):
        raw = pd.read_csv(csv)
        raw['trade_date'] = raw['trade_date'].astype(str)
        prev = raw[raw['trade_date'] < DATE]
        pc_f = float(prev['close'].iloc[-1]) if len(prev) else None
        fdf = norm(raw[raw['trade_date'] == DATE])
    # 2) mootdx 生产源
    mdf = None
    try:
        m = tf.historical_1m(sym, DATE.replace('-', ''), offset=2000)
        if m is None or len(m) == 0:
            m = tf.historical_1m(sym, DATE, offset=2000)
        mdf = norm(m) if m is not None and len(m) else None
    except Exception as e:
        print('  mootdx historical_1m 失败: %s' % e)

    print('  bars: F盘=%s  mootdx=%s' % (len(fdf) if fdf is not None else 'NA',
                                          len(mdf) if mdf is not None else 'NA'))
    if fdf is not None and len(fdf):
        print('  F盘  首/末 bar: %s %.4f  ..  %s %.4f' % (
            fdf['trade_time'].iloc[0], fdf['close'].iloc[0],
            fdf['trade_time'].iloc[-1], fdf['close'].iloc[-1]))
    if mdf is not None and len(mdf):
        print('  moot 首/末 bar: %s %.4f  ..  %s %.4f' % (
            mdf['trade_time'].iloc[0], mdf['close'].iloc[0],
            mdf['trade_time'].iloc[-1], mdf['close'].iloc[-1]))

    # PC：优先用 mootdx 日线前收（与生产 refresh_daily 同源）
    pc_m = None
    try:
        dk = tf.get(sym, period='1d', count=8, as_dataframe=True)
        if dk is not None and len(dk):
            dk['trade_date'] = dk['trade_date'].astype(str) if 'trade_date' in dk.columns else None
            if dk['trade_date'] is not None:
                pv = dk[dk['trade_date'] < DATE]
                if len(pv):
                    pc_m = float(pv['close'].iloc[-1])
    except Exception as e:
        print('  日线前收取失败: %s' % e)
    print('  PC: F盘前收=%s  mootdx前收=%s' % (pc_f, pc_m))

    # 逐 bar 对齐差异（同 trade_time 的 close 差）
    if fdf is not None and mdf is not None and len(fdf) and len(mdf):
        a = fdf[['trade_time', 'close', 'volume']].rename(columns={'close': 'c_f', 'volume': 'v_f'})
        b = mdf[['trade_time', 'close', 'volume']].rename(columns={'close': 'c_m', 'volume': 'v_m'})
        mg = a.merge(b, on='trade_time', how='outer', indicator=True)
        both = mg[mg['_merge'] == 'both']
        only_f = mg[mg['_merge'] == 'left_only']
        only_m = mg[mg['_merge'] == 'right_only']
        if len(both):
            d = (both['c_f'].astype(float) - both['c_m'].astype(float)).abs()
            print('  对齐 bar=%d  close 最大差=%.4f  平均差=%.4f  差>0.001 的 bar=%d'
                  % (len(both), d.max(), d.mean(), int((d > 0.001).sum())))
            vf = both['v_f'].astype(float).sum(); vm = both['v_m'].astype(float).sum()
            print('  volume 合计: F盘=%.0f  mootdx=%.0f  比值=%.3f' % (vf, vm, (vf / vm) if vm else 0))
        print('  仅F盘有的bar=%d  仅mootdx有的bar=%d' % (len(only_f), len(only_m)))
        if len(only_f):
            print('    仅F盘示例: %s' % list(only_f['trade_time'].head(5)))
        if len(only_m):
            print('    仅moot示例: %s' % list(only_m['trade_time'].head(5)))

    # 跑 detect_for
    row = {}
    for tag, df_, pc in (('F盘tickflow', fdf, pc_f if pc_f else pc_m),
                         ('mootdx生产源', mdf, pc_m if pc_m else pc_f)):
        if df_ is None or len(df_) < 10 or not pc:
            print('  [%s] 跳过（数据不足/无PC）' % tag)
            row[tag] = None
            continue
        sigs, _ = run_detect(sym, name, df_, pc)
        nb = sum(1 for s in sigs if s[0] == 'B')
        ns = sum(1 for s in sigs if s[0] == 'S')
        nx = sum(1 for s in sigs if s[0] == 'X')
        print('  [%s] pc=%.4f bars=%d → B=%d S=%d X=%d 合计=%d'
              % (tag, pc, len(df_), nb, ns, nx, len(sigs)))
        for s in sigs:
            print('       %s @ %s px=%.3f reason=%s' % (s[0], s[12] if len(s) > 12 else '?', s[1], s[4]))
        row[tag] = (nb, ns, nx)
    results[sym] = row

print('\n' + '=' * 78)
print('结论汇总（signals: B/S/X）')
for sym, row in results.items():
    print('  %-12s F盘=%-12s mootdx=%s' % (sym, row.get('F盘tickflow'), row.get('mootdx生产源')))
print('判定：mootdx 也出信号 → 实盘漏检(live bug)；mootdx=0 而 F盘>0 → 数据口径失真，')
print('      recalc/reconcile 与 auto_tune 结论被污染，需修口径而非改算法参数。')
