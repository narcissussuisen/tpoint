#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""v10_factor_grid.py — v10.0.0 因子组合网格寻优（2026-08-05 凌晨 · v10.0.0 分支）

设计（依据：0804 寻优结论 + 0805 R-A/R-B 负结果 + open_source_survey 可借鉴清单）：
- 出场层：trail 0.5/0.5（两段式已 PASS）
- 信号确认层（过滤器·非触发源，R-B 负结果已证触发源不可行）：
  F1 RSI(14) 极值位置过滤：B 需 RSI ≤ rsi_b（防半山腰接刀）；S 需 RSI ≥ rsi_s
  F2 量能确认（CSDN 三重共振借鉴）：B 需 vol_ratio ≤ vol_b_max（缩量回调=抛压衰竭）；
     S 需 vol_ratio ≥ vol_s_min（放量滞涨=动能衰减）
  F3 尾盘禁新开：14:30 后不开新仓（T0GridTrader/T0T 借鉴；X/TRAIL 出场不受限）
- 网格：rsi_b {35,40,45} × rsi_s {65,60,55} × vol_b_max {0.8,1.2,∞} × vol_s_min {1.0,1.5,0}
  先固定 F3=开；共 27 组合 + 两个基线（v9.3.0=trail0.4/0.6 无过滤 / v9.4.1=trail0.5/0.5 无过滤）
- 指标（用户口径）：净胜率/盈亏比/总收益率/最大回撤/夏普/日均信号数；要求「全面优于 9.3.0」
  才认定 v10.0.0 组合成立（收益↑ 回撤↓ 夏普↑，胜率/盈亏比不降）
- 数据：F盘全历史 5 只 watchlist；信号=生产同源复算（detect_for）后按 bar 过滤器过滤
产出：output/v10_grid_<date>.json
"""
import os, sys, json, datetime, itertools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import numpy as np
import factor_optimizer as FO
from exit_manager import aggregate_metrics

wl = json.load(open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8'))

RSI_B = [35, 40, 45]
RSI_S = [65, 60, 55]
VOL_B = [0.8, 1.2, 99.0]     # B 缩量上限（99=不启用）
VOL_S = [1.5, 1.0, 0.0]      # S 放量下限（0=不启用）
CUTOFF = '14:30'             # F3 尾盘禁新开


def rsi14(c):
    """RSI(14) Wilder 平滑，严格尾窗（禁用 np.convolve 'same'——居中窗口=前视，0805 已抓虫）。"""
    n = len(c)
    out = np.full(n, 50.0)
    if n < 15:
        return out
    delta = np.diff(c)
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    au = up[:14].mean(); ad = dn[:14].mean()
    rs = au / ad if ad > 0 else 100.0
    out[14] = 100 - 100 / (1 + rs)
    for i in range(15, n):
        au = (au * 13 + up[i - 1]) / 14
        ad = (ad * 13 + dn[i - 1]) / 14
        rs = au / ad if ad > 0 else 100.0
        out[i] = 100 - 100 / (1 + rs)
    return out


def trailing_ma(x, w):
    """尾窗移动均值（前 w-1 根用已有数据均值，无前视）。"""
    out = np.zeros(len(x))
    s = 0.0
    for i in range(len(x)):
        s += x[i]
        if i >= w:
            s -= x[i - w]
        out[i] = s / min(i + 1, w)
    return out


def apply_filters(sigs, data, df, rsi_b, rsi_s, vol_b, vol_s, cutoff=CUTOFF):
    """对复算信号按 bar 应用 F1/F2/F3 过滤。sigs 含 idx/price/type。"""
    c = data['c']
    rsi = rsi14(c)
    vol = df['volume'].values.astype(float) if 'volume' in df.columns else None
    vma = trailing_ma(vol, 20) if vol is not None else None
    tt = [str(t) for t in (df['trade_time'].values if 'trade_time' in df.columns else [])]
    out = []
    for s in sigs:
        i = s['idx']
        hhmm = tt[i][11:16] if i < len(tt) else '15:00'
        if hhmm >= cutoff:                       # F3 尾盘禁新开
            continue
        if s['type'] == 'B':
            if rsi[i] > rsi_b:                   # F1：位置不够低不接
                continue
            if vol is not None and vma[i] > 0 and vol[i] / vma[i] > vol_b:   # F2：需缩量
                continue
        else:
            if rsi[i] < rsi_s:                   # F1：位置不够高不抛
                continue
            if vol is not None and vma[i] > 0 and vol[i] / vma[i] < vol_s:   # F2：需放量
                continue
        out.append(s)
    return out


def metrics(trips, n_days, n_sig):
    m = aggregate_metrics(trips)
    return {'n_trips': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
            'total_ret': m.get('total_ret', m.get('total_ret_pct', 0)),
            'max_dd': m.get('max_drawdown_pct'),
            'sharpe': m.get('sharpe'), 'sig_per_day': round(n_sig / max(n_days, 1), 2)}


def run_sym(sym, name, days):
    """返回 {variant_key: metrics}。信号只复算一次，过滤器在信号后应用。
    v2（0805 修正）：trail 固定 0.4/0.6（0.5/0.5 已证胜率虚胖/总收益崩）；
    全因子消融——单因子与组合分开评，找「全面优于基线」的真实增益点。"""
    sig_days = FO.day_signals(sym, name, days, FO.CUR_ATR)
    n_raw = sum(len(s) for _, _, s in sig_days)
    res = {}
    res['v9.3.0_baseline'] = metrics(FO.eval_config(sig_days, 0.4, 0.6), len(days), n_raw)

    # 消融变体：(label, trail, rsi_b, rsi_s, vol_b, vol_s, cutoff)；None=不启用该因子
    ABL = [
        ('F3_尾盘禁新开',        (0.4, 0.6), None, None, None, None, '14:30'),
        ('F1_RSI过滤_松45/55',   (0.4, 0.6), 45, 55, None, None, None),
        ('F1_RSI过滤_严40/60',   (0.4, 0.6), 40, 60, None, None, None),
        ('F2_量能确认',          (0.4, 0.6), None, None, 1.2, 1.0, None),
        ('F1F3_松RSI+尾盘',      (0.4, 0.6), 45, 55, None, None, '14:30'),
        ('F1F2_RSI+量能',        (0.4, 0.6), 45, 55, 1.2, 1.0, None),
        ('F1F2F3_全组合',        (0.4, 0.6), 45, 55, 1.2, 1.0, '14:30'),
        ('F2F3_量能+尾盘',       (0.4, 0.6), None, None, 1.2, 1.0, '14:30'),
    ]
    for label, (ta, tp), rb, rs, vb, vs, co in ABL:
        trips = []
        n_f = 0
        for d, data, sigs in sig_days:
            df = data['df']
            fs = sigs
            if rb is not None or vb is not None or co is not None:
                fs = apply_filters(sigs, data, df,
                                   rb if rb is not None else 999, rs if rs is not None else -999,
                                   vb if vb is not None else 99.0, vs if vs is not None else 0.0,
                                   cutoff=co if co else '99:99')
            n_f += len(fs)
            data['sym'] = sym
            trips.extend(FO.eval_config([(d, data, fs)], ta, tp))
        res[label] = metrics(trips, len(days), n_f)
    return res, n_raw


def main():
    date = datetime.date.today().strftime('%Y-%m-%d')
    rep = {'date': date, 'design': 'trail0.5/0.5 + F1 RSI极值过滤 + F2 量能确认 + F3 14:30禁新开',
           'symbols': {}, 'pool': {}}
    pool = {}
    for sym, name in wl.items():
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            rep['symbols'][sym] = {'error': str(e)}
            continue
        res, n_raw = run_sym(sym, name, days)
        rep['symbols'][sym] = {'name': name, 'n_days': len(days), 'variants': res}
        for k, m in res.items():
            pool.setdefault(k, []).append(m)
        b = res['v9.3.0_baseline']
        print(f"[{sym}] 930基线 wr={b['win_rate']}% ret={b['total_ret']} dd={b['max_dd']} sharpe={b['sharpe']}", flush=True)
    # 池级聚合（各变体跨标的平均）
    for k, ms in pool.items():
        ok = [m for m in ms if m['n_trips'] > 0]
        if not ok:
            continue
        rep['pool'][k] = {
            'win_rate': round(sum(m['win_rate'] for m in ok) / len(ok), 1),
            'pl_ratio': round(sum(m['pl_ratio'] for m in ok) / len(ok), 2),
            'total_ret': round(sum(m['total_ret'] for m in ok), 2),
            'max_dd': round(max(m['max_dd'] for m in ok), 2),
            'sharpe': round(sum(m['sharpe'] for m in ok if m['sharpe'] is not None) / max(len([m for m in ok if m['sharpe'] is not None]), 1), 2),
            'sig_per_day': round(sum(m['sig_per_day'] for m in ok) / len(ok), 2),
            'n_trips': sum(m['n_trips'] for m in ok),
        }
    out = os.path.join(ROOT, 'output', f'v10_grid_{date}.json')
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    # 池级排名（按 总收益 排序，打印 top8 + 基线）
    rank = sorted(rep['pool'].items(), key=lambda x: -x[1]['total_ret'])
    print('\n== 池级总收益排名 ==')
    for k, m in rank[:8]:
        print(f"  {k}: ret={m['total_ret']} wr={m['win_rate']}% dd={m['max_dd']} sharpe={m['sharpe']} sig/d={m['sig_per_day']} n={m['n_trips']}")
    for k in ['v9.3.0_baseline']:
        m = rep['pool'].get(k)
        if m:
            print(f"  [基线]{k}: ret={m['total_ret']} wr={m['win_rate']}% dd={m['max_dd']} sharpe={m['sharpe']} sig/d={m['sig_per_day']}")
    print(f'[ok] {out}')


if __name__ == '__main__':
    main()
