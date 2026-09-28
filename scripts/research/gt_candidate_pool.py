#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""池级 GT 候选合规重跑（RSI Round 2；2026-09-29 夜）

目的：把 5 个 AI 判断队列候选（buy_threshold=0.55 / w_vwap=1.4 / w_macd_div=0.7 /
w_vol_div=0.4 / w_rsi=1.0）从「单标的 11 日 samebar n=23~29」升级为
「池级（tune_pool_40）× 60 日 × exec_delay_bars=1」的合规证据，供参数层
daily_agent.apply 门槛（z≥1.0 且 Δ≥+2.0pp 且 n≥30）判定；同时顺带做
G2 regime_gate 样本增厚复检（ON/OFF paired）。

口径纪律（docs/rsi_loop_agenda.md 纪律 7）：
  同一套伪样本服务全部候选臂（paired）——伪样本按【基线信号】的 segs/quotas 抽一次，
  全部候选复用；真/伪臂 exec_delay 必须一致（本脚本统一 =1）。

判定（对齐 gates.py / exit_anchor_grid）：
  z≥1.0 且 p<0.05 且 n_trips≥30 且 Δnet_wr ≥ +2.0pp 且 mean_net 不恶化 ⇒ PASS_GATE

产物：output/gt_candidate_pool_<date>.json（含每候选 pooled 统计 + 伪样本分布摘要）
长跑三件套：信号免疫 + beat/feishu + DONE_FLAG。
"""
import os
import sys
import json
import argparse
import datetime
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, os.path.join(ROOT, 'scripts', 'research'))

import exit_anchor_grid as G                                       # noqa: E402
from general_signal import detect_signals_general, GENERAL_DEFAULT  # noqa: E402
from simulate_position_sm import simulate_position_sm              # noqa: E402

POOL_FILE = os.path.join(ROOT, 'data', 'tune_pool_40.json')
OUT = os.path.join(ROOT, 'output')

for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        import signal as _sig
        _sig.signal(getattr(_sig, _s), _sig.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

TASK_TAG = "gt_candidate_pool"
HEARTBEAT = os.path.join(OUT, f"{TASK_TAG}_heartbeat.txt")
DONE_FLAG = os.path.join(OUT, f"{TASK_TAG}_done.flag")
NOTIFY = r"C:/Users/YZP/.workbuddy/notify.py"

# 候选臂：基线 + AI 判断队列 5 候选 + regime_gate A/B（G2 增厚）
CANDIDATES = {
    'A0_baseline':                    {},
    'C_buy_threshold_0.55':           {'buy_threshold': 0.55},
    'C_w_vwap_1.4':                   {'w_vwap': 1.4},
    'C_w_macd_div_0.7':               {'w_macd_div': 0.7},
    'C_w_vol_div_0.4':                {'w_vol_div': 0.4},
    'C_w_rsi_1.0':                    {'w_rsi': 1.0},
    'G_regime_gate_ON':               {'regime_gate': True},
    'G_regime_gate_OFF':              {'regime_gate': False},
}
BASELINE = 'A0_baseline'

Z_MIN = 1.0
P_MAX = 0.05
N_MIN = 30
DELTA_WR_PP = 2.0


def beat(msg):
    try:
        with open(HEARTBEAT, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass
    print(f"{datetime.datetime.now():%H:%M:%S} {msg}", flush=True)


def feishu(text):
    try:
        import subprocess
        subprocess.Popen([sys.executable, NOTIFY, text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_pool(path):
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    pool = raw.get('pool') if isinstance(raw, dict) else raw
    syms = []
    for ent in pool:
        s = ent.get('symbol') if isinstance(ent, dict) else ent
        if s:
            syms.append(s)
    return syms


def eval_pool(syms, days, m, seed, exec_delay, hb_every=5):
    """返回 {cand: {'real_rets': [...], 'rand_rets': [[...]...]}}，严格 paired。"""
    acc = {c: {'real': [], 'rand': [[] for _ in range(m)]} for c in CANDIDATES}
    errors = {}
    base_cfg = GENERAL_DEFAULT
    for i, sym in enumerate(syms, 1):
        try:
            recs, name, err = G.detect_recs(sym, days)
            if err:
                errors[sym] = err
                beat(f"[{i}/{len(syms)}] {sym} 跳过：{err}")
                continue
            if not recs:
                errors[sym] = 'no_valid_days'
                beat(f"[{i}/{len(syms)}] {sym} 跳过：no_valid_days")
                continue

            # —— 价格面 + 伪样本配额（按基线信号抽一次，全部候选共享）——
            prices_by_day = []
            segs_by_day = {}
            quotas_by_day = {}
            for rec in recs:
                d = rec['date']
                segs = G.seg_array(rec['times'])
                dom = G.tradable_domain(sym, name, rec['h'], rec['lo'],
                                        rec['c'], rec['v'], rec['pc'])
                segs_by_day[d] = segs
                quotas_by_day[d] = G.quotas_from_real(rec['sigs'], segs)
                data = rec['data']
                prices_by_day.append((d, {'o': data['o'], 'h': data['h'],
                                          'lo': data['lo'], 'c': data['c'],
                                          'atr': data['atr'], 'trend': data.get('trend'),
                                          'n': data['n'], 'pc': rec['pc'],
                                          'sym': sym, 'date': d}))

            pseudo_sets = []
            for b in range(m):
                rng = np.random.default_rng(seed * 1000003 + b)
                pb = []
                for rec in recs:
                    d = rec['date']
                    if not rec['sigs']:
                        pb.append((d, []))
                        continue
                    dom = G.tradable_domain(sym, name, rec['h'], rec['lo'],
                                            rec['c'], rec['v'], rec['pc'])
                    ps, _st = G.sample_pseudo_sigs(rng, dom, segs_by_day[d],
                                                   rec['c'], quotas_by_day[d])
                    pb.append((d, ps))
                pseudo_sets.append(pb)

            cost = G.cost_for_symbol(sym)
            hb = G.has_base_of(sym)
            cfg_exit = G._mk_cfg(dict(trail_activate_pct=0.4, trail_pct=0.6))

            for cand, ov in CANDIDATES.items():
                cfg_gt = replace(base_cfg, signal_gap=G.SIGNAL_GAP, **ov)
                sigs_by_day = [(rec['date'],
                                detect_signals_general(rec['data'], rec['pc'], cfg_gt))
                               for rec in recs]
                sm = simulate_position_sm(sigs_by_day, prices_by_day,
                                          config_long=cfg_exit, config_short=cfg_exit,
                                          cost=cost, has_base=hb,
                                          exec_delay_bars=exec_delay)
                acc[cand]['real'].extend(float(t['ret_pct']) for t in sm['trips'])
                for b in range(m):
                    smb = simulate_position_sm(pseudo_sets[b], prices_by_day,
                                               config_long=cfg_exit, config_short=cfg_exit,
                                               cost=cost, has_base=hb,
                                               exec_delay_bars=exec_delay)
                    acc[cand]['rand'][b].extend(float(t['ret_pct']) for t in smb['trips'])
        except Exception as e:  # 单标的不打断全池
            errors[sym] = f'{type(e).__name__}: {e}'
            beat(f"[{i}/{len(syms)}] {sym} 异常：{type(e).__name__}: {e}")
        if i % hb_every == 0:
            beat(f"[{i}/{len(syms)}] 已完成 {i} 标的（errors={len(errors)}）")
    return acc, errors


def summarize(acc, m):
    out = {}
    for cand, d in acc.items():
        st = G.trip_stats(d['real'])
        rw = np.array([G.trip_stats(x)['net_wr'] if x else np.nan for x in d['rand']],
                      dtype=float)
        rm = np.array([G.trip_stats(x)['mean_net'] if x else np.nan for x in d['rand']],
                      dtype=float)
        p_wr, z_wr = G._pz(st['net_wr'], rw)
        p_mn, z_mn = G._pz(st['mean_net'], rm)
        out[cand] = dict(stats=st, p_netwr=p_wr, z_netwr=z_wr,
                         p_meannet=p_mn, z_meannet=z_mn,
                         rand_netwr_median=float(np.nanmedian(rw)) if len(rw) else None)
    return out


def main():
    ap = argparse.ArgumentParser(description='池级 GT 候选合规重跑（paired，exec_delay=1）')
    ap.add_argument('--pool', default=POOL_FILE)
    ap.add_argument('--syms', default='', help='覆盖池（逗号分隔，冒烟用）')
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--m', type=int, default=50)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--exec-delay', dest='exec_delay', type=int, default=1, choices=(0, 1))
    ap.add_argument('--out', default='')
    ap.add_argument('--feishu', action='store_true')
    a = ap.parse_args()

    syms = ([s.strip() for s in a.syms.split(',') if s.strip()] if a.syms
            else load_pool(a.pool))
    out_fp = a.out or os.path.join(
        OUT, f"gt_candidate_pool_{datetime.date.today():%Y%m%d}.json")

    if os.path.exists(DONE_FLAG):
        os.remove(DONE_FLAG)
    beat(f"🚀 起跑 池级 GT 候选重跑 | {len(syms)} 标的 × {len(CANDIDATES)} 候选 × "
         f"m={a.m} × {a.days}日 × exec_delay={a.exec_delay}")
    if a.feishu:
        feishu(f"[tpoint RSI] 池级 GT 候选重跑起跑：{len(syms)} 标的 × "
               f"{len(CANDIDATES)} 候选（paired，exec_delay={a.exec_delay}）")

    t0 = datetime.datetime.now()
    acc, errors = eval_pool(syms, a.days, a.m, a.seed, a.exec_delay)
    pooled = summarize(acc, a.m)

    base = pooled[BASELINE]['stats']
    rows = []
    for cand, r in pooled.items():
        st = r['stats']
        d_wr = (None if (st['net_wr'] is None or base['net_wr'] is None)
                else (st['net_wr'] - base['net_wr']) * 100)
        ref = CANDIDATES[cand]
        if cand == BASELINE:
            verdict = 'BASELINE'
        else:
            ok_n = st['n_trips'] >= N_MIN
            ok_z = (r['z_netwr'] is not None and r['z_netwr'] >= Z_MIN
                    and r['p_netwr'] is not None and r['p_netwr'] < P_MAX)
            mean_not_worse = (st['mean_net'] is not None and base['mean_net'] is not None
                              and st['mean_net'] >= base['mean_net'] - 1e-9)
            ok_d = (d_wr is not None and d_wr >= DELTA_WR_PP and mean_not_worse)
            verdict = 'PASS_GATE' if (ok_n and ok_z and ok_d) else 'FAIL'
        rows.append(dict(candidate=cand, overrides=ref, n_trips=st['n_trips'],
                         net_wr=(None if st['net_wr'] is None else round(st['net_wr'] * 100, 3)),
                         mean_net=(None if st['mean_net'] is None else round(st['mean_net'], 4)),
                         pl_ratio=(None if st['pl_ratio'] is None else round(st['pl_ratio'], 4)),
                         pct_ge_1=(None if st['pct_ge_1'] is None else round(st['pct_ge_1'] * 100, 3)),
                         max_loss=(None if st['max_loss'] is None else round(st['max_loss'], 3)),
                         delta_net_wr_pp=(None if d_wr is None else round(d_wr, 3)),
                         z_netwr=(None if r['z_netwr'] is None else round(r['z_netwr'], 3)),
                         p_netwr=r['p_netwr'], z_meannet=(None if r['z_meannet'] is None else round(r['z_meannet'], 3)),
                         rand_netwr_median_pct=(None if r['rand_netwr_median'] is None
                                                else round(r['rand_netwr_median'] * 100, 3)),
                         verdict=verdict))

    dt = (datetime.datetime.now() - t0).total_seconds()
    result = dict(
        task='gt_candidate_pool',
        generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        caliber=dict(pool=a.pool, n_syms=len(syms), days=a.days, m=a.m, seed=a.seed,
                     exec_delay_bars=a.exec_delay,
                     judge=dict(z_min=Z_MIN, p_max=P_MAX, n_min=N_MIN, delta_wr_pp=DELTA_WR_PP),
                     paired_note='伪样本按基线信号 quota 抽一次，全部候选共享；真/伪臂同 exec_delay'),
        rows=rows, errors=errors, elapsed_s=round(dt, 1),
        watermark=('exec_delay_bars=1 成交口径（1=次根 bar open 对齐实盘）；'
                   '本产物用于参数层 apply 门槛判定，非验收门读数'))

    with open(out_fp, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    passed = [r['candidate'] for r in rows if r['verdict'] == 'PASS_GATE']
    lines = [f"池级 GT 候选重跑完成 耗时 {dt:.0f}s（{len(syms)} 标的，errors={len(errors)}）",
             f"基线：n={base['n_trips']} wr={base['net_wr'] and round(base['net_wr']*100,2)}% "
             f"mean_net={base['mean_net'] and round(base['mean_net'],4)}pp"]
    for r in rows:
        lines.append(f"{r['verdict']:9s} {r['candidate']:24s} n={r['n_trips']:5d} "
                     f"wr={r['net_wr']}% Δ={r['delta_net_wr_pp']}pp z={r['z_netwr']} "
                     f"mn={r['mean_net']}")
    lines.append(f"过闸候选：{passed if passed else '无'}")
    lines.append(f"产物：{out_fp}")
    for ln in lines:
        print(ln)
    with open(HEARTBEAT, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    with open(DONE_FLAG, 'w', encoding='utf-8') as f:
        f.write(f"done {datetime.datetime.now():%Y-%m-%d %H:%M:%S} out={out_fp} "
                f"passed={passed}\n")
    if a.feishu:
        feishu("[tpoint RSI] 池级 GT 候选重跑完成\n" + '\n'.join(lines[:min(12, len(lines))]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
