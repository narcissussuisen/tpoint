# -*- coding: utf-8 -*-
"""
r4a_triple_stop.py —— R4a 三条件止损（针对个股）离线回测 A/B

背景：P2 发现个股 Precision 53.1%（入场离真底远、易被套）但 Capture 73.5%（最终能兑现）。
      个股问题在「入场后被正常下探洗掉」（stop_mode='atr' 噪音止损太紧）。
T0T 借鉴：三条件止损核心=「技术位确认才止损」（避免噪音洗），tpoint 现有 stop_mode='trend' 即其近似。

本脚本（极简，只动出场、不碰入场、无未来函数、不改 exit_manager 代码）：
  A 基线     = make_config()  默认：stop_mode='atr'(1.5×ATR 噪音止损) + 时间90 + trail 0.4/0.6
  B 三条件   = stop_mode='trend'(技术位翻空才止损) + FIXSTOP 1.5%(尾端兜底) + 时间90 + trail 0.4/0.6

仅针对个股池（is_fund=False），对比 WR/净收益/最大回撤/单笔均值，判定三条件止损是否改善个股出场。
输出：output/r4a_triple_stop_<date>.json（分池 A/B 对比 + 逐标明细）。
"""
import sys, csv, json, os, glob, argparse, datetime, subprocess
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import simulate_day, make_config, cost_for_symbol
from daily_signal_review import build_data
from evaluate_signal_validity_v2 import is_fund

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

HEARTBEAT = os.path.join(OUT, 'r4a_triple_stop_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'r4a_triple_stop_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'

# A 基线（生产默认） vs B 三条件止损（R4a 针对个股）
CFG_A = dict(name='基线(atr1.5+时间90+trail0.4/0.6)',
             cfg=make_config())
CFG_B = dict(name='三条件止损(trend+FIXSTOP1.5+时间90+trail0.4/0.6)',
             cfg=make_config(use_stop=True, stop_mode='trend',
                             use_fixed_stop=True, fixed_stop_pct=1.5,
                             use_time=True, time_stop_bars=90,
                             use_trailing=True, trail_activate_pct=0.4, trail_pct=0.6))


def feishu(text):
    try:
        with open(HEARTBEAT, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(text) + '\n')
    except Exception:
        pass
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs])
        h = np.array([float(x['high']) for x in rs])
        lo = np.array([float(x['low']) for x in rs])
        c = np.array([float(x['close']) for x in rs])
        v = np.array([float(x['volume']) for x in rs])
        days[d] = (o, h, lo, c, v)
    return days


def run_symbol_cfg(sym, cfg, min_days=5):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return {'sym': sym, 'error': 'no_data'}
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cost = cost_for_symbol(sym)
    trips_all = []
    prev_close = None
    n_days_ok = 0
    for d in dates:
        o, h, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        trips_all.extend(simulate_day(sigs, prices, cfg, cost))
        n_days_ok += 1
        prev_close = c[-1]
    if n_days_ok < min_days:
        return {'sym': sym, 'error': f'insufficient_days({n_days_ok})'}
    return {'sym': sym, 'days': n_days_ok, 'trips': trips_all}


def summarize(trips):
    if not trips:
        return dict(n=0, wr=None, total_ret=None, avg_trip=None, max_dd=None, win=0, loss=0)
    n = len(trips)
    wins = sum(1 for t in trips if t['ret_pct'] > 0)
    rets = [float(t['ret_pct']) for t in trips]
    cum = np.cumsum(rets)
    max_dd = float((cum - np.maximum.accumulate(cum)).min()) if n else 0.0
    return dict(n=n, wr=round(100.0 * wins / n, 1), total_ret=round(sum(rets), 2),
                avg_trip=round(sum(rets) / n, 4), win=wins, loss=n - wins, max_dd=round(max_dd, 2))


def _fmt(x):
    return f'{x:.2f}%' if isinstance(x, (int, float)) else str(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0, help='0=全部个股池')
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    stock_syms = [os.path.basename(f).replace('_1m.csv', '') for f in files
                  if not is_fund(os.path.basename(f).replace('_1m.csv', ''))]
    if a.max_syms:
        stock_syms = stock_syms[:a.max_syms]

    feishu(f'🚀 R4a 三条件止损 A/B 回测启动：个股池 {len(stock_syms)} 只\n'
           f'A={CFG_A["name"]}\nB={CFG_B["name"]}')

    per_sym = {}
    agg_A = dict(n=0, wr_wins=0, trips=0, rets=[], dd_sym=[])
    agg_B = dict(n=0, wr_wins=0, trips=0, rets=[], dd_sym=[])
    for idx, sym in enumerate(stock_syms):
        rA = run_symbol_cfg(sym, CFG_A['cfg'])
        rB = run_symbol_cfg(sym, CFG_B['cfg'])
        if 'error' in rA or 'error' in rB:
            continue
        sA = summarize(rA['trips'])
        sB = summarize(rB['trips'])
        per_sym[sym] = dict(days=rA['days'], A=sA, B=sB)
        agg_A['n'] += 1; agg_B['n'] += 1
        agg_A['wr_wins'] += sA['win']; agg_A['trips'] += sA['n']; agg_A['rets'].extend([float(t['ret_pct']) for t in rA['trips']])
        agg_B['wr_wins'] += sB['win']; agg_B['trips'] += sB['n']; agg_B['rets'].extend([float(t['ret_pct']) for t in rB['trips']])
        agg_A['dd_sym'].append(sA['max_dd']); agg_B['dd_sym'].append(sB['max_dd'])
        if (idx + 1) % 30 == 0:
            feishu(f'⏳ R4a 进度 {idx+1}/{len(stock_syms)} 个股')

    def _agg(g):
        rets = g['rets']
        wr = (100.0 * g['wr_wins'] / g['trips']) if g['trips'] else None
        return dict(n_syms=g['n'], n_trips=g['trips'], wr=round(wr, 1) if wr is not None else None,
                    total_ret=round(sum(rets), 2), avg_trip=round(sum(rets) / len(rets), 4) if rets else None,
                    max_dd_sym_mean=round(float(np.mean(g['dd_sym'])), 2) if g['dd_sym'] else None,
                    max_dd_sym_worst=round(float(np.min(g['dd_sym'])), 2) if g['dd_sym'] else None)

    A = _agg(agg_A)
    B = _agg(agg_B)

    out = dict(
        meta=dict(date=a.out_suffix, n_stock=len(stock_syms),
                  A=CFG_A['name'], B=CFG_B['name'],
                  note='R4a 三条件止损针对个股 A/B；仅动出场、不碰入场、无未来函数'),
        A=A, B=B,
        delta=dict(
            wr_pp=(round(B['wr'] - A['wr'], 1) if (A['wr'] is not None and B['wr'] is not None) else None),
            total_ret_pp=(round(B['total_ret'] - A['total_ret'], 2)),
            avg_trip_pp=(round((B['avg_trip'] - A['avg_trip']) * 100, 3) if (A['avg_trip'] is not None and B['avg_trip'] is not None) else None),
        ),
        per_sym=per_sym,
    )
    fn = f'r4a_triple_stop_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'\n=== R4a 三条件止损 A/B（个股 {A["n_syms"]} 只）===')
    print(f'  A {CFG_A["name"]}: trips={A["n_trips"]} WR={_fmt(A["wr"])} net={_fmt(A["total_ret"])} '
          f'avg={_fmt(A["avg_trip"])} 均DD={_fmt(A["max_dd_sym_mean"])}')
    print(f'  B {CFG_B["name"]}: trips={B["n_trips"]} WR={_fmt(B["wr"])} net={_fmt(B["total_ret"])} '
          f'avg={_fmt(B["avg_trip"])} 均DD={_fmt(B["max_dd_sym_mean"])}')
    print(f'  Δ(B-A): WR={out["delta"]["wr_pp"]}pp net={out["delta"]["total_ret_pp"]}% avg={out["delta"]["avg_trip_pp"]}pp')
    print(f'JSON -> {fp}')

    feishu(f'✅ R4a 三条件止损 A/B 完成（个股 {A["n_syms"]} 只）：\n'
           f'A 基线: WR={_fmt(A["wr"])} net={_fmt(A["total_ret"])} avg={_fmt(A["avg_trip"])}；\n'
           f'B 三条件: WR={_fmt(B["wr"])} net={_fmt(B["total_ret"])} avg={_fmt(B["avg_trip"])}；\n'
           f'Δ(B-A): WR={out["delta"]["wr_pp"]}pp net={out["delta"]["total_ret_pp"]}%\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 R4a 三条件止损 A/B 异常: {e}')
        raise
