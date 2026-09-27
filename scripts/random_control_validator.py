# -*- coding: utf-8 -*-
"""
random_control_validator.py —— 随机入场蒙特卡洛对照验证器（点位优越性一票否决组件）

回答 DET v2.0 三轴绝对指标从未回答的问题：「信号点位是否优于同时段随机入场」。

零假设 H0：信号不比随机入场好。
对每个 (sym, date)，真实信号集 = detect_signals_general 当日输出（与 v2 相同输入构造：
pc=前一交易日收盘，signal_gap=6，ATR_N=20 无未来函数）。每个蒙特卡洛样本在**同时段
同标的同日**的可交易域内抽伪信号集（数量 1:1 分侧匹配、三段时段分层、同侧间隔
≥SIGNAL_GAP），在完全相同口径下计算指标，构造零假设分布。

指标（真实 vs 每个伪样本，同口径）：
  A. 单笔前向：TEP（B: max(fwd_high[1:W_PREC+1])/p-1-COST>0；S 对称）；
     Capture@EOD（B: max(fwd_close)/p-1 >= c1*ATR_n/p；S 对称）。
  B. round-trip（simulate_position_sm，生产 EXIT_CFG 语义：无止损/无时间止损/
     移动止损 0.4/0.6 + S信号出场）：净胜率 net_wr、平均每笔净收益 mean_net。
     性能口径：M 个伪样本全量算单笔指标；round-trip 仅在前 --m-rt（默认100）个
     伪样本子集上计算（JSON 中注明子集口径）。

判定（一票否决，跨日 pooling）：
  p = 伪样本指标 >= 真实指标的比例（单侧）；z = (real - mean_rand)/std_rand。
  PASS = ((p_netwr < 0.05 且 z_netwr >= 1.0) 或 (p_meannet < 0.05 且 z_meannet >= 1.0))
         且 n_signals >= 30。
  n_signals < 30 → INSUFFICIENT_SAMPLE（只观察不判定）；其余 FAIL。

自测试（--self-test）：把真实信号替换为一组按同样规则抽的随机信号作为"待验对象"，
跑完整 M 样本对照，verdict 必须 != PASS；若 PASS 说明验证器分辨率为零，
退出码 2 并打印 SELF-TEST-FAILED。

用法：
  python scripts/random_control_validator.py --sym 300010.SZ --days 60 --m 500
  python scripts/random_control_validator.py --sym 300010.SZ --days 60 --m 500 --self-test
  python scripts/random_control_validator.py --all --days 60 --m 500

产物：output/random_control_<sym>_<date>.json（utf-8, ensure_ascii=False）+ stdout 摘要。
随机种子固定（--seed 默认 42，可复现）。
"""
import argparse
import csv
import datetime
import json
import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT          # noqa: E402
from daily_signal_review import build_data                                  # noqa: E402
from exit_manager import make_config, cost_for_symbol, limit_thr            # noqa: E402
from simulate_position_sm import simulate_position_sm                       # noqa: E402
# 复用 v2 的无未来函数 ATR 与标的类型识别（口径同源铁律）
from evaluate_signal_validity_v2 import _atr_n, is_fund                     # noqa: E402

DATA_DIRS = [r'F:/keyfactor_data/1m_clean', r'F:/keyfactor_data/1m']  # 优先级从高到低
OUT = os.path.join(ROOT, 'output')
MON_CFG = os.path.join(ROOT, 'data', 'monitor_config.json')
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')

# —— 口径参数（与 DET v2.0 同源）——
C1 = 1.5              # 兑现阈值 ATR 系数（v2 同值）
EPS_ABS = 0.005       # 兑现阈值绝对口径兜底（atr<=0 时）
ATR_N = 20            # ATR 回看根数（信号前，无未来函数）
W_PREC = 30           # 前向窗口（业务硬约束，锁定 30）
COST = 0.0005         # 单边成本（TEP 口径）
SIGNAL_GAP = 6        # 同侧信号最小间隔（与 v2 评估口径一致）

P_PASS = 0.05         # 单侧 p 阈值
Z_PASS = 1.0          # z 阈值
N_MIN_SIGNALS = 30    # 最小信号数（不足 → INSUFFICIENT_SAMPLE）

SEG_NAMES = {0: '早盘(09:31-10:30)', 1: '盘中(10:31-14:29)', 2: '尾盘(14:30-15:00)'}


# ----------------------------------------------------------------------------- #
# 数据加载（保留逐 bar trade_time 用于时段分层；数组口径同 v2 load_days）
# ----------------------------------------------------------------------------- #
def find_data_path(sym):
    for d in DATA_DIRS:
        p = os.path.join(d, f'{sym}_1m.csv')
        if os.path.exists(p):
            return p
    return None


def load_days_full(path):
    """返回 (days, name)：days[date] = dict(o,h,lo,c,v,times)，按 trade_time 升序。"""
    rows = {}
    name = ''
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if not name:
                name = row.get('name', '') or ''
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        days[d] = dict(
            o=np.array([float(x['open']) for x in rs]),
            h=np.array([float(x['high']) for x in rs]),
            lo=np.array([float(x['low']) for x in rs]),
            c=np.array([float(x['close']) for x in rs]),
            v=np.array([float(x['volume']) for x in rs]),
            times=[x['trade_time'] for x in rs],
        )
    return days, name


# ----------------------------------------------------------------------------- #
# 向量化预计算（语义与 v2._atr_n / 前向窗口完全一致，逐 bar 一次性算好，
# 之后真实/伪信号评估只是数组查表 —— M×days 蒙特卡洛的性能关键）
# ----------------------------------------------------------------------------- #
def atr_arr(h, lo, c, n=ATR_N):
    """atr[i] = mean(TR[max(1,i-n) .. i-1])，与 v2._atr_n 逐点等价（无未来函数）。"""
    N = len(c)
    pc = np.empty(N)
    pc[0] = c[0]
    pc[1:] = c[:-1]
    tr = np.maximum(h - lo, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
    cs = np.concatenate(([0.0], np.cumsum(tr)))
    idx = np.arange(N)
    j0 = np.maximum(1, idx - n)
    cnt = idx - j0
    atr = np.zeros(N)
    m = cnt > 0
    atr[m] = (cs[idx[m]] - cs[j0[m]]) / cnt[m]
    return atr


def forward_arrays(h, lo, c, w=W_PREC):
    """fwd_max_high[i] = max(h[i+1 : i+1+w])；fwd_min_low 对称；
    fwd_max_close_eod[i] = max(c[i+1:])（to-EOD）；fwd_min_close_eod 对称。"""
    N = len(c)
    ext_h = np.concatenate([h, np.full(w, -np.inf)])
    ext_l = np.concatenate([lo, np.full(w, np.inf)])
    sw_h = np.lib.stride_tricks.sliding_window_view(ext_h, w)
    sw_l = np.lib.stride_tricks.sliding_window_view(ext_l, w)
    fwd_max_high = sw_h[1:N + 1].max(axis=1)
    fwd_min_low = sw_l[1:N + 1].min(axis=1)
    rev_cmax = np.maximum.accumulate(c[::-1])[::-1]
    rev_cmin = np.minimum.accumulate(c[::-1])[::-1]
    fwd_max_close_eod = np.full(N, np.nan)
    fwd_min_close_eod = np.full(N, np.nan)
    fwd_max_close_eod[:N - 1] = rev_cmax[1:]
    fwd_min_close_eod[:N - 1] = rev_cmin[1:]
    return fwd_max_high, fwd_min_low, fwd_max_close_eod, fwd_min_close_eod


def seg_array(times):
    """逐 bar 时段分层：0=早盘(<=10:30) 1=盘中(10:31-14:29) 2=尾盘(>=14:30)。"""
    segs = []
    for t in times:
        hhmm = str(t)[-8:]
        try:
            minutes = int(hhmm[:2]) * 60 + int(hhmm[3:5])
        except (ValueError, IndexError):
            minutes = -1
        if minutes <= 10 * 60 + 30:
            segs.append(0)
        elif minutes <= 14 * 60 + 29:
            segs.append(1)
        else:
            segs.append(2)
    return np.array(segs, dtype=int)


def limit_thr_for(sym, name=''):
    """一字板判定的涨跌停阈值：主板 ST 5%；其余按 exit_manager.limit_thr 代码前缀口径
    （创业板/科创板 20%、主板 10%、北交所 30%。300010 虽 ST 但创业板注册制口径仍 20%，
    与 monitor_config.json 注释一致）。"""
    code = sym.split('.')[0]
    if 'ST' in (name or '').upper() and code.startswith(('60', '00')):
        return 0.05
    return limit_thr(sym)


def tradable_domain(sym, name, h, lo, c, v, pc):
    """可交易域 bool 数组：idx∈[2, n-W_PREC]；剔除 volume==0 与一字板
    （high==low 且收于涨/跌停价，基于前收 pc 与代码前缀阈值）。"""
    N = len(c)
    idx = np.arange(N)
    thr = limit_thr_for(sym, name)
    if pc and pc > 0:
        up = round(pc * (1 + thr), 2)
        dn = round(pc * (1 - thr), 2)
        yizi = (h == lo) & ((c >= up - 1e-9) | (c <= dn + 1e-9))
    else:
        yizi = np.zeros(N, dtype=bool)
    return (idx >= 2) & (idx <= N - W_PREC) & (v > 0) & (~yizi) & (c > 0)


# ----------------------------------------------------------------------------- #
# 伪信号抽样（零假设构造）
# ----------------------------------------------------------------------------- #
def sample_pseudo_sigs(rng, domain_mask, segs, c, quotas, gap=SIGNAL_GAP, max_retry=50):
    """按 (side, seg) 配额在可交易域内抽伪信号，同侧索引差 >= gap（重抽至满足，
    max_retry 次后放宽间隔约束并标记 relaxed）。

    quotas: {(side, seg): k}。返回 (sigs, status)：status='ok'/'relaxed'/'insufficient_domain'。
    """
    dom = np.where(domain_mask)[0]
    # 任一配额段候选不足 → 直接 insufficient（不浪费重抽）
    for (side, seg), k in quotas.items():
        if k > 0 and int(((segs[dom] == seg)).sum()) < k:
            return [], 'insufficient_domain'
    sigs = []
    status = 'ok'
    for side in ('B', 'S'):
        quota = {seg: quotas.get((side, seg), 0) for seg in (0, 1, 2)}
        need = sum(quota.values())
        if need == 0:
            continue
        picked = None
        for _ in range(max_retry):
            order = rng.permutation(dom)
            rem = dict(quota)
            sel = []
            for i in order:
                s = int(segs[i])
                if rem.get(s, 0) <= 0:
                    continue
                if all(abs(int(i) - j) >= gap for j in sel):
                    sel.append(int(i))
                    rem[s] -= 1
            if all(v == 0 for v in rem.values()):
                picked = sel
                break
        if picked is None:  # 放宽间隔约束（保留时段分层与数量匹配）
            status = 'relaxed'
            order = rng.permutation(dom)
            rem = dict(quota)
            sel = []
            for i in order:
                s = int(segs[i])
                if rem.get(s, 0) <= 0:
                    continue
                if all(abs(int(i) - j) >= 1 for j in sel):
                    sel.append(int(i))
                    rem[s] -= 1
            if not all(v == 0 for v in rem.values()):
                return [], 'insufficient_domain'
            picked = sel
        for i in sorted(picked):
            sigs.append({'type': side, 'idx': i, 'price': round(float(c[i]), 2),
                         'reason': 'rand'})
    sigs.sort(key=lambda s: s['idx'])
    return sigs, status


def quotas_from_real(real_sigs, segs):
    """真实信号逐侧逐段数量 → 伪信号配额（1:1 分侧分时段匹配）。"""
    q = {}
    for s in real_sigs:
        key = (s['type'], int(segs[s['idx']]))
        q[key] = q.get(key, 0) + 1
    return q


# ----------------------------------------------------------------------------- #
# 指标计算（真实/伪同口径）
# ----------------------------------------------------------------------------- #
def bar_metrics(sigs_by_day, pre_by_day):
    """单笔前向指标（向量化查表）。返回 (tep_rate, capture_rate, n)。
    TEP: B: max(fwd_high[1:W_PREC+1])/p-1-COST > 0；S: 1-min(fwd_low)/p-COST > 0。
    Capture@EOD: B: max(fwd_close)/p-1 >= c1*ATR_n/p；S: 1-min(fwd_close)/p >= eps。
    """
    tep_flags = []
    cap_flags = []
    for date, sigs in sigs_by_day:
        pre = pre_by_day[date]
        atr = pre['atr']
        fmh, fml = pre['fwd_max_high'], pre['fwd_min_low']
        fmc, fmic = pre['fwd_max_close_eod'], pre['fwd_min_close_eod']
        for s in sigs:
            i = s['idx']
            p = float(s['price'])
            if p <= 0:
                continue
            a = float(atr[i])
            eps = (C1 * a / p) if a > 0 else EPS_ABS
            if s['type'] == 'B':
                tep_flags.append(fmh[i] / p - 1.0 - COST > 0)
                cap_flags.append(fmc[i] / p - 1.0 >= eps)
            else:
                tep_flags.append(1.0 - fml[i] / p - COST > 0)
                cap_flags.append(1.0 - fmic[i] / p >= eps)
    n = len(tep_flags)
    if n == 0:
        return None, None, 0
    return float(np.mean(tep_flags)), float(np.mean(cap_flags)), n


def roundtrip_metrics(sigs_by_day, prices_by_day, cfg, cost, has_base):
    """round-trip 口径：simulate_position_sm → (net_wr, mean_net, n_trips)。"""
    sm = simulate_position_sm(sigs_by_day, prices_by_day,
                              config_long=cfg, config_short=cfg,
                              cost=cost, has_base=has_base)
    trips = sm['trips']
    if not trips:
        return None, None, 0
    rets = np.array([float(t['ret_pct']) for t in trips])
    return float((rets > 0).mean()), float(rets.mean()), len(trips)


def has_base_of(sym):
    """monitor_config.json per_symbol[sym].has_base（兼容顶层 sym 键布局），缺省 True。"""
    try:
        with open(MON_CFG, encoding='utf-8') as f:
            cfg = json.load(f)
        ent = (cfg.get('per_symbol') or {}).get(sym)
        if ent is None:
            ent = cfg.get(sym)
        if isinstance(ent, dict) and 'has_base' in ent:
            return bool(ent['has_base'])
    except Exception:
        pass
    return True


# ----------------------------------------------------------------------------- #
# 单标的验证主流程
# ----------------------------------------------------------------------------- #
def validate_symbol(sym, days_req, m, m_rt, seed, self_test=False, verbose=True,
                    min_hist_diff=None, cfg_overrides=None):
    path = find_data_path(sym)
    if path is None:
        return {'sym': sym, 'error': f'no_data({DATA_DIRS})'}
    days_all, name = load_days_full(path)
    dates_all = sorted(days_all.keys())

    # —— 全历史推进 prev_close 链（与 v2 同口径），仅在最近 days_req 日做评估 ——
    recs = []
    prev_close = None
    for d in dates_all:
        dd = days_all[d]
        c = dd['c']
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': dd['o'], 'high': dd['h'], 'low': dd['lo'],
                           'close': c, 'volume': dd['v'],
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        prev_close = c[-1]
        if data is None:
            continue
        _cfg = replace(GENERAL_DEFAULT, signal_gap=SIGNAL_GAP)
        if min_hist_diff is not None:
            _cfg = replace(_cfg, min_hist_diff=float(min_hist_diff))
        for _k, _v in (cfg_overrides or {}).items():
            if hasattr(_cfg, _k):
                _cfg = replace(_cfg, **{_k: _v})
        sigs = detect_signals_general(data, pc, _cfg)
        recs.append(dict(date=d, pc=float(pc), sigs=sigs, data=data,
                         o=dd['o'], h=dd['h'], lo=dd['lo'], c=c, v=dd['v'],
                         times=dd['times']))
    recs = recs[-days_req:] if days_req > 0 else recs
    if not recs:
        return {'sym': sym, 'error': 'no_valid_days'}

    # —— 逐日预计算（向量化）——
    pre_by_day = {}
    prices_by_day = []
    day_detail = []
    n_real = 0
    for k, rec in enumerate(recs):
        d = rec['date']
        h, lo, c, v = rec['h'], rec['lo'], rec['c'], rec['v']
        atr = atr_arr(h, lo, c, ATR_N)
        fmh, fml, fmc, fmic = forward_arrays(h, lo, c, W_PREC)
        segs = seg_array(rec['times'])
        dom = tradable_domain(sym, name, h, lo, c, v, rec['pc'])
        pre_by_day[d] = dict(atr=atr, fwd_max_high=fmh, fwd_min_low=fml,
                             fwd_max_close_eod=fmc, fwd_min_close_eod=fmic,
                             segs=segs, dom=dom)
        data = rec['data']
        prices_by_day.append((d, {'o': data['o'], 'h': data['h'], 'lo': data['lo'],
                                  'c': data['c'], 'atr': data['atr'],
                                  'trend': data.get('trend'), 'n': data['n'],
                                  'pc': rec['pc'], 'sym': sym, 'date': d}))
        quotas = quotas_from_real(rec['sigs'], segs)
        rec['quotas'] = quotas
        n_real += len(rec['sigs'])
        day_detail.append(dict(
            date=d, n_B=sum(1 for s in rec['sigs'] if s['type'] == 'B'),
            n_S=sum(1 for s in rec['sigs'] if s['type'] == 'S'),
            domain_size=int(dom.sum()), pc=round(rec['pc'], 3),
            quotas={f'{side}{seg}': k for (side, seg), k in sorted(quotas.items())},
        ))
        if verbose and (k + 1) % 10 == 0:
            print(f'  [心跳] {sym} 预处理 {k + 1}/{len(recs)} 日', flush=True)

    sigs_real_by_day = [(rec['date'], rec['sigs']) for rec in recs]

    # —— 出场配置（生产 EXIT_CFG 语义）与成本、底仓 ——
    cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                      trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)
    cost = cost_for_symbol(sym)
    hb = has_base_of(sym)

    # —— 真实指标（self-test 时替换为一组按同规则抽的随机信号作"待验对象"）——
    if self_test:
        rng_st = np.random.default_rng(seed * 1000003 + 777)
        st_sigs_by_day = []
        for rec in recs:
            d = rec['date']
            pre = pre_by_day[d]
            ss, _st = sample_pseudo_sigs(rng_st, pre['dom'], pre['segs'],
                                         rec['c'], rec['quotas'])
            st_sigs_by_day.append((d, ss))
        subject_sigs_by_day = st_sigs_by_day
    else:
        subject_sigs_by_day = sigs_real_by_day

    real_tep, real_cap, n_bar = bar_metrics(subject_sigs_by_day, pre_by_day)
    real_netwr, real_meannet, n_trips = roundtrip_metrics(
        subject_sigs_by_day, prices_by_day, cfg, cost, hb)
    n_signals = sum(len(s) for _, s in subject_sigs_by_day)

    # —— 蒙特卡洛零假设分布 ——
    rand_tep = np.full(m, np.nan)
    rand_cap = np.full(m, np.nan)
    rand_netwr = np.full(min(m_rt, m), np.nan)
    rand_meannet = np.full(min(m_rt, m), np.nan)
    n_insufficient = 0
    n_relaxed = 0
    for b in range(m):
        rng = np.random.default_rng(seed * 1000003 + b)
        pseudo_by_day = []
        skip_sample = False
        for rec in recs:
            d = rec['date']
            if not rec['sigs']:
                pseudo_by_day.append((d, []))
                continue
            pre = pre_by_day[d]
            ps, status = sample_pseudo_sigs(rng, pre['dom'], pre['segs'],
                                            rec['c'], rec['quotas'])
            if status == 'insufficient_domain':
                # 该日记 insufficient_domain 跳过（本样本该日为空集）
                n_insufficient += 1
                pseudo_by_day.append((d, []))
                continue
            if status == 'relaxed':
                n_relaxed += 1
            pseudo_by_day.append((d, ps))
        t, cp, _ = bar_metrics(pseudo_by_day, pre_by_day)
        rand_tep[b] = t if t is not None else np.nan
        rand_cap[b] = cp if cp is not None else np.nan
        if b < m_rt:
            wr, mn, _ = roundtrip_metrics(pseudo_by_day, prices_by_day, cfg, cost, hb)
            rand_netwr[b] = wr if wr is not None else np.nan
            rand_meannet[b] = mn if mn is not None else np.nan
        if verbose and (b + 1) % 100 == 0:
            print(f'  [心跳] {sym} 蒙特卡洛 {b + 1}/{m}', flush=True)

    def _pz(real, dist):
        dist = dist[~np.isnan(dist)]
        if real is None or len(dist) == 0:
            return None, None, {}
        p = float((dist >= real).mean())
        mu = float(dist.mean())
        sd = float(dist.std(ddof=1)) if len(dist) > 1 else 0.0
        if sd > 0:
            z = (real - mu) / sd
        else:
            z = 0.0 if real == mu else (1e9 if real > mu else -1e9)
        qs = np.nanpercentile(dist, [5, 50, 95])
        return float(p), float(z), dict(p5=float(qs[0]), p50=float(qs[1]),
                                        p95=float(qs[2]), mean=mu, std=sd,
                                        n=len(dist))

    p_tep, z_tep, q_tep = _pz(real_tep, rand_tep)
    p_cap, z_cap, q_cap = _pz(real_cap, rand_cap)
    p_wr, z_wr, q_wr = _pz(real_netwr, rand_netwr)
    p_mn, z_mn, q_mn = _pz(real_meannet, rand_meannet)

    # —— 判定（一票否决）——
    if n_signals < N_MIN_SIGNALS:
        verdict = 'INSUFFICIENT_SAMPLE'
    else:
        ok_wr = (p_wr is not None and p_wr < P_PASS and z_wr is not None and z_wr >= Z_PASS)
        ok_mn = (p_mn is not None and p_mn < P_PASS and z_mn is not None and z_mn >= Z_PASS)
        verdict = 'PASS' if (ok_wr or ok_mn) else 'FAIL'

    out = dict(
        meta=dict(
            sym=sym, name=name, is_fund=is_fund(sym), data_path=path,
            generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            seed=seed, m=m, m_rt=min(m_rt, m),
            rt_subset_note='round-trip 指标仅在前 m_rt 个伪样本子集上计算（性能口径）；'
                           'TEP/Capture 为全 M 样本',
            days_requested=days_req,
            params=dict(C1=C1, EPS_ABS=EPS_ABS, ATR_N=ATR_N, W_PREC=W_PREC,
                        COST=COST, SIGNAL_GAP=SIGNAL_GAP,
                        P_PASS=P_PASS, Z_PASS=Z_PASS, N_MIN_SIGNALS=N_MIN_SIGNALS),
            exit_cfg=cfg, cost=cost, has_base=hb,
            engine='general_signal.detect_signals_general (GT-1.0/v5, 与 v2 同输入构造)',
            units='net_wr/tep/capture=比例(0-1); mean_net=百分点/笔(与 simulate_position_sm '
                  'ret_pct 同单位); p/z 为单侧(越大越好)',
            self_test=self_test,
        ),
        verdict=verdict,
        n_signals=n_signals, n_days=len(recs),
        n_days_with_signals=sum(1 for r in recs if r['sigs']),
        n_trips=n_trips,
        real=dict(net_wr=real_netwr, mean_net=real_meannet,
                  tep=real_tep, capture=real_cap),
        random_dist=dict(net_wr=q_wr, mean_net=q_mn, tep=q_tep, capture=q_cap),
        p=dict(net_wr=p_wr, mean_net=p_mn, tep=p_tep, capture=p_cap),
        z=dict(net_wr=z_wr, mean_net=z_mn, tep=z_tep, capture=z_cap),
        sampling=dict(insufficient_domain_events=n_insufficient,
                      relaxed_gap_events=n_relaxed),
        days=day_detail,
    )
    return out


# ----------------------------------------------------------------------------- #
# 摘要打印 / 落盘
# ----------------------------------------------------------------------------- #
def _f(x, unit='rate'):
    """unit='rate': 比例(0-1) → 百分比；unit='pp': 百分点(ret_pct 单位) → 原值带 pp。"""
    if x is None:
        return '   --   '
    if unit == 'pp':
        return f'{x:+6.3f}pp'
    return f'{x * 100:6.2f}%'


def print_summary(r):
    if 'error' in r:
        print(f'== {r["sym"]}: ERROR {r["error"]}')
        return
    m_ = r['meta']
    print(f'\n=== 随机对照验证 {m_["sym"]}({m_.get("name", "")}) '
          f'{"[SELF-TEST] " if m_["self_test"] else ""}'
          f'| {r["n_days"]}日(有信号{r["n_days_with_signals"]}) '
          f'n_signals={r["n_signals"]} n_trips={r["n_trips"]} '
          f'M={m_["m"]}(rt子集{m_["m_rt"]}) seed={m_["seed"]} ===')
    print(f'  has_base={m_["has_base"]} cost={m_["cost"]} 数据={m_["data_path"]}')
    hdr = f'  {"指标":<10} {"真实":>8} {"rand_p5":>8} {"rand_p50":>8} {"rand_p95":>8} {"p":>7} {"z":>7}'
    print(hdr)
    for key, unit in (('net_wr', 'rate'), ('mean_net', 'pp'),
                      ('tep', 'rate'), ('capture', 'rate')):
        q = r['random_dist'][key]
        print(f'  {key:<10} {_f(r["real"][key], unit)} '
              f'{_f(q.get("p5"), unit)} {_f(q.get("p50"), unit)} {_f(q.get("p95"), unit)} '
              f'{r["p"][key] if r["p"][key] is not None else float("nan"):7.4f} '
              f'{r["z"][key] if r["z"][key] is not None else float("nan"):7.2f}')
    print('  (单位: net_wr/tep/capture=比例; mean_net=百分点/笔, 与 ret_pct 同单位)')
    print(f'  >>> verdict = {r["verdict"]}'
          f'  (判据: (p_netwr<{P_PASS}且z>={Z_PASS}) 或 (p_meannet<{P_PASS}且z>={Z_PASS}),'
          f' n_signals>={N_MIN_SIGNALS})')
    if r['sampling']['insufficient_domain_events'] or r['sampling']['relaxed_gap_events']:
        print(f'  抽样事件: insufficient_domain={r["sampling"]["insufficient_domain_events"]}'
              f' relaxed_gap={r["sampling"]["relaxed_gap_events"]}')


def save_json(r, self_test=False, tag=''):
    if 'error' in r:
        return None
    os.makedirs(OUT, exist_ok=True)
    sym = r['meta']['sym'].replace('.', '')
    today = datetime.date.today().strftime('%Y%m%d')
    suffix = '_selftest' if self_test else ''
    if tag:
        suffix += '_' + tag
    fp = os.path.join(OUT, f'random_control_{sym}_{today}{suffix}.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    return fp


def main():
    ap = argparse.ArgumentParser(description='随机入场蒙特卡洛对照验证器（点位优越性一票否决）')
    ap.add_argument('--sym', default='')
    ap.add_argument('--all', action='store_true', help='watchlist 全部标的')
    ap.add_argument('--days', type=int, default=60, help='最近 N 个交易日')
    ap.add_argument('--m', type=int, default=500, help='蒙特卡洛样本数')
    ap.add_argument('--m-rt', type=int, default=100,
                    help='round-trip 指标伪样本子集大小（性能口径）')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--self-test', action='store_true',
                    help='注入实验：真实信号替换为纯随机信号，verdict 必须 != PASS')
    ap.add_argument('--min-hist-diff', type=float, default=None,
                    help='MACD 背离强度门槛 A/B（任务1.1；None=生产默认 0.0）')
    ap.add_argument('--set', dest='cfg_set', action='append', default=[],
                    help='GT 参数覆盖（可重复）：--set w_vol_div=0.4 --set buy_threshold=0.5')
    ap.add_argument('--tag', default='',
                    help='产物文件名附加标签（A/B 对照用，如 mhd015）')
    a = ap.parse_args()

    # 解析 --set key=value（bool/int/float 自动转型）
    def _parse_val(s):
        if s.lower() in ('true', 'false'):
            return s.lower() == 'true'
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return s
    cfg_overrides = {}
    for kv in a.cfg_set:
        if '=' not in kv:
            ap.error(f'--set 格式错误（需 key=value）: {kv}')
        k, v = kv.split('=', 1)
        cfg_overrides[k.strip()] = _parse_val(v.strip())

    if a.all:
        with open(WATCHLIST, encoding='utf-8') as f:
            syms = list(json.load(f).keys())
    elif a.sym:
        syms = [s.strip() for s in a.sym.split(',') if s.strip()]
    else:
        ap.error('必须指定 --sym 或 --all')

    t0 = datetime.datetime.now()
    exit_code = 0
    for sym in syms:
        r = validate_symbol(sym, a.days, a.m, a.m_rt, a.seed,
                            self_test=a.self_test, min_hist_diff=a.min_hist_diff,
                            cfg_overrides=cfg_overrides)
        if a.min_hist_diff is not None:
            r.setdefault('meta', {})['min_hist_diff'] = a.min_hist_diff
        if cfg_overrides:
            r.setdefault('meta', {})['cfg_overrides'] = cfg_overrides
        print_summary(r)
        fp = save_json(r, self_test=a.self_test, tag=a.tag)
        if fp:
            print(f'  JSON -> {fp}')
        if a.self_test and r.get('verdict') == 'PASS':
            print('SELF-TEST-FAILED: 随机信号被判 PASS —— 验证器分辨率为零，工具不可信')
            exit_code = 2
    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f'\n总耗时 {dt:.1f}s')
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
