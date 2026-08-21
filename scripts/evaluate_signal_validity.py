# -*- coding: utf-8 -*-
"""
evaluate_signal_validity.py —— 【DET Framework】做T信号三维有效性评估（Phase 0 地基）

命称：DET 做T信号三维有效性评估体系（DET = Direction·Extreme·Theoretical）
不预设正T/反T配对方向。对每个独立信号评估三个维度：
  1. DA  方向准确性(Direction Accuracy)：信号后 H 根内价格是否越过成本线 ε（B 后涨 / S 后跌）
  2. EC  极值捕捉精度(Extreme Capture)：信号点距真实局部极值的偏差 ≤ δ（抓的是真顶底，非噪声）
  3. TEP 理论净值(Theoretical Edge)：若出场完美，该信号本身值多少（剥离出场损耗，只看信号质量）

数据：F:/keyfactor_data/1m_clean 全池（189+ 标的）
引擎：core/general_signal.detect_signals_general（与回测/灰度同源）

默认参数（待分布反调）：
  H_VALUES = [15, 30, 60]   前向评价窗口（根）
  EPS      = 0.0015         成本线阈值（0.15%）
  DELTA    = 0.005          极值捕捉容差（0.5%，DET 主判据；δ 曲线 0.3/0.5/1.0 见 --delta 扫描）
  W        = 30             极值邻域半径（根）
  DA_THRESH= 0.55           系统有效判据（仅标注，先出分布）

用法：
  python evaluate_signal_validity.py [--max-syms N] [--out-suffix DATE]
"""
import sys, csv, json, os, glob, argparse, datetime
from dataclasses import replace
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from composite_scorer_v4full import detect_signals_v4full, V4FULL_DEFAULT
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = r'F:/WorkBuddyItem/automation-2026-08-03-09-39-31'
H_VALUES = [15, 30, 60]
EPS = 0.0015
DELTA = 0.005
W = 30
MIN_DAYS = 3
ENGINE = 'general'          # 'general' | 'v4'：信号生成引擎
VOL_GATE = False            # 仅 general 引擎生效：B 侧量比门控 vol_ratio_b_max=1.2
CLUSTER_THRESH = 8          # 同型信号间隔 <= 此值(根) 视为"冗余聚集"
SIGNAL_GAP = 6              # 信号最小间隔（根）；P1.1 反调 6→8 用 --signal-gap 覆盖


def load_days(path):
    """按 trade_date 分组，返回 {date: (o,h,lo,c,v) ndarray}。"""
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


def _pct(arr):
    """安全分位数（输入 list）。"""
    if not arr:
        return None
    a = np.asarray(arr, dtype=float)
    return dict(median=float(np.median(a)), mean=float(a.mean()),
                p10=float(np.percentile(a, 10)), p90=float(np.percentile(a, 90)))


def _cluster_stats(sigs, thresh):
    """同型信号间隔 <= thresh(根) 视为冗余聚集；返回各级原始计数。
    恒基于原始信号（生成器输出），不受量比门控影响。"""
    last = {'B': -999, 'S': -999}
    tot = {'B': 0, 'S': 0}
    hit = {'B': 0, 'S': 0}
    for s in sorted(sigs, key=lambda x: x['idx']):
        t = s['type']
        tot[t] += 1
        if (s['idx'] - last[t]) <= thresh:
            hit[t] += 1
        last[t] = s['idx']
    return dict(b_total=tot['B'], b_hit=hit['B'], s_total=tot['S'], s_hit=hit['S'])


def evaluate_symbol(sym, path):
    """返回该标的评估 dict，或带 error 的 dict。"""
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    prev_close = None
    sig_list = []   # (sigs, c, n)
    n_ok = 0
    cl_b_total = cl_b_hit = cl_s_total = cl_s_hit = 0
    for d in dates:
        o, h_, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h_, 'low': lo, 'close': c,
                           'volume': v, 'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        # —— 信号生成（引擎可切换）——
        if ENGINE == 'v4':
            sigs = detect_signals_v4full(data, pc, V4FULL_DEFAULT)
        else:
            # 注意：量比门控是 live 单 bar 路径(check_general_b_trigger)独有的过滤，
            # detect_signals_general 批量入口不会读 cfg.vol_ratio_b_max，必须事后按
            # 信号携带的 vol_ratio 字段复刻 live 行为：vol_ratio > 1.2 的 B 丢弃。
            _cfg = replace(GENERAL_DEFAULT, signal_gap=SIGNAL_GAP)
            sigs = detect_signals_general(data, pc, _cfg)
            if VOL_GATE:
                sigs = [s for s in sigs
                        if not (s['type'] == 'B' and isinstance(s.get('vol_ratio'), (int, float))
                                and s['vol_ratio'] > 1.2)]
        # —— 同型信号聚集度（冗余度，基于实际推送信号）——
        _cl = _cluster_stats(sigs, CLUSTER_THRESH)
        cl_b_total += _cl['b_total']; cl_b_hit += _cl['b_hit']
        cl_s_total += _cl['s_total']; cl_s_hit += _cl['s_hit']
        if sigs:
            sig_list.append((sigs, c, len(c)))
            n_ok += 1
        prev_close = c[-1]
    if n_ok < MIN_DAYS:
        return {'sym': sym, 'error': f'insufficient_days({n_ok})'}

    # 池级累加器（跨该标的全部信号）
    b_tot = s_tot = 0
    ec_b_total = ec_s_total = 0
    ec_b_ok = ec_s_ok = 0
    ec_b_err = []
    ec_s_err = []
    da = {H: dict(bt=0, bc=0, btep=[], st=0, sc=0, step=[]) for H in H_VALUES}

    for sigs, c, n in sig_list:
        h_max = min(i + H_VALUES[-1] for i in [0])  # placeholder
        for sig in sigs:
            i = sig['idx']
            typ = sig['type']
            p = float(sig['price'])
            if typ == 'B':
                b_tot += 1
                ec_b_total += 1
            else:
                s_tot += 1
                ec_s_total += 1
            # —— EC：邻域极值（不依赖 H）——
            wl = max(0, i - W)
            wh = min(n - 1, i + W)
            if typ == 'B':
                ext = float(c[wl:wh + 1].min())
                ec_ok = p <= ext * (1 + DELTA)
                ec_err = (p - ext) / ext if ext > 0 else 1.0
                ec_b_err.append(ec_err)
                ec_b_ok += 1 if ec_ok else 0
            else:
                ext = float(c[wl:wh + 1].max())
                ec_ok = p >= ext * (1 - DELTA)
                ec_err = (ext - p) / ext if ext > 0 else 1.0
                ec_s_err.append(ec_err)
                ec_s_ok += 1 if ec_ok else 0
            # —— DA / TEP：前向窗口（依赖 H）——
            lo_i = i + 1
            hi_i = min(i + H_VALUES[-1], n - 1)
            if lo_i > hi_i:
                continue  # 信号在末尾，无法评估前向
            fwd = c[lo_i:hi_i + 1]
            for H in H_VALUES:
                sub = fwd[:H]
                if typ == 'B':
                    best = float(sub.max())
                    is_da = best >= p * (1 + EPS)
                    tep = best / p - 1.0
                    da[H]['bt'] += 1
                    da[H]['bc'] += 1 if is_da else 0
                    da[H]['btep'].append(tep)
                else:
                    best = float(sub.min())
                    is_da = best <= p * (1 - EPS)
                    tep = 1.0 - best / p
                    da[H]['st'] += 1
                    da[H]['sc'] += 1 if is_da else 0
                    da[H]['step'].append(tep)

    def _da_block(H):
        d = da[H]
        bt = d['bt']
        st = d['st']
        return dict(
            B_total=bt, B_da=d['bc'], B_da_rate=(d['bc'] / bt) if bt else None,
            S_total=st, S_da=d['sc'], S_da_rate=(d['sc'] / st) if st else None,
            B_tep_mean=float(np.mean(d['btep'])) if d['btep'] else None,
            B_tep_pos_rate=float(np.mean([1 for x in d['btep'] if x > 0])) if d['btep'] else None,
            S_tep_mean=float(np.mean(d['step'])) if d['step'] else None,
            S_tep_pos_rate=float(np.mean([1 for x in d['step'] if x > 0])) if d['step'] else None,
        )

    per_h_tep = {H: dict(btep=list(da[H]['btep']), step=list(da[H]['step'])) for H in H_VALUES}
    res = dict(
        sym=sym, days=n_ok,
        n_sig=b_tot + s_tot, n_B=b_tot, n_S=s_tot,
        density=round((b_tot + s_tot) / n_ok, 2) if n_ok else 0,
        ec=dict(
            B_ehr=(ec_b_ok / ec_b_total) if ec_b_total else None,
            S_ehr=(ec_s_ok / ec_s_total) if ec_s_total else None,
            B_ec_err=_pct(ec_b_err), S_ec_err=_pct(ec_s_err),
        ),
        cluster=dict(
            B_redundant_frac=(cl_b_hit / cl_b_total) if cl_b_total else None,
            S_redundant_frac=(cl_s_hit / cl_s_total) if cl_s_total else None,
            B_total=cl_b_total, S_total=cl_s_total, thresh=CLUSTER_THRESH,
        ),
        da={f'H{H}': _da_block(H) for H in H_VALUES},
    )
    return res, per_h_tep


def main():
    global DELTA, EPS, H_VALUES, W, ENGINE, VOL_GATE, CLUSTER_THRESH, SIGNAL_GAP
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0, help='0=全部清洁池')
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--delta', type=float, default=DELTA, help='极值捕捉容差 δ（反调扫描用）')
    ap.add_argument('--eps', type=float, default=EPS, help='成本线阈值 ε')
    ap.add_argument('--H', default='15,30,60', help='前向窗口逗号列表')
    ap.add_argument('--w', type=int, default=W, help='极值邻域半径（根）')
    ap.add_argument('--signal-gap', type=int, default=SIGNAL_GAP,
                    help='信号最小间隔（根）；P1.1 反调 6→8 用此覆盖')
    ap.add_argument('--engine', default='general', choices=['general', 'v4'],
                    help='信号生成引擎：general=GT-1.0/v5；v4=composite_scorer_v4full 影子')
    ap.add_argument('--vol-gate', action='store_true',
                    help='仅 general 引擎：加 B 侧量比门控 vol_ratio_b_max=1.2（实盘口径）')
    ap.add_argument('--cluster-thresh', type=int, default=CLUSTER_THRESH,
                    help='同型信号间隔<=此值(根)记为冗余聚集')
    a = ap.parse_args()
    DELTA = a.delta
    EPS = a.eps
    H_VALUES = [int(x) for x in a.H.split(',') if x.strip()]
    W = a.w
    SIGNAL_GAP = a.signal_gap
    ENGINE = a.engine
    VOL_GATE = a.vol_gate
    CLUSTER_THRESH = a.cluster_thresh

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    if a.max_syms:
        files = files[:a.max_syms]

    pool_da = {H: dict(bt=0, bc=0, st=0, sc=0) for H in H_VALUES}
    pool_tep = {H: dict(btep=[], step=[]) for H in H_VALUES}
    pool_ec_b_total = pool_ec_s_total = 0
    pool_ec_b_ok = pool_ec_s_ok = 0
    pool_ec_b_err = []
    pool_ec_s_err = []
    pool_cl_b_total = pool_cl_s_total = 0
    pool_cl_b_hit = pool_cl_s_hit = 0
    pool_total_sigs = 0
    pool_total_days = 0
    sym_results = {}
    n_syms = 0

    for path in files:
        sym = os.path.basename(path).replace('_1m.csv', '')
        r, per_h = evaluate_symbol(sym, path)
        if 'error' in r:
            print(f'[{sym}] SKIP: {r["error"]}')
            continue
        n_syms += 1
        pool_total_sigs += r['n_sig']
        pool_total_days += r['days']
        sym_results[sym] = {k: r[k] for k in ('days', 'n_sig', 'n_B', 'n_S', 'density', 'ec', 'cluster', 'da')}
        # 池级冗余度累加（基于各标原始计数）
        cl = r['cluster']
        if cl['B_total']:
            pool_cl_b_total += cl['B_total']; pool_cl_b_hit += round(cl['B_redundant_frac'] * cl['B_total'])
        if cl['S_total']:
            pool_cl_s_total += cl['S_total']; pool_cl_s_hit += round(cl['S_redundant_frac'] * cl['S_total'])
        # 池级累加（直接跨信号汇总，非 per-symbol 均值平均）
        for H in H_VALUES:
            d = r['da'][f'H{H}']
            p = pool_da[H]
            p['bt'] += d['B_total'] or 0
            p['bc'] += d['B_da'] or 0
            p['st'] += d['S_total'] or 0
            p['sc'] += d['S_da'] or 0
            pool_tep[H]['btep'].extend(per_h[H]['btep'])
            pool_tep[H]['step'].extend(per_h[H]['step'])
        ec = r['ec']
        # EC 池级：用 sym 的 ehr 加权不可行（需原列表）；改为累加 ok/total 比例需原计数
        # 简化：EC 池级直接对 sym 的 ehr 做样本量加权
        if ec['B_ehr'] is not None and r['n_B']:
            pool_ec_b_total += r['n_B']
            pool_ec_b_ok += round(ec['B_ehr'] * r['n_B'])
        if ec['S_ehr'] is not None and r['n_S']:
            pool_ec_s_total += r['n_S']
            pool_ec_s_ok += round(ec['S_ehr'] * r['n_S'])

    # 池级 DA/TEP 需原列表，但 sym 仅存均值 → 池级 DA 率用加权（ok/total 已由 pool_da 累加）
    def _pool_da_block(H):
        p = pool_da[H]
        pt = pool_tep[H]
        bt, st = p['bt'], p['st']
        return dict(
            B_total=bt, B_da=p['bc'], B_da_rate=(p['bc'] / bt) if bt else None,
            S_total=st, S_da=p['sc'], S_da_rate=(p['sc'] / st) if st else None,
            B_tep_mean=float(np.mean(pt['btep'])) if pt['btep'] else None,
            B_tep_pos_rate=float(np.mean([1 for x in pt['btep'] if x > 0])) if pt['btep'] else None,
            S_tep_mean=float(np.mean(pt['step'])) if pt['step'] else None,
            S_tep_pos_rate=float(np.mean([1 for x in pt['step'] if x > 0])) if pt['step'] else None,
        )

    pool = dict(
        n_syms=n_syms, total_sigs=pool_total_sigs, total_days=pool_total_days,
        density=round(pool_total_sigs / pool_total_days, 3) if pool_total_days else 0,
        ec=dict(
            B_ehr=(pool_ec_b_ok / pool_ec_b_total) if pool_ec_b_total else None,
            S_ehr=(pool_ec_s_ok / pool_ec_s_total) if pool_ec_s_total else None,
        ),
        cluster=dict(
            B_redundant_frac=(pool_cl_b_hit / pool_cl_b_total) if pool_cl_b_total else None,
            S_redundant_frac=(pool_cl_s_hit / pool_cl_s_total) if pool_cl_s_total else None,
            B_total=pool_cl_b_total, S_total=pool_cl_s_total, thresh=CLUSTER_THRESH,
        ),
        da={f'H{H}': _pool_da_block(H) for H in H_VALUES},
    )

    _eng_name = ('general_signal.detect_signals_general (GT-1.0/v5)'
                 if ENGINE == 'general' else 'composite_scorer_v4full.detect_signals_v4full (v4 影子)')
    out = dict(
        meta=dict(
            framework='DET Framework (Direction·Extreme·Theoretical)',
            engine=_eng_name,
            vol_gate=VOL_GATE,
            data_dir=DATA_DIR, n_files=len(files),
            params=dict(H_VALUES=H_VALUES, EPS=EPS, DELTA=DELTA, W=W, SIGNAL_GAP=SIGNAL_GAP,
                        MIN_DAYS=MIN_DAYS),
            da_thresh=0.55,
        ),
        pool=pool,
        symbols=sym_results,
    )
    fn = f'signal_validity_default_{a.out_suffix}.json'
    with open(os.path.join(OUT, fn), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # 控制台摘要
    print(f'\n=== 池级（{n_syms} 标的 / {pool_total_days} 交易日 / {pool_total_sigs} 信号）===')
    for H in H_VALUES:
        b = pool['da'][f'H{H}']
        print(f'  H={H:>2}: B-DA={_fmt(b["B_da_rate"])}  S-DA={_fmt(b["S_da_rate"])}')
    print(f'  B-EHR(δ={DELTA})={_fmt(pool["ec"]["B_ehr"])}  S-EHR={_fmt(pool["ec"]["S_ehr"])}')
    cl = pool['cluster']
    print(f'  信号密度={pool["density"]} 笔/日/标的 | 冗余聚集(B/S, ≤{cl["thresh"]}根)='
          f'{_fmt(cl["B_redundant_frac"])}/{_fmt(cl["S_redundant_frac"])}')
    print(f'  引擎={ENGINE} signal_gap={SIGNAL_GAP}{" +量比门控1.2" if VOL_GATE else ""}')
    print(f'JSON -> {os.path.join(OUT, fn)}')


def _fmt(x):
    return f'{x*100:.1f}%' if isinstance(x, (int, float)) else str(x)


if __name__ == '__main__':
    main()
