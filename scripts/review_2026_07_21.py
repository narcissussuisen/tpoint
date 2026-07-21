# -*- coding: utf-8 -*-
"""tpoint 2026-07-21 复盘：用与生产完全一致的引擎(miji_alpha + monitor.detect_for 逻辑)在真实行情上重放。
- 1m K线 (MootdxDataSource，与生产同源)
- MACD_GATE_MODE='strict' (生产默认) 与 'off' (纯引力) 双跑，量化参数影响
- 输出 output/review_2026_07_21.json + logs/review_summary.txt
"""
import sys, os, json, time
sys.path.insert(0, r'C:\Users\YZP\WorkBuddy\Claw\tpoint\core')
sys.path.insert(0, r'C:\Users\YZP\WorkBuddy\Claw\tpoint')
import numpy as np
import pandas as pd
from datasource import MootdxDataSource
from miji_alpha import (compute_miji_indicators, check_b_trigger, check_s_trigger,
                        gravity_signal, macd_divergence_signal, MACD_GATE_MODE)
from indicators import K1, stars
from exit_manager import make_config

TODAY = '2026-07-21'
OUT = r'C:\Users\YZP\WorkBuddy\Claw\tpoint\output\review_2026_07_21.json'
SUM = r'C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\review_summary.txt'

EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
COLDOWN_BARS = 3
MAX_B_DAILY = 12
MAX_S_DAILY = 12
MAX_SIZE_PCT = 8

def strength_size(g_dev_pct, m_present):
    strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
    return 4 if strong else 2

def get_pc(ds, sym, day):
    try:
        d = ds.klines.get(sym, period='1d', count=60)
        if d is None or len(d) == 0:
            return None
        d = d.sort_values('trade_date').reset_index(drop=True)
        last = str(d['trade_date'].iloc[-1])[:10]
        if last == day:
            return float(d['close'].iloc[-2])
        idx = d.index[d['trade_date'] == day]
        if len(idx):
            i = idx[0]
            return float(d['close'].iloc[i-1]) if i > 0 else float(d['close'].iloc[0])
        return float(d['close'].iloc[-1])
    except Exception as e:
        return None

def get_daily_vol_med(ds, sym, day, win=10):
    try:
        d = ds.klines.get(sym, period='1d', count=60)
        if d is None or len(d) == 0:
            return None
        d = d.sort_values('trade_date').reset_index(drop=True)
        vols = d['volume'].clip(lower=0).values.astype(float)
        # 取 day 之前最近 win 根（不含 day 当日）
        idx = d.index[d['trade_date'] == day]
        upto = idx[0] if len(idx) else len(d)
        prev = vols[max(0, upto-win):upto]
        if len(prev) == 0:
            return float(vols.mean())
        return float(np.median(prev))
    except Exception:
        return None

def fetch_1m(ds, sym, day):
    if day == TODAY:
        df = ds.intraday(sym)
    else:
        try:
            df = ds.historical_1m(sym, day)
        except Exception:
            df = None
    if df is None or len(df) < 5:
        return None
    df = df.sort_values('trade_time').reset_index(drop=True)
    df['volume'] = df['volume'].clip(lower=0)
    return df

def build_data(df, pc):
    c = df['close'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    o = df['open'].values.astype(float) if 'open' in df.columns else c.copy()
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    has_vol = v is not None and np.sum(v) > 0
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = df
    return data

def simulate_day(sym, data, pc, name, gate_mode='strict'):
    """忠实复刻 monitor.detect_for 的逐bar逻辑（全量重放，不过滤早盘）。"""
    c = data['c']; lo = data['lo']; h = data['h']; vwap = data['vwap']; atr = data['atr']
    dif = data['dif']; dea = data['dea']; hist = data['hist']
    n = data['n']; df = data['df']
    trade_times = df['trade_time'].values if df is not None else None

    pos = None
    b_count = 0; s_count = 0
    sigs = []  # (type, idx, price, reason)
    run_hi_max = -1e9
    # 诊断
    g_oversold = 0; g_overbought = 0
    m_buydiv = 0; m_selldiv = 0
    max_os_dev = 0.0; max_ob_dev = 0.0
    miss_buy = 0; miss_sell = 0  # gravity触发但被strict gate挡下
    for i in range(2, n):
        if atr[i] <= 0:
            continue
        run_hi_max = max(run_hi_max, h[i])
        near_limit_up = ((run_hi_max - pc) / pc >= 0.20) if pc > 0 else False
        g, gd = gravity_signal(c, vwap, atr, i)
        m, md = macd_divergence_signal(h, lo, c, dif, dea, hist, i)
        if g == 1:
            g_oversold += 1
            max_os_dev = min(max_os_dev, gd)
            if m != 1:
                miss_buy += 1
        if g == -1:
            g_overbought += 1
            max_ob_dev = max(max_ob_dev, gd)
            if m != -1:
                miss_sell += 1
        if m == 1: m_buydiv += 1
        if m == -1: m_selldiv += 1

        # ---- 持仓中：出场管理 ----
        if pos is not None:
            side = pos['side']
            if side == 'long' and c[i] > pos['max_fav']:
                pos['max_fav'] = float(c[i])
            if side == 'short' and c[i] < pos['max_fav']:
                pos['max_fav'] = float(c[i])
            exited = False
            if EXIT_CFG['s_signal_exit']:
                if side == 'long':
                    ts, rs = check_s_trigger(data, i, macd_gate_mode=gate_mode)
                    if ts:
                        sigs.append(('S_exit', i, c[i], rs)); pos = None; exited = True
                else:
                    tb, rb = check_b_trigger(data, i, macd_gate_mode=gate_mode)
                    if tb:
                        sigs.append(('B_exit', i, c[i], rb)); pos = None; exited = True
            if not exited and EXIT_CFG['use_trailing']:
                if side == 'long':
                    fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        tstop = pos['max_fav'] * (1 - EXIT_CFG['trail_pct']/100.0)
                        if c[i] <= tstop:
                            sigs.append(('TRAIL', i, c[i], 'trail')); pos = None; exited = True
                else:
                    fav_ret = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT_CFG['trail_activate_pct']:
                        tstop = pos['max_fav'] * (1 + EXIT_CFG['trail_pct']/100.0)
                        if c[i] >= tstop:
                            sigs.append(('TRAIL', i, c[i], 'trail')); pos = None; exited = True
            continue

        # ---- 空仓：自由双向 ----
        if gate_mode == 'off':
            tb, rb = check_b_trigger(data, i, macd_gate_mode='off')
            ts, rs = check_s_trigger(data, i, macd_gate_mode='off')
        else:
            tb, rb = check_b_trigger(data, i)
            ts, rs = check_s_trigger(data, i)
        if not (tb or ts):
            continue
        if tb:
            s_pct = strength_size((c[i]-vwap[i])/vwap[i]*100.0, 'MACD' in (rb or ''))
            if s_pct > 0 and b_count < MAX_B_DAILY:
                b_count += 1
                sigs.append(('B', i, c[i], rb))
                pos = {'side':'long','entry_price':float(c[i]),'entry_idx':i,
                       'max_fav':float(c[i]),'entry_reason':rb or '','stop_price':-1e9,'size_pct':s_pct}
        if ts:
            s_pct = strength_size((c[i]-vwap[i])/vwap[i]*100.0, 'MACD' in (rs or ''))
            if s_pct > 0 and s_count < MAX_S_DAILY and not near_limit_up:
                s_count += 1
                sigs.append(('S', i, c[i], rs))
                pos = {'side':'short','entry_price':float(c[i]),'entry_idx':i,
                       'max_fav':float(c[i]),'entry_reason':rs or '','stop_price':-1e9,'size_pct':s_pct}
    final_pos = None
    if pos is not None:
        final_pos = {'side': pos['side'], 'entry_price': round(pos['entry_price'],3),
                     'max_fav': round(pos['max_fav'],3), 'entry_reason': pos['entry_reason'],
                     'size_pct': pos['size_pct']}
    return {
        'n_signals': len(sigs), 'b_count': b_count, 's_count': s_count,
        'signals': [(t, int(i), round(float(p),3), r) for t,i,p,r in sigs],
        'final_pos': final_pos,
        'diag': {'g_oversold': g_oversold, 'g_overbought': g_overbought,
                 'm_buydiv': m_buydiv, 'm_selldiv': m_selldiv,
                 'max_os_dev': round(max_os_dev,3), 'max_ob_dev': round(max_ob_dev,3),
                 'miss_buy': miss_buy, 'miss_sell': miss_sell},
    }

def day_stats(df, data, pc):
    c = data['c']; h = data['h']; lo = data['lo']; vwap = data['vwap']; atr = data['atr']; v = data['v']
    n = data['n']
    open_p = float(c[0]); high_p = float(h.max()); low_p = float(lo.min()); close_p = float(c[-1])
    day_chg = (close_p - pc)/pc*100 if pc else 0.0
    total_vol = float(np.sum(data['v'])) if data['v'] is not None else 0.0
    atr_last = float(atr[-1])
    atr_pct = atr_last/close_p*100 if close_p else 0.0
    range_pct = (high_p-low_p)/close_p*100 if close_p else 0.0
    dev_abs = np.abs((c - vwap)/vwap*100)
    avg_dev = float(dev_abs.mean())
    max_dev = float(dev_abs.max())
    return {
        'open': round(open_p,3), 'high': round(high_p,3), 'low': round(low_p,3),
        'close': round(close_p,3), 'pc': round(pc,3) if pc else None,
        'day_chg_pct': round(day_chg,3), 'total_volume': total_vol,
        'atr_last': round(atr_last,4), 'atr_pct': round(atr_pct,3),
        'intraday_range_pct': round(range_pct,3),
        'avg_abs_dev_vwap_pct': round(avg_dev,3), 'max_abs_dev_vwap_pct': round(max_dev,3),
        'n_bars': int(n),
    }

def summarize(sym, name, day, df, pc, daily_vol_med):
    data = build_data(df, pc)
    st = day_stats(df, data, pc)
    strict = simulate_day(sym, data, pc, name, 'strict')
    off = simulate_day(sym, data, pc, name, 'off')
    st['total_volume_vs_med'] = round(st['total_volume']/daily_vol_med,3) if daily_vol_med else None
    return {'sym': sym, 'name': name, 'day': day, 'stats': st,
            'strict': strict, 'off': off}

def main():
    ds = MootdxDataSource()
    results = {'today': {}, 'prior_signal_days_161129': [], 'meta': {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'engine': 'miji_alpha v9.1.4 (1m, strict MACD gate)',
        'MACD_GATE_MODE': MACD_GATE_MODE,
    }}
    # ---- 今日两标的 ----
    for sym, name in [('161129.SZ','原油LOF易方达'), ('688347.SH','华虹宏力')]:
        df = fetch_1m(ds, sym, TODAY)
        if df is None:
            results['today'][sym] = {'error': 'no 1m data'}
            continue
        pc = get_pc(ds, sym, TODAY)
        dvm = get_daily_vol_med(ds, sym, TODAY)
        r = summarize(sym, name, TODAY, df, pc, dvm)
        results['today'][sym] = r
        _dump(results)
        print(f"[today] {sym} {name}: chg={r['stats']['day_chg_pct']}% "
              f"strict(B={r['strict']['b_count']},S={r['strict']['s_count']}) "
              f"off(B={r['off']['b_count']},S={r['off']['s_count']}) "
              f"g_os={r['strict']['diag']['g_oversold']} m_buydiv={r['strict']['diag']['m_buydiv']}")
    # ---- 161129 近N个正常信号日（倒序回溯）----
    candidates = ['2026-07-20','2026-07-17','2026-07-16','2026-07-15','2026-07-14',
                  '2026-07-13','2026-07-10','2026-07-09','2026-07-08','2026-07-07','2026-07-06','2026-07-03']
    collected = 0
    for day in candidates:
        if collected >= 5:
            break
        df = fetch_1m(ds, '161129.SZ', day)
        if df is None:
            print(f"[prior] {day} 161129: 无数据，跳过")
            continue
        pc = get_pc(ds, '161129.SZ', day)
        dvm = get_daily_vol_med(ds, '161129.SZ', day)
        r = summarize('161129.SZ', '原油LOF易方达', day, df, pc, dvm)
        results['prior_signal_days_161129'].append(r)
        _dump(results)
        print(f"[prior] {day} 161129: chg={r['stats']['day_chg_pct']}% "
              f"strict(B={r['strict']['b_count']},S={r['strict']['s_count']}) "
              f"off(B={r['off']['b_count']},S={r['off']['s_count']}) "
              f"g_os={r['strict']['diag']['g_oversold']} m_buydiv={r['strict']['diag']['m_buydiv']}")
        if r['strict']['b_count'] + r['strict']['s_count'] >= 1:
            collected += 1
    _dump(results)
    print("DONE")

def _dump(results):
    try:
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("dump err", e)

if __name__ == '__main__':
    main()
