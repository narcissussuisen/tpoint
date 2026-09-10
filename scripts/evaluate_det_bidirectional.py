# -*- coding: utf-8 -*-
"""
evaluate_det_bidirectional.py —— 原生双向 DET 评估（对账后正确口径）

背景：对账定位 20pp WR 鸿沟根因 = 方向 bug（回测只正T，生产双向）+ regime 门控默认关。
本脚本按生产逻辑「原生路由」：
  - B 信号 → 正T 配对（simulate_day，B 建仓 → S/止损/移动/时间出场）
  - S 信号 → 反T 配对（simulate_bidirectional，S 建仓 → B/止损/移动/时间出场）
  - 信号生成 = 生产参数（regime_gate=true + signal_gap=8）
  - 出场 = 正T EXIT_CFG（FIXSTOP1.5 + trail 0.4/0.6，无 ATR）

输出四象限：基金/个股 × B信号(正T)/S信号(反T) 的 Precision/Capture/Attainment + 双向净收益。

DET 尺度（方向无关）：Precision=B抓底(离真底近)/S抓顶(离真顶近)；Capture=B上方获利/S下方获利。
"""
import sys, csv, json, os, glob, argparse, datetime, subprocess
from dataclasses import replace
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from exit_manager import simulate_day, make_config, cost_for_symbol
from simulate_bidirectional import simulate_bidirectional
from daily_signal_review import build_data
from evaluate_signal_validity_v2 import is_fund, _atr_n, W_PREC, C2, DELTA_ABS

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

HEARTBEAT = os.path.join(OUT, 'det_bidir_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'det_bidir_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'

# 生产参数（对账后正确口径）
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


def eval_symbol(sym):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return None
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cost = cost_for_symbol(sym)
    prev_close = None
    # DET 信号级累加（B/S 分开）
    det = dict(b_tot=0, b_prec=0, b_cap=0, b_att=0, s_tot=0, s_prec=0, s_cap=0, s_att=0)
    long_ret = []; short_ret = []
    for d in dates:
        o, h_, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h_, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        n = len(c)
        sigs = detect_signals_general(data, pc, CFG_SIG)
        prices = {'o': o, 'h': h_, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': n, 'date': d, 'pc': pc, 'sym': sym}
        # 原生双向路由：B→正T，S→反T
        long_ret.extend(t['ret_pct'] for t in simulate_day(sigs, prices, CFG_EXIT, cost))
        short_ret.extend(t['ret_pct'] for t in simulate_bidirectional(sigs, prices, CFG_EXIT, cost))
        # DET 信号级（方向无关）
        for s in sigs:
            i = s['idx']; p = float(s['price']); typ = s['type']
            atr = _atr_n(h_, lo, c, i, 20)
            delta_eff = max(DELTA_ABS, C2 * atr / p) if p > 0 else DELTA_ABS
            X_dyn = (1.0 * atr / p) if (p > 0 and atr > 0) else 0.0015
            eps_dyn = (1.5 * atr / p) if (p > 0 and atr > 0) else 0.005
            # Precision（i+1 起 W_PREC 局部窗口）
            end = min(i + 1 + W_PREC, n)
            if (i + 1 >= end) or ((end - (i + 1)) < W_PREC):
                continue
            if typ == 'B':
                base = float(lo[i + 1:end].min())
                dev = (p - base) / base if base > 0 else 1.0
                det['b_tot'] += 1
                if dev <= delta_eff:
                    det['b_prec'] += 1
                # Capture（上方获利，to-EOD，close）
                if i + 1 < n:
                    mh = float(c[i + 1:].max())
                    if (mh / p - 1.0) >= eps_dyn:
                        det['b_cap'] += 1
                    # Attainment（触达 high）
                    mhh = float(h_[i + 1:].max())
                    if mhh >= p * (1 + X_dyn):
                        det['b_att'] += 1
            else:
                base = float(h_[i + 1:end].max())
                dev = (base - p) / base if base > 0 else 1.0
                det['s_tot'] += 1
                if dev <= delta_eff:
                    det['s_prec'] += 1
                if i + 1 < n:
                    ml = float(c[i + 1:].min())
                    if (1.0 - ml / p) >= eps_dyn:
                        det['s_cap'] += 1
                    mll = float(lo[i + 1:].min())
                    if mll <= p * (1 - X_dyn):
                        det['s_att'] += 1
        prev_close = c[-1]

    def _r(ok, tot):
        return (ok / tot) if tot else None
    bidir_ret = long_ret + short_ret
    return dict(
        sym=sym, is_fund=is_fund(sym),
        prec_b=_r(det['b_prec'], det['b_tot']), prec_s=_r(det['s_prec'], det['s_tot']),
        cap_b=_r(det['b_cap'], det['b_tot']), cap_s=_r(det['s_cap'], det['s_tot']),
        att_b=_r(det['b_att'], det['b_tot']), att_s=_r(det['s_att'], det['s_tot']),
        n_B=det['b_tot'], n_S=det['s_tot'],
        long_net=(sum(long_ret) if long_ret else None), short_net=(sum(short_ret) if short_ret else None),
        bidir_net=(sum(bidir_ret) if bidir_ret else None),
        long_wr=(sum(1 for x in long_ret if x > 0) / len(long_ret) if long_ret else None),
        short_wr=(sum(1 for x in short_ret if x > 0) / len(short_ret) if short_ret else None),
    )


def _grp(rows, key):
    vals = [r[key] for r in rows if r[key] is not None]
    return (float(np.mean(vals)) if vals else None, len(vals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0)
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    syms = [os.path.basename(f).replace('_1m.csv', '') for f in files]
    if a.max_syms:
        syms = syms[:a.max_syms]

    feishu(f'🚀 原生双向 DET 评估启动：{len(syms)} 标的（生产参数 regime开+gap8+双向）')

    rows = []
    for idx, sym in enumerate(syms):
        r = eval_symbol(sym)
        if r:
            rows.append(r)
        if (idx + 1) % 50 == 0:
            feishu(f'⏳ 双向DET 进度 {idx+1}/{len(syms)}')

    funds = [r for r in rows if r['is_fund']]
    stocks = [r for r in rows if not r['is_fund']]

    def block(grp):
        return dict(
            n=len(grp),
            prec_b=_grp(grp, 'prec_b')[0], prec_s=_grp(grp, 'prec_s')[0],
            cap_b=_grp(grp, 'cap_b')[0], cap_s=_grp(grp, 'cap_s')[0],
            att_b=_grp(grp, 'att_b')[0], att_s=_grp(grp, 'att_s')[0],
            long_net=_grp(grp, 'long_net')[0], short_net=_grp(grp, 'short_net')[0],
            bidir_net=_grp(grp, 'bidir_net')[0],
            long_wr=_grp(grp, 'long_wr')[0], short_wr=_grp(grp, 'short_wr')[0],
        )

    out = dict(
        meta=dict(date=a.out_suffix, note='原生双向 DET（regime_gate=true+signal_gap=8+双向配对+正T EXIT_CFG）'),
        fund=block(funds), stock=block(stocks),
        symbols=rows,
    )
    fn = f'det_bidirectional_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    def pct(x):
        return f'{x*100:.1f}%' if isinstance(x, (int, float)) else 'n/a'

    print(f'\n=== 原生双向 DET 四象限（基金{out["fund"]["n"]}只 / 个股{out["stock"]["n"]}只）===')
    for name, b in [('基金', out['fund']), ('个股', out['stock'])]:
        print(f'{name}: B(正T抓底) Prec={pct(b["prec_b"])} Cap={pct(b["cap_b"])} 触达={pct(b["att_b"])} | '
              f'S(反T抓顶) Prec={pct(b["prec_s"])} Cap={pct(b["cap_s"])} 触达={pct(b["att_s"])}')
        print(f'    双向净收益: 正T={pct(b["long_net"])} 反T={pct(b["short_net"])} 合计={pct(b["bidir_net"])} | '
              f'WR 正T={pct(b["long_wr"])} 反T={pct(b["short_wr"])}')
    print(f'JSON -> {fp}')

    feishu(f'✅ 原生双向 DET 完成（基金{out["fund"]["n"]}/个股{out["stock"]["n"]}）：\n'
           f'基金 B Prec={pct(out["fund"]["prec_b"])}/S Prec={pct(out["fund"]["prec_s"])}；'
           f'个股 B Prec={pct(out["stock"]["prec_b"])}/S Prec={pct(out["stock"]["prec_s"])}\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 原生双向 DET 异常: {e}')
        raise
