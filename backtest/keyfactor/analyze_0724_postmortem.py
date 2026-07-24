# -*- coding: utf-8 -*-
"""161129 / 513310 在 2026-07-24 的 floor 信号复盘 + 历史胜率基线对照.

口径(与 monitor.detect_for 同构):
  - 指标: core/miji_alpha.compute_miji_indicators
  - 入口: check_b_trigger/check_s_trigger(macd_gate_mode='floor')
  - 出场: 实盘 EXIT_CFG 纯移动止损(use_trailing=True, trail_activate=0.4, trail_pct=0.6, s_signal_exit=True)
每层独立(每日重置仓位)，与实盘每交易日重置一致。
输出: output/postmortem_20260724/{history_roundtrips.csv, signals_0724.csv, summary.json}
"""
import os
import sys
import json
import csv

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import miji_alpha as MA

MA.MACD_GATE_MODE = 'floor'   # 生产 v9.2.x floor 默认

DATA_DIR = r'F:/keyfactor_data/1m'
OUT = os.path.join(ROOT, 'output', 'postmortem_20260724')
SYMS = [('161129.SZ', '原油LOF(易方达原油QDII-LOF)'),
        ('513310.SH', '中韩半导体ETF')]
DAY = '2026-07-24'

import matplotlib
matplotlib.use('Agg')
import matplotlib.font_manager as fm
FONT = r'C:/Windows/Fonts/simhei.ttf'
if os.path.exists(FONT):
    fm.fontManager.addfont(FONT)
    plt = __import__('matplotlib.pyplot')
    plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
    plt.rcParams['axes.unicode_minus'] = False


# ============ 数据加载 ============
def load_day(sym, day):
    f = os.path.join(DATA_DIR, f'{sym}_1m.csv')
    df = pd.read_csv(f, encoding='utf-8-sig')
    df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
    df['trade_date'] = df['trade_date'].astype(str)
    day_df = df[df['trade_date'] == day].reset_index(drop=True)
    prev = df[df['trade_date'] < day]['trade_date'].max()
    pc_row = df[df['trade_date'] == prev]
    pc = float(pc_row['close'].iloc[-1]) if len(pc_row) else float(day_df['close'].iloc[0])
    return day_df, pc


# ============ 复刻 floor 检测+出场状态机 ============
def run_floor(day, pc, sym, trail_pct=0.6, cutoff_min=None, require_strong=False):
    """cutoff_min: 该时刻(含)之后不再开新仓, 形如 '14:30'; require_strong: 仅强信号(size>=4)入场。"""
    o = day['open'].values.astype(float)
    h = day['high'].values.astype(float)
    lo = day['low'].values.astype(float)
    c = day['close'].values.astype(float)
    v = day['volume'].values.astype(float)
    tt = day['trade_time'].values
    data = MA.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    vwap = data['vwap']; n = data['n']
    COLDOWN_BARS = 3

    def strength_size(g_dev_pct, m_present):
        strong = (abs(g_dev_pct) >= 2.0) or bool(m_present)
        return 4 if strong else 2

    def limit_up(sym):
        code = sym.split('.')[0]
        if code.startswith(('300', '301', '688')):
            return 0.20
        if code.startswith(('8', '4', '92')):
            return 0.30
        return 0.10

    def after_cutoff(t):
        if cutoff_min is None:
            return False
        ch, cm = map(int, cutoff_min.split(':'))
        hh, mm, _ = map(int, t.split(':'))
        return hh * 60 + mm >= ch * 60 + cm

    EXIT = {'use_trailing': True, 'trail_activate_pct': 0.4,
            'trail_pct': trail_pct, 's_signal_exit': True}
    events = []
    pos = None
    b_last = -9999
    s_last = -9999
    b_count = 0
    s_count = 0
    run_hi_max = -1e9
    for i in range(2, n):
        if data['atr'][i] <= 0:
            continue
        run_hi_max = max(run_hi_max, h[i])
        near_limit_up = ((run_hi_max - pc) / pc >= limit_up(sym)) if pc > 0 else False
        if pos is not None:
            if pos['side'] == 'long':
                if c[i] > pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            else:
                if c[i] < pos['max_fav']:
                    pos['max_fav'] = float(c[i])
            exited = False
            if not exited and EXIT['s_signal_exit']:
                if pos['side'] == 'long':
                    ts, rs = MA.check_s_trigger(data, i)
                    if ts:
                        events.append(('X', float(c[i]), i, 'S', pos['entry_price'], pos['side']))
                        pos = None; exited = True
                else:
                    tb, rb = MA.check_b_trigger(data, i)
                    if tb:
                        events.append(('X', float(c[i]), i, 'B', pos['entry_price'], pos['side']))
                        pos = None; exited = True
            if not exited and EXIT['use_trailing']:
                if pos['side'] == 'long':
                    fav_ret = (pos['max_fav'] - pos['entry_price']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 - EXIT['trail_pct'] / 100.0)
                        if c[i] <= trail_stop:
                            events.append(('X', float(c[i]), i, 'TRAIL', pos['entry_price'], pos['side']))
                            pos = None; exited = True
                else:
                    fav_ret = (pos['entry_price'] - pos['max_fav']) / pos['entry_price'] * 100
                    if fav_ret >= EXIT['trail_activate_pct']:
                        trail_stop = pos['max_fav'] * (1 + EXIT['trail_pct'] / 100.0)
                        if c[i] >= trail_stop:
                            events.append(('X', float(c[i]), i, 'TRAIL', pos['entry_price'], pos['side']))
                            pos = None; exited = True
            continue
        tb, rb = MA.check_b_trigger(data, i)
        ts, rs = MA.check_s_trigger(data, i)
        if not (tb or ts):
            continue
        if after_cutoff(tt[i]):
            continue
        if tb:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rb or ''))
            strong = s_pct >= 4
            if (not require_strong or strong) and (i - b_last) >= COLDOWN_BARS and b_count < MA.MAX_B_DAILY:
                b_last = i; b_count += 1
                events.append(('B', float(c[i]), i, rb or '', None, 'long'))
                pos = {'side': 'long', 'entry_price': float(c[i]), 'entry_idx': i, 'max_fav': float(c[i])}
        if ts:
            s_pct = strength_size((c[i] - vwap[i]) / vwap[i] * 100.0, 'MACD' in (rs or ''))
            strong = s_pct >= 4
            if (not require_strong or strong) and (i - s_last) >= COLDOWN_BARS and s_count < MA.MAX_S_DAILY and not near_limit_up:
                s_last = i; s_count += 1
                events.append(('S', float(c[i]), i, rs or '', None, 'short'))
                pos = {'side': 'short', 'entry_price': float(c[i]), 'entry_idx': i, 'max_fav': float(c[i])}
    # EOD 了结
    if pos is not None:
        events.append(('X', float(c[-1]), n - 1, 'EOD', pos['entry_price'], pos['side']))
    out = []
    for typ, price, i, reason, entry, side in events:
        out.append({'type': typ, 'time': tt[i], 'price': price, 'reason': reason,
                    'entry_price': entry, 'side': side, 'idx': int(i)})
    return out


def atr0(data, i):
    return data['atr'][i] <= 0


# ============ 配对 roundtrip ============
def pair_roundtrips(events):
    trips = []
    open_pos = None
    for e in events:
        if e['type'] in ('B', 'S'):
            open_pos = e
        elif e['type'] == 'X' and open_pos is not None:
            entry = open_pos['price']
            exit_p = e['price']
            if open_pos['side'] == 'long':
                pnl = (exit_p / entry - 1) * 100
            else:
                pnl = (entry / exit_p - 1) * 100
            trips.append({'sym': None, 'entry_time': open_pos['time'], 'entry_price': entry,
                          'exit_time': e['time'], 'exit_price': exit_p, 'exit_reason': e['reason'],
                          'side': open_pos['side'], 'pnl_pct': pnl})
            open_pos = None
    if open_pos is not None:
        trips.append({'entry_time': open_pos['time'], 'entry_price': open_pos['entry_price'],
                      'exit_time': 'EOD', 'exit_price': None, 'exit_reason': 'OPEN_EOD',
                      'side': open_pos['side'], 'pnl_pct': None})
    return trips


# ============ 前向收益 ============
def fwd_rets(day_df, sig_time, horizons=(6, 12, 24, 48, 120)):
    c = day_df['close'].values.astype(float)
    tt = day_df['trade_time'].values
    # 定位信号 bar
    idx = None
    for k, t in enumerate(tt):
        if t >= sig_time:
            idx = k; break
    if idx is None:
        return {h: None for h in horizons}
    base = c[idx]
    res = {}
    for h in horizons:
        j = idx + h
        if j < len(c):
            res[h] = (c[j] / base - 1) * 100
        else:
            res[h] = None
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    # ---------- 历史基线: 全部可用交易日 ----------
    hist_trips = []
    hist_signals = []  # 用于 skill
    for sym, name in SYMS:
        f = os.path.join(DATA_DIR, f'{sym}_1m.csv')
        df = pd.read_csv(f, encoding='utf-8-sig')
        df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
        df['trade_date'] = df['trade_date'].astype(str)
        dates = sorted(df['trade_date'].unique())
        for d in dates:
            if d == DAY:
                continue  # 7/24 单独分析
            day, pc = load_day(sym, d)
            if len(day) < 30:
                continue
            ev = run_floor(day, pc, sym)
            trips = pair_roundtrips(ev)
            for t in trips:
                t['sym'] = sym; t['date'] = d
                hist_trips.append(t)
            for e in ev:
                if e['type'] in ('B', 'S'):
                    fr = fwd_rets(day, e['time'])
                    hist_signals.append({'sym': sym, 'date': d, 'type': e['type'],
                                         'price': e['price'], 'fwd': fr})
    # 历史统计
    closed = [t for t in hist_trips if t['pnl_pct'] is not None]
    wins = [t for t in closed if t['pnl_pct'] > 0]
    losses = [t for t in closed if t['pnl_pct'] <= 0]
    hr = len(wins) / len(closed) if closed else 0
    avg_pnl = float(np.mean([t['pnl_pct'] for t in closed])) if closed else 0
    avg_win = float(np.mean([t['pnl_pct'] for t in wins])) if wins else 0
    avg_loss = float(np.mean([t['pnl_pct'] for t in losses])) if losses else 0
    gross_win = sum(t['pnl_pct'] for t in wins)
    gross_loss = -sum(t['pnl_pct'] for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    # 历史 skill (方向前向收益)
    def skill_of(sigs, h):
        vals = []
        for s in sigs:
            v = s['fwd'].get(h)
            if v is None:
                continue
            vals.append(v if s['type'] == 'B' else -v)
        return float(np.mean(vals)) if vals else 0
    hist_skill = {h: skill_of(hist_signals, h) for h in (6, 12, 24, 48, 120)}
    print(f'[HISTORY {SYMS[0][0]}+{SYMS[1][0]}] days={len(dates)-1} roundtrips={len(hist_trips)} '
          f'closed={len(closed)} win_rate={hr:.3f} avg_pnl={avg_pnl:.4f}% '
          f'avg_win={avg_win:.4f}% avg_loss={avg_loss:.4f}% PF={pf:.2f}')
    print(f'  hist_skill=%s' % {h: round(v, 4) for h, v in hist_skill.items()})

    # ---------- 参数敏感性扫描(历史) ----------
    def stat_trips(trips):
        cl = [t for t in trips if t['pnl_pct'] is not None]
        if not cl:
            return dict(n=0, hr=0, avg=0, aw=0, al=0, pf=0)
        w = [t for t in cl if t['pnl_pct'] > 0]
        l = [t for t in cl if t['pnl_pct'] <= 0]
        gw = sum(t['pnl_pct'] for t in w)
        gl = -sum(t['pnl_pct'] for t in l)
        pf = gw / gl if gl > 0 else float('inf')
        return dict(n=len(cl), hr=len(w) / len(cl), avg=float(np.mean([t['pnl_pct'] for t in cl])),
                    aw=float(np.mean([t['pnl_pct'] for t in w])) if w else 0,
                    al=float(np.mean([t['pnl_pct'] for t in l])) if l else 0, pf=pf)

    configs = [
        ('base(0.4/0.6,无时限)', dict(trail_pct=0.6, cutoff_min=None, require_strong=False)),
        ('A:放宽移动止损(0.4/1.0)', dict(trail_pct=1.0, cutoff_min=None, require_strong=False)),
        ('B:尾盘禁开(14:30)', dict(trail_pct=0.6, cutoff_min='14:30', require_strong=False)),
        ('C:A+B组合', dict(trail_pct=1.0, cutoff_min='14:30', require_strong=False)),
        ('D:仅强信号+放宽止损+尾盘禁', dict(trail_pct=1.0, cutoff_min='14:30', require_strong=True)),
    ]
    sweep = []
    for label, kw in configs:
        trips_all = []
        for sym, name in SYMS:
            f = os.path.join(DATA_DIR, f'{sym}_1m.csv')
            df = pd.read_csv(f, encoding='utf-8-sig')
            df['trade_time'] = df['trade_time'].astype(str).str.split(' ').str[-1]
            df['trade_date'] = df['trade_date'].astype(str)
            for d in dates:
                if d == DAY:
                    continue
                day, pc = load_day(sym, d)
                if len(day) < 30:
                    continue
                ev = run_floor(day, pc, sym, **kw)
                for t in pair_roundtrips(ev):
                    t['sym'] = sym
                    trips_all.append(t)
        st = stat_trips(trips_all)
        sweep.append({'config': label, **{k: (round(v, 4) if isinstance(v, float) else v)
                                           for k, v in st.items()}})
        print(f'  SWEEP {label}: n={st["n"]} HR={st["hr"]:.3f} avg={st["avg"]:.4f}% '
              f'aw={st["aw"]:.4f}% al={st["al"]:.4f}% PF={st["pf"]:.2f}')

    with open(os.path.join(OUT, 'sensitivity.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['config', 'n', 'win_rate', 'avg_pnl', 'avg_win', 'avg_loss', 'profit_factor'])
        for s in sweep:
            w.writerow([s['config'], s['n'], s['hr'], s['avg'],
                        s['aw'], s['al'], s['pf']])

    # ---------- 7/24 当日真实信号(审计) ----------
    audit = []
    for line in open(os.path.join(ROOT, 'data', 'push_audit.jsonl'), encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('ts', '').startswith(DAY) and d.get('sym') in dict(SYMS):
            audit.append({'sym': d['sym'], 'type': d['type'], 'time': d['ts'].split(' ')[1],
                          'price': float(d['price'])})
    sig_rows = []
    for a in audit:
        sym, name = [s for s in SYMS if s[0] == a['sym']][0]
        day, pc = load_day(sym, DAY)
        fr = fwd_rets(day, a['time'])
        # 当日 floor roundtrip 真实配对(用审计的 B 与 X)
        sig_rows.append({'sym': sym, 'name': name, 'type': a['type'], 'time': a['time'],
                         'price': a['price'], 'fwd': fr})
    # 7/24 的真实 roundtrip (用审计 B/X 配对)
    day_trips = []
    for sym, name in SYMS:
        aud_sym = [a for a in audit if a['sym'] == sym]
        open_pos = None
        for a in aud_sym:
            if a['type'] in ('B', 'S'):
                open_pos = a
            elif a['type'] == 'X' and open_pos is not None:
                entry = open_pos['price']; exit_p = a['price']
                side = 'long' if open_pos['type'] == 'B' else 'short'
                pnl = (exit_p / entry - 1) * 100 if side == 'long' else (entry / exit_p - 1) * 100
                day_trips.append({'sym': sym, 'side': side, 'entry_time': open_pos['time'],
                                  'entry_price': entry, 'exit_time': a['time'], 'exit_price': exit_p,
                                  'exit_reason': 'live_X', 'pnl_pct': pnl})
                open_pos = None
        if open_pos is not None:
            day, pc = load_day(sym, DAY)
            eod = float(day['close'].values[-1])
            side = 'long' if open_pos['type'] == 'B' else 'short'
            pnl = (eod / open_pos['price'] - 1) * 100 if side == 'long' else (open_pos['price'] / eod - 1) * 100
            day_trips.append({'sym': sym, 'side': side, 'entry_time': open_pos['time'],
                              'entry_price': open_pos['price'], 'exit_time': 'EOD',
                              'exit_price': eod, 'exit_reason': 'EOD', 'pnl_pct': pnl})

    day_closed = [t for t in day_trips if t['pnl_pct'] is not None]
    day_wins = [t for t in day_closed if t['pnl_pct'] > 0]
    day_hr = len(day_wins) / len(day_closed) if day_closed else 0
    day_skill = {h: skill_of([{'type': a['type'], 'fwd': a['fwd']} for a in sig_rows if a['sym'] == sym], h)
                 for h in (6, 12, 24, 48, 120)}
    print(f'[7/24] signals={len(audit)} roundtrips={len(day_trips)} win_rate={day_hr:.3f}')
    for t in day_trips:
        print(f"   {t['sym']} {t['side']:4} {t['entry_time']}@{t['entry_price']} -> "
              f"{t['exit_time']}@{t['exit_price']} ({t['exit_reason']}) pnl={t['pnl_pct']:.3f}%")
    print(f'  7/24 skill=%s' % {h: round(v, 4) for h, v in day_skill.items()})

    # ---------- 写出 CSV / JSON ----------
    with open(os.path.join(OUT, 'history_roundtrips.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sym', 'date', 'side', 'entry_time', 'entry_price', 'exit_time',
                    'exit_price', 'exit_reason', 'pnl_pct'])
        for t in hist_trips:
            w.writerow([t['sym'], t['date'], t['side'], t['entry_time'], t['entry_price'],
                        t['exit_time'], t['exit_price'], t['exit_reason'],
                        '' if t['pnl_pct'] is None else round(t['pnl_pct'], 4)])
    with open(os.path.join(OUT, 'signals_0724.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sym', 'name', 'type', 'time', 'price', 'fwd6', 'fwd12', 'fwd24', 'fwd48', 'fwd120'])
        for a in sig_rows:
            w.writerow([a['sym'], a['name'], a['type'], a['time'], a['price'],
                        a['fwd'][6], a['fwd'][12], a['fwd'][24], a['fwd'][48], a['fwd'][120]])
    with open(os.path.join(OUT, 'day_roundtrips_0724.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sym', 'side', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'exit_reason', 'pnl_pct'])
        for t in day_trips:
            w.writerow([t['sym'], t['side'], t['entry_time'], t['entry_price'], t['exit_time'],
                        t['exit_price'], t['exit_reason'], round(t['pnl_pct'], 4)])

    summary = {
        'history': {'n_days': len(dates) - 1, 'roundtrips': len(hist_trips),
                    'closed': len(closed), 'win_rate': round(hr, 4),
                    'avg_pnl': round(avg_pnl, 4), 'avg_win': round(avg_win, 4),
                    'avg_loss': round(avg_loss, 4), 'profit_factor': round(pf, 2),
                    'skill': {h: round(v, 4) for h, v in hist_skill.items()}},
        'sensitivity': sweep,
        'day0724': {'signals': len(audit), 'roundtrips': len(day_trips),
                    'closed': len(day_closed), 'win_rate': round(day_hr, 4),
                    'trips': day_trips, 'skill': {h: round(v, 4) for h, v in day_skill.items()}},
    }
    with open(os.path.join(OUT, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print('DONE ->', OUT)


if __name__ == '__main__':
    main()
