# -*- coding: utf-8 -*-
"""
evaluate_signal_validity_v2.py —— DET v2.0 三轴信号质量评估（Attainment·Precision·Capture·TEP）

针对「日内分时抓顶底」重定义，**退役净方向 DA_net**（mu>-md，衡量涨跌预测，结构性≈50%）。

三轴（+ 理论边）：
  1. Attainment  触达命中率（无未来函数 → 实盘信号 gate）
       B: max(fwd_high) >= p*(1+X)   ；S: min(fwd_low) <= p*(1-X)
       语义：信号方向的目标位有没有被「摸到过」。
  2. Precision   极值邻近精度（含未来函数 → 仅盘后回测，防开天眼：窗口 i+1 起）
       B: 真底 = min(low[i+1 : i+1+W_PREC])，dev = (p - 真底)/真底；
       命中 = dev <= delta_eff（lag = argmin+1 >= 1，天然 <= W_PREC，无需额外 K_MAX 约束）。
       S: 真顶 = max(high[i+1 : i+1+W_PREC])，dev = (真顶 - p)/真顶。
       语义：信号点离真极值多近（抓得准不准）。窗口从 i+1 起，真极值必在信号之后，消除 lag=0 开天眼。
  3. Capture     兑现率（无未来函数）
       B: max(fwd_close)/p - 1 >= eps_dyn  ；S: 1 - min(fwd_close)/p >= eps_dyn
       语义：入场后实际获利空间是否达标（抓到后赚没赚到）。用 close（可成交价），非 high。
  + TEP 理论边（扣成本 COST=0.0005）：
       B: edge = max(fwd_high)/p - 1 - COST；S: edge = 1 - min(fwd_low)/p - COST；TEP = P(edge>0)。

综合分（纯乘法 1.0×1.0）：composite = Precision_rate × Capture_rate @ to-EOD。

动态阈值（双轨，主判据=相对口径 ATR 比例）：
  绝对口径：X_ABS=0.0015, EPS_ABS=0.005, DELTA_ABS=0.005
  相对口径：X = k*ATR_n/p, eps = c1*ATR_n/p, delta = c2*ATR_n/p
  默认 k=0.5, c1=0.5, c2=0.25, ATR_n=信号前20根（无未来函数）。

数据：F:/keyfactor_data/1m_clean 全池
引擎：core/general_signal.detect_signals_general（与生产 monitor 同源）

用法：
  python evaluate_signal_validity_v2.py [--max-syms N] [--out-suffix DATE] [--k 0.5] [--c1 0.5] [--c2 0.25] [--k-max 60]
"""
import sys, csv, json, os, glob, argparse, datetime, subprocess
from dataclasses import replace
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

# —— 口径参数（DET v2.0）——
HORIZONS = [30, 60, 120, 'EOD']   # EOD = to-EOD（信号→当日收盘，主判据）
X_ABS = 0.0015       # 触达阈值（绝对口径，0.15%）
EPS_ABS = 0.005      # 兑现阈值（绝对口径，0.5%）
DELTA_ABS = 0.005    # 邻近容差（绝对口径，0.5%）
K = 1.0              # 触达阈值 ATR 系数（专家口径：p±1×ATR 视为触顶/触底）
C1 = 1.5             # 兑现阈值 ATR 系数（实质获利，须 > K 避免与触达退化重合）
C2 = 1.0             # 邻近容差 ATR 系数（信号贴极值的容许偏差）
ATR_N = 20           # ATR 回看根数（信号前，无未来函数）
W_PREC = 30          # 【业务硬约束，严禁纳入优化参数池】抓顶底=看信号后 30 根 K 线（约 30 分钟持有周期）
                     # 邻近率对 W_PREC 固有敏感（20→40 根 69.2%→58.1%），一旦开放给优化器会"曲线救国"过拟合，必须锁定。
COST = 0.0005        # 单边成本（佣金万1×2 + 滑点，与成本模型一致）
MIN_DAYS = 10          # 评审修复⑤：3→10，剔除短样本标的（ETF 67 天 vs 个股 21 天权重失衡）
SIGNAL_GAP = 6
VOL_GATE = False
ENGINE = 'general'

# —— P2 基线（2026-08-23，187标/6000日，标的等权口径）——
# 【双轨制铁律】对外汇报唯一主口径=标的等权，且基金/个股必须分池，禁止混算均值。
# R4 及后续任何改动，必须分类别跑数据、与下列基线对比：个股 Precision 显著提升（如 53.1%→57%）才证明有价值，
# 杜绝「基金高精度掩盖个股低分」的假象。
BASELINE = dict(
    fund=dict(prec_b=0.793, cap_b=0.696, comp_b=round(0.793 * 0.696, 4)),    # 基金：Prec 79.3% / Cap 69.6%
    stock=dict(prec_b=0.531, cap_b=0.735, comp_b=round(0.531 * 0.735, 4)),   # 个股：Prec 53.1% / Cap 73.5%
    note='P2 基线（标的等权）：R4 分类别增益判据',
)


def is_fund(sym):
    """标的类型识别：基金(ETF/LOF) vs 个股。P2 发现两池 Precision 差 26.2pp，必须分池评估。"""
    code = sym.split('.')[0]
    # ETF/LOF：沪市 5xxxxx（50/51/56/58 等）；深市 15xxxx/16xxxx/18xxxx
    return code.startswith('5') or code.startswith('15') or code.startswith('16') or code.startswith('18')

# —— 监控（longtask-feishu-monitor 模式）——
HEARTBEAT = os.path.join(OUT, 'signal_validity_v2_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'signal_validity_v2_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'


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


def _atr_n(h, lo, c, i, n=ATR_N):
    """信号 idx=i 前 n 根的平均真实波幅（无未来函数）。"""
    trs = []
    lo_i = max(1, i - n)
    for j in range(lo_i, i):
        pc = c[j - 1] if j > 0 else c[0]
        trs.append(max(h[j] - lo[j], abs(h[j] - pc), abs(lo[j] - pc)))
    return float(np.mean(trs)) if trs else 0.0


def _fmt(x):
    return f'{x*100:.1f}%' if isinstance(x, (int, float)) else str(x)


def evaluate_symbol(sym, path):
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    prev_close = None
    sig_list = []  # (sigs, o, h, lo, c, n)
    n_ok = 0
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
        _cfg = replace(GENERAL_DEFAULT, signal_gap=SIGNAL_GAP)
        sigs = detect_signals_general(data, pc, _cfg)
        if VOL_GATE:
            sigs = [s for s in sigs
                    if not (s['type'] == 'B' and isinstance(s.get('vol_ratio'), (int, float))
                            and s['vol_ratio'] > 1.2)]
        if sigs:
            sig_list.append((sigs, o, h_, lo, c, len(c)))
            n_ok += 1
        prev_close = c[-1]
    if n_ok < MIN_DAYS:
        return {'sym': sym, 'error': f'insufficient_days({n_ok})'}

    # 三轴累加器（Attainment/Capture/TEP 按 horizon；Precision 单独 to-EOD）
    acc = {h: dict(b_tot=0, b_att=0, b_cap=0, b_tep=0, s_tot=0, s_att=0, s_cap=0, s_tep=0)
           for h in HORIZONS}
    prec = dict(b_tot=0, b_hit=0, b_dev=[], s_tot=0, s_hit=0, s_dev=[])

    for sigs, o, h_, lo, c, n in sig_list:
        for sig in sigs:
            i = sig['idx']
            typ = sig['type']
            p = float(sig['price'])
            atr = _atr_n(h_, lo, c, i, ATR_N)
            X_dyn = (K * atr / p) if (p > 0 and atr > 0) else X_ABS
            eps_dyn = (C1 * atr / p) if (p > 0 and atr > 0) else EPS_ABS
            delta_dyn = (C2 * atr / p) if (p > 0 and atr > 0) else DELTA_ABS

            # —— Attainment / Capture / TEP（前向窗口）——
            # 触达(Attainment)用 high/low：触及目标位看日内真实高低点。
            # 兑现(Capture)/理论边(TEP)用 close：保守可成交价，避免 high 高估（评审修复①）。
            for hh in HORIZONS:
                end = n if hh == 'EOD' else min(i + 1 + hh, n)
                if i + 1 >= end:
                    continue
                fwd_h = h_[i + 1:end]
                fwd_l = lo[i + 1:end]
                fwd_c = c[i + 1:end]
                if len(fwd_h) == 0:
                    continue
                if typ == 'B':
                    mh = float(np.max(fwd_h))      # 触达用 high
                    mc = float(np.max(fwd_c))      # 兑现/理论边用 close
                    att = mh >= p * (1 + X_dyn)
                    cap = (mc / p - 1.0) >= eps_dyn
                    tep = (mc / p - 1.0 - COST) > 0
                    acc[hh]['b_tot'] += 1
                    acc[hh]['b_att'] += 1 if att else 0
                    acc[hh]['b_cap'] += 1 if cap else 0
                    acc[hh]['b_tep'] += 1 if tep else 0
                else:
                    ml = float(np.min(fwd_l))      # 触达用 low
                    mcl = float(np.min(fwd_c))     # 兑现/理论边用 close
                    att = ml <= p * (1 - X_dyn)
                    cap = (1.0 - mcl / p) >= eps_dyn
                    tep = (1.0 - mcl / p - COST) > 0
                    acc[hh]['s_tot'] += 1
                    acc[hh]['s_att'] += 1 if att else 0
                    acc[hh]['s_cap'] += 1 if cap else 0
                    acc[hh]['s_tep'] += 1 if tep else 0

            # —— Precision（前向 W_PREC 局部窗口，i+1 起=真极值必在信号之后，防空开天眼；剔除尾部）——
            # 修复②：窗口从 i+1 起（不含信号 bar 自身，消除 lag=0 自洽开天眼）；
            #         尾部不足 W_PREC 根的信号剔除（真极值尚未形成）。
            end = min(i + 1 + W_PREC, n)
            if (i + 1 >= end) or ((end - (i + 1)) < W_PREC):
                continue
            delta_eff = max(DELTA_ABS, C2 * atr / p) if p > 0 else DELTA_ABS
            if typ == 'B':
                win_low = lo[i + 1:end]
                j = int(np.argmin(win_low))
                lag = j + 1  # >= 1，真底在信号之后
                base = float(win_low[j])
                dev = (p - base) / base if base > 0 else 1.0
                prec['b_tot'] += 1
                prec['b_dev'].append(dev)
                if dev <= delta_eff:
                    prec['b_hit'] += 1
            else:
                win_high = h_[i + 1:end]
                j = int(np.argmax(win_high))
                lag = j + 1
                base = float(win_high[j])
                dev = (base - p) / base if base > 0 else 1.0
                prec['s_tot'] += 1
                prec['s_dev'].append(dev)
                if dev <= delta_eff:
                    prec['s_hit'] += 1

    return dict(sym=sym, days=n_ok, acc=acc, prec=prec)


def _rate(ok, tot):
    return (ok / tot) if tot else None


def main():
    global K, C1, C2, VOL_GATE, SIGNAL_GAP
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0, help='0=全部清洁池')
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--k', type=float, default=K, help='触达阈值 ATR 系数')
    ap.add_argument('--c1', type=float, default=C1, help='兑现阈值 ATR 系数')
    ap.add_argument('--c2', type=float, default=C2, help='邻近容差 ATR 系数')
    # 注意：W_PREC 无 --w-prec 入口，硬锁定为常量 30（业务硬约束，严禁优化）
    ap.add_argument('--vol-gate', action='store_true')
    ap.add_argument('--signal-gap', type=int, default=SIGNAL_GAP)
    ap.add_argument('--random-control', nargs='*', default=None, metavar='SYM',
                    help='[任务0.3] 对指定标的附加快档随机对照判决（空=watchlist 全部；M=200/m_rt=50/seed=42）；不带本 flag 时行为不变')
    a = ap.parse_args()
    K, C1, C2 = a.k, a.c1, a.c2
    VOL_GATE, SIGNAL_GAP = a.vol_gate, a.signal_gap

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    if a.max_syms:
        files = files[:a.max_syms]

    feishu(f'🚀 DET v2.0 三轴评估启动：{len(files)} 标的，'
           f'k={K}/c1={C1}/c2={C2}/W_PREC={W_PREC}，horizon={HORIZONS}')

    pool_acc = {h: dict(b_tot=0, b_att=0, b_cap=0, b_tep=0, s_tot=0, s_att=0, s_cap=0, s_tep=0)
                for h in HORIZONS}
    pool_prec = dict(b_tot=0, b_hit=0, b_dev=[], s_tot=0, s_hit=0, s_dev=[])
    sym_results = {}
    sym_rates = []   # 每标的 per-symbol rate（标的等权口径，评审修复⑤）
    n_syms = 0
    total_days = 0

    for idx, path in enumerate(files):
        sym = os.path.basename(path).replace('_1m.csv', '')
        r = evaluate_symbol(sym, path)
        if 'error' in r:
            continue
        n_syms += 1
        total_days += r['days']
        sym_results[sym] = {'days': r['days'], 'is_fund': is_fund(sym)}
        eod_r = r['acc']['EOD']
        sym_rates.append(dict(
            sym=sym,
            is_fund=is_fund(sym),
            att_b=_rate(eod_r['b_att'], eod_r['b_tot']), att_s=_rate(eod_r['s_att'], eod_r['s_tot']),
            cap_b=_rate(eod_r['b_cap'], eod_r['b_tot']), cap_s=_rate(eod_r['s_cap'], eod_r['s_tot']),
            tep_b=_rate(eod_r['b_tep'], eod_r['b_tot']), tep_s=_rate(eod_r['s_tep'], eod_r['s_tot']),
            prec_b=_rate(r['prec']['b_hit'], r['prec']['b_tot']),
            prec_s=_rate(r['prec']['s_hit'], r['prec']['s_tot']),
        ))
        for hh in HORIZONS:
            for kk in pool_acc[hh]:
                pool_acc[hh][kk] += r['acc'][hh][kk]
        for kk in pool_prec:
            if kk.endswith('_dev'):
                pool_prec[kk].extend(r['prec'][kk])
            else:
                pool_prec[kk] += r['prec'][kk]
        if (idx + 1) % 40 == 0:
            feishu(f'⏳ DET v2.0 进度 {idx+1}/{len(files)} 标的完成')

    def _block(acc_h):
        d = acc_h
        b_att = _rate(d['b_att'], d['b_tot'])
        s_att = _rate(d['s_att'], d['s_tot'])
        b_cap = _rate(d['b_cap'], d['b_tot'])
        s_cap = _rate(d['s_cap'], d['s_tot'])
        b_tep = _rate(d['b_tep'], d['b_tot'])
        s_tep = _rate(d['s_tep'], d['s_tot'])
        return dict(
            B_total=d['b_tot'], S_total=d['s_tot'],
            B_Attainment=b_att, S_Attainment=s_att,
            B_Capture=b_cap, S_Capture=s_cap,
            B_TEP=b_tep, S_TEP=s_tep,
        )

    def _prec_block(pp, side):
        tot = pp[f'{side}_tot']
        hit = pp[f'{side}_hit']
        devs = pp[f'{side}_dev']
        return dict(
            valid=tot, hit=hit, rate=_rate(hit, tot),
            dev_median=(float(np.median(devs)) if devs else None),
        )

    prec_b = _prec_block(pool_prec, 'b')
    prec_s = _prec_block(pool_prec, 's')
    # 综合分（纯乘法 1.0×1.0，to-EOD）：Precision × Capture
    eod = pool_acc['EOD']
    cap_b = _rate(eod['b_cap'], eod['b_tot'])
    cap_s = _rate(eod['s_cap'], eod['s_tot'])
    comp_b = (prec_b['rate'] * cap_b) if (prec_b['rate'] is not None and cap_b is not None) else None
    comp_s = (prec_s['rate'] * cap_s) if (prec_s['rate'] is not None and cap_s is not None) else None

    # 标的等权口径（评审修复⑤）：每标 rate 的简单平均，避免 ETF 长样本压过个股短样本
    def _sym_mean(key):
        vals = [s[key] for s in sym_rates if s[key] is not None]
        return float(np.mean(vals)) if vals else None

    sw_prec_b = _sym_mean('prec_b'); sw_prec_s = _sym_mean('prec_s')
    sw_cap_b = _sym_mean('cap_b'); sw_cap_s = _sym_mean('cap_s')
    symbol_weighted = dict(
        att_b=_sym_mean('att_b'), att_s=_sym_mean('att_s'),
        cap_b=sw_cap_b, cap_s=sw_cap_s,
        tep_b=_sym_mean('tep_b'), tep_s=_sym_mean('tep_s'),
        prec_b=sw_prec_b, prec_s=sw_prec_s,
        comp_b=(sw_prec_b * sw_cap_b) if (sw_prec_b is not None and sw_cap_b is not None) else None,
        comp_s=(sw_prec_s * sw_cap_s) if (sw_prec_s is not None and sw_cap_s is not None) else None,
        note='全池标的等权（仅作参考；对外主口径=分池标的等权，见 fund_pool/stock_pool）',
    )

    # —— 双轨制铁律：基金/个股分池（标的等权），禁止混算均值 ——
    fund_rates = [s for s in sym_rates if s.get('is_fund')]
    stock_rates = [s for s in sym_rates if not s.get('is_fund')]

    def _grp_block(grp):
        def m(key):
            vals = [s[key] for s in grp if s[key] is not None]
            return float(np.mean(vals)) if vals else None
        p_b, p_s = m('prec_b'), m('prec_s')
        c_b, c_s = m('cap_b'), m('cap_s')
        return dict(
            n_syms=len(grp),
            prec_b=p_b, prec_s=p_s, cap_b=c_b, cap_s=c_s,
            att_b=m('att_b'), att_s=m('att_s'),
            tep_b=m('tep_b'), tep_s=m('tep_s'),
            comp_b=(p_b * c_b) if (p_b is not None and c_b is not None) else None,
            comp_s=(p_s * c_s) if (p_s is not None and c_s is not None) else None,
            note='分池标的等权（对外主口径）',
        )

    fund_pool = _grp_block(fund_rates)
    stock_pool = _grp_block(stock_rates)

    pool = dict(
        n_syms=n_syms, total_days=total_days,
        n_fund=len(fund_rates), n_stock=len(stock_rates),
        horizons={str(h): _block(pool_acc[h]) for h in HORIZONS},
        precision=dict(B=prec_b, S=prec_s),
        composite=dict(B=comp_b, S=comp_s, note='Precision × Capture @to-EOD（信号等权）'),
        symbol_weighted=symbol_weighted,
        fund_pool=fund_pool,      # 【主口径】基金池标的等权
        stock_pool=stock_pool,    # 【主口径】个股池标的等权
        baseline=BASELINE,        # P2 基线（R4 分类别增益判据）
    )

    # —— [任务0.3/R1a 2026-09-28] 可选随机对照节：零假设一票否决（快档 M=200）——
    # 仅当显式带 --random-control 时运行；默认路径零改动（性能与产物结构不变）。
    random_control = None
    if a.random_control is not None:
        from random_control_validator import validate_symbol
        rc_syms = list(a.random_control)
        if not rc_syms:  # 空列表 = watchlist 全部
            try:
                with open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8') as f:
                    rc_syms = list(json.load(f).keys())
            except Exception:
                rc_syms = []
        random_control = {}
        for sym in rc_syms:
            try:
                r = validate_symbol(sym, days_req=60, m=200, m_rt=50, seed=42, verbose=False)
                random_control[sym] = {
                    k: r.get(k) for k in ('verdict', 'n_signals', 'n_days', 'n_trips', 'real', 'p', 'z', 'error')
                    if r.get(k) is not None
                }
            except Exception as e:
                random_control[sym] = {'error': f'{type(e).__name__}: {e}'}

    out = dict(
        meta=dict(
            framework='DET v2.0 (Attainment·Precision·Capture·TEP)',
            engine='general_signal.detect_signals_general (GT-1.0/v5)',
            vol_gate=VOL_GATE, data_dir=DATA_DIR, n_files=len(files),
            params=dict(HORIZONS=[str(h) for h in HORIZONS],
                        K=K, C1=C1, C2=C2, ATR_N=ATR_N, W_PREC=W_PREC,
                        X_ABS=X_ABS, EPS_ABS=EPS_ABS, DELTA_ABS=DELTA_ABS, COST=COST,
                        SIGNAL_GAP=SIGNAL_GAP),
        ),
        pool=pool,
        symbols=sym_results,
    )
    if random_control is not None:
        out['random_control'] = random_control

    fn = f'signal_validity_v2_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print(f'\n=== DET v2.0 池级（{n_syms} 标的 / {total_days} 交易日）===')
    print(f'  口径: k={K} c1={C1} c2={C2} W_PREC={W_PREC} COST={COST} | 主判据 to-EOD')
    for hh in HORIZONS:
        b = pool['horizons'][str(hh)]
        print(f'  H={hh:>3}: B 触达={_fmt(b["B_Attainment"])} 兑现={_fmt(b["B_Capture"])} TEP={_fmt(b["B_TEP"])}'
              f' | S 触达={_fmt(b["S_Attainment"])} 兑现={_fmt(b["S_Capture"])} TEP={_fmt(b["S_TEP"])}')
    print(f'  Precision(前向{W_PREC}根局部窗口): B 邻近={_fmt(prec_b["rate"])} (有效{prec_b["valid"]}, 中位偏差{_fmt(prec_b["dev_median"])})'
          f' | S 邻近={_fmt(prec_s["rate"])} (有效{prec_s["valid"]})')
    print(f'  综合分(to-EOD, Precision×Capture): B={_fmt(comp_b)}  S={_fmt(comp_s)}')
    print(f'  【双轨制分池·标的等权】')
    print(f'    基金({fund_pool["n_syms"]}只): Prec B={_fmt(fund_pool["prec_b"])} Cap B={_fmt(fund_pool["cap_b"])} Comp B={_fmt(fund_pool["comp_b"])}')
    print(f'    个股({stock_pool["n_syms"]}只): Prec B={_fmt(stock_pool["prec_b"])} Cap B={_fmt(stock_pool["cap_b"])} Comp B={_fmt(stock_pool["comp_b"])}')
    print(f'  P2基线: 基金 Prec79.3%/Cap69.6% | 个股 Prec53.1%/Cap73.5%')
    if random_control:
        print(f'\n=== 随机对照（零假设一票否决，快档 M=200）===')
        for sym, r in random_control.items():
            if 'error' in r:
                print(f'  {sym}: ERROR {r["error"]}')
                continue
            _zw = (r.get('z') or {}).get('net_wr')
            _pw = (r.get('p') or {}).get('net_wr')
            _nw = (r.get('real') or {}).get('net_wr')
            print(f'  {sym}: verdict={r.get("verdict")} n_sig={r.get("n_signals")} '
                  f'net_wr={_fmt(_nw)} p_netwr={_pw} z_netwr={None if _zw is None else round(_zw, 2)}')
    print(f'JSON -> {fp}')

    feishu(f'✅ DET v2.0 三轴评估完成（{n_syms}标/{total_days}日，双轨制分池）：\n'
           f'基金({fund_pool["n_syms"]}只) Prec={_fmt(fund_pool["prec_b"])}/Cap={_fmt(fund_pool["cap_b"])}/Comp={_fmt(fund_pool["comp_b"])}；\n'
           f'个股({stock_pool["n_syms"]}只) Prec={_fmt(stock_pool["prec_b"])}/Cap={_fmt(stock_pool["cap_b"])}/Comp={_fmt(stock_pool["comp_b"])}\n'
           f'产物 output/{fn}')

    if random_control:
        _rc_txt = '；'.join(f"{s}:{r.get('verdict', 'ERR')}" for s, r in random_control.items())
        feishu(f'🎲 随机对照（零假设，快档 M=200）: {_rc_txt}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 DET v2.0 异常中断: {e}')
        raise
