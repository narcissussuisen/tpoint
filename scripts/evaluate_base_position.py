# -*- coding: utf-8 -*-
"""
evaluate_base_position.py —— 底仓模型合规评估（修复裸卖空后的正确回测）

对账发现致命缺陷：simulate_bidirectional 允许无底仓先 S（裸卖空），虚高反T 收益。
本脚本用 core/simulate_base_position.py 的底仓模型（首日 B 建底仓 → 之后正T+反T+底仓隔夜盈亏），
输出基金/个股 × 正T/反T/持仓 的完整分解，公正检验做T能力。
"""
import sys, csv, json, os, glob, argparse, datetime, subprocess
from dataclasses import replace
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import make_config, cost_for_symbol
from daily_signal_review import build_data
from simulate_base_position import simulate_base_position, agg_rets
from evaluate_signal_validity_v2 import is_fund

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

HEARTBEAT = os.path.join(OUT, 'base_pos_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'base_pos_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'

CFG_SIG = replace(GENERAL_DEFAULT, regime_gate=True, signal_gap=8)
CFG_EXIT = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                       use_fixed_stop=True, fixed_stop_pct=1.5)


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


def run_sym(sym):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return None
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cost = cost_for_symbol(sym)
    sigs_by_day = []
    prev = None
    for d in dates:
        o, h_, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev if prev is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h_, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        prices = {'o': o, 'h': h_, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': len(c), 'date': d, 'pc': pc, 'sym': sym}
        sigs = detect_signals_general(data, pc, CFG_SIG)
        sigs_by_day.append((d, sigs, prices))
        prev = c[-1]
    r = simulate_base_position(sigs_by_day, None, CFG_EXIT, cost)
    L = agg_rets(r['long_ret']); S = agg_rets(r['short_ret']); H = agg_rets(r['hold_ret'])
    return dict(
        sym=sym, is_fund=is_fund(sym), n_days=r['n_days'],
        long_net=L['net'], long_wr=L['wr'], long_n=L['n'],
        short_net=S['net'], short_wr=S['wr'], short_n=S['n'],
        hold_net=H['net'], hold_n=H['n'],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0)
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    syms = [os.path.basename(f).replace('_1m.csv', '') for f in files]
    if a.max_syms:
        syms = syms[:a.max_syms]

    feishu(f'🚀 底仓模型合规评估启动：{len(syms)} 标的（首日B建底仓→正T+反T+隔夜盈亏）')

    rows = []
    for idx, sym in enumerate(syms):
        r = run_sym(sym)
        if r:
            rows.append(r)
        if (idx + 1) % 50 == 0:
            feishu(f'⏳ 底仓模型进度 {idx+1}/{len(syms)}')

    funds = [r for r in rows if r['is_fund']]
    stocks = [r for r in rows if not r['is_fund']]

    def _m(grp, key):
        vals = [r[key] for r in grp if r[key] is not None]
        return float(np.mean(vals)) if vals else None

    def block(grp):
        return dict(
            n=len(grp),
            long_net=_m(grp, 'long_net'), short_net=_m(grp, 'short_net'), hold_net=_m(grp, 'hold_net'),
            long_wr=_m(grp, 'long_wr'), short_wr=_m(grp, 'short_wr'),
            total_net=(_m(grp, 'long_net') or 0) + (_m(grp, 'short_net') or 0) + (_m(grp, 'hold_net') or 0),
        )

    out = dict(
        meta=dict(date=a.out_suffix, note='底仓模型合规回测（首日B建底仓，反T需底仓，隔夜盈亏计入）'),
        fund=block(funds), stock=block(stocks), symbols=rows,
    )
    fn = f'base_position_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    def pct(x):
        return f'{x:.2f}%' if isinstance(x, (int, float)) else 'n/a'

    print(f'\n=== 底仓模型合规评估（基金{out["fund"]["n"]}只 / 个股{out["stock"]["n"]}只）===')
    for name, b in [('基金', out['fund']), ('个股', out['stock'])]:
        print(f'{name}: 正T net={pct(b["long_net"])} WR={pct(b["long_wr"])} | '
              f'反T net={pct(b["short_net"])} WR={pct(b["short_wr"])} | '
              f'底仓隔夜={pct(b["hold_net"])} | 合计={pct(b["total_net"])}')
    print(f'JSON -> {fp}')

    feishu(f'✅ 底仓模型合规评估完成：\n'
           f'基金: 正T={pct(out["fund"]["long_net"])}/反T={pct(out["fund"]["short_net"])}/隔夜={pct(out["fund"]["hold_net"])}/合计={pct(out["fund"]["total_net"])}；\n'
           f'个股: 正T={pct(out["stock"]["long_net"])}/反T={pct(out["stock"]["short_net"])}/隔夜={pct(out["stock"]["hold_net"])}/合计={pct(out["stock"]["total_net"])}\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 底仓模型评估异常: {e}')
        raise
