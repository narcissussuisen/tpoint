# -*- coding: utf-8 -*-
"""
exit_anchor_grid.py —— G1 出场锚对齐 K1「1~3 点/次」trail/硬止损随机对照网格

命题（docs/player_benchmark.md §五-G1，output/player_level_assessment_20260928.md 批准）：
  当前出场 trail 0.4/0.6 把 mean_win 封顶在 0.717pp，远低于选手 K1 锚「1~3 点/次」；
  亏损侧无硬底（单笔 max -14.755pp vs 选手 -5% 硬割）。
  ⇒ 对「trail 档 + FIXSTOP 硬止损档」做大样本网格，随机对照口径逐臂判定。

口径铁律（与 random_control_validator.py 全同源）：
  - 信号 = detect_signals_general（GT-1.0 默认 + signal_gap=6），pc=前一交易日收盘全历史链；
  - round-trip = simulate_position_sm（底仓模型 + cost_for_symbol 双边成本）；
  - 伪信号 = 同日同时段分层 1:1 配额抽样（sample_pseudo_sigs），**同一套伪样本服务全部臂**
    （paired 对照，臂间差异只来自出场配置）；
  - 基线臂 A0 = 当前生产语义（trail 0.4/0.6、无 FIXSTOP、S信号出场）。

判定（每臂，pooled 跨标的逐笔 pooling）：
  z>=1.0 且 p<0.05（net_wr 或 mean_net 对随机分布，单侧）
  且 Δnet_wr vs A0 >= +2.0pp 且 n_trips>=30
  且 盈亏比 与 ≥+1%达成率 双改善（vs A0）。
  n_trips<30 → INSUFFICIENT（只观察不判定）。

⚠️ T0 水印：全部数字为 samebar 口径（信号 bar 即成交），系统性偏乐观——
   跨臂相对比较有效，绝对值须带折扣读（人审卡 P-20260928-samebar 待裁决）。

用法：
  venv\\Scripts\\python.exe scripts\\research\\exit_anchor_grid.py --syms 300010.SZ --days 20 --m 10   # 冒烟
  venv\\Scripts\\python.exe scripts\\research\\exit_anchor_grid.py --days 60 --m 50 --feishu            # 全池
产物：output/exit_anchor_grid_<yyyymmdd>.json + stdout 汇总表。
"""
import argparse
import datetime
import json
import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GENERAL_DEFAULT          # noqa: E402
from daily_signal_review import build_data                                  # noqa: E402
from exit_manager import make_config, cost_for_symbol                       # noqa: E402
from simulate_position_sm import simulate_position_sm                       # noqa: E402
from random_control_validator import (                                      # noqa: E402
    find_data_path, load_days_full, seg_array, tradable_domain,
    sample_pseudo_sigs, quotas_from_real, has_base_of, SIGNAL_GAP)

POOL_FILE = os.path.join(ROOT, 'data', 'tune_pool_40.json')
OUT = os.path.join(ROOT, 'output')

# ----------------------------------------------------------------------------- #
# 长跑三件套（longtask-feishu-monitor）：信号免疫 + beat/feishu 双函数 + DONE_FLAG
# ----------------------------------------------------------------------------- #
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        import signal as _sig
        _sig.signal(getattr(_sig, _s), _sig.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

TASK_TAG = "exit_anchor_grid"
HEARTBEAT = os.path.join(OUT, f"{TASK_TAG}_heartbeat.txt")
DONE_FLAG = os.path.join(OUT, f"{TASK_TAG}_done.flag")
NOTIFY = r"C:/Users/YZP/.workbuddy/notify.py"   # 飞书全局群 hook b4eba7a9
_FEISHU_ON = False  # _run() 里按 --feishu 置位


def beat(text):
    """高频：只写心跳文件（append，供看门狗检测 staleness），不推飞书。"""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
        with open(HEARTBEAT, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    + " " + str(text) + "\n")
    except Exception:
        pass


def feishu(text):
    """低频：心跳 + 推飞书全局群（--feishu 才推；失败不阻断主流程）。"""
    beat(text)
    if not _FEISHU_ON:
        return
    try:
        import subprocess
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[feishu push failed] {e}", flush=True)

# ----------------------------------------------------------------------------- #
# 臂定义（K1 锚：1~3 点/次 ⇒ trail 激活/回撤档上移；选手止损纪律 ⇒ FIXSTOP 1.5-3%）
# kwargs 直喂 exit_manager.make_config；未显式给出的键取 make_config 默认
# （use_stop=False/use_time=False 在 _mk_cfg 里统一钉死，与验证器基线口径一致）。
# ----------------------------------------------------------------------------- #
ARMS = {
    'A0_base_t0.4/0.6':      dict(trail_activate_pct=0.4, trail_pct=0.6),
    # —— trail 档（孤立 trail 效应，无 FIXSTOP）——
    'T1_act0.8_tr1.0':       dict(trail_activate_pct=0.8, trail_pct=1.0),
    'T2_act1.2_tr1.0':       dict(trail_activate_pct=1.2, trail_pct=1.0),
    'T3_act1.2_tr1.5':       dict(trail_activate_pct=1.2, trail_pct=1.5),
    'T4_act1.6_tr1.5':       dict(trail_activate_pct=1.6, trail_pct=1.5),
    'T5_act2.0_tr2.0':       dict(trail_activate_pct=2.0, trail_pct=2.0),
    # —— FIXSTOP 档（基线 trail 上叠硬止损）——
    'F1_fix1.5':             dict(trail_activate_pct=0.4, trail_pct=0.6,
                                  use_fixed_stop=True, fixed_stop_pct=1.5),
    'F2_fix2.0':             dict(trail_activate_pct=0.4, trail_pct=0.6,
                                  use_fixed_stop=True, fixed_stop_pct=2.0),
    'F3_fix2.5':             dict(trail_activate_pct=0.4, trail_pct=0.6,
                                  use_fixed_stop=True, fixed_stop_pct=2.5),
    'F4_fix3.0':             dict(trail_activate_pct=0.4, trail_pct=0.6,
                                  use_fixed_stop=True, fixed_stop_pct=3.0),
    # —— 组合档（trail 对齐 K1 + 硬底）——
    'C1_a1.2t1.5_f1.5':      dict(trail_activate_pct=1.2, trail_pct=1.5,
                                  use_fixed_stop=True, fixed_stop_pct=1.5),
    'C2_a1.2t1.5_f2.0':      dict(trail_activate_pct=1.2, trail_pct=1.5,
                                  use_fixed_stop=True, fixed_stop_pct=2.0),
    'C3_a2.0t2.0_f2.0':      dict(trail_activate_pct=2.0, trail_pct=2.0,
                                  use_fixed_stop=True, fixed_stop_pct=2.0),
}
BASELINE = 'A0_base_t0.4/0.6'

P_PASS = 0.05
Z_PASS = 1.0
DELTA_WR_PASS = 2.0    # Δnet_wr vs A0（pp）
N_MIN_TRIPS = 30


def _mk_cfg(kwargs):
    """钉死与验证器基线一致的开关位，只放行臂定义的旋钮。"""
    base = dict(use_stop=False, use_time=False, use_trailing=True, s_signal_exit=True)
    base.update(kwargs)
    return make_config(**base)


def trip_stats(rets):
    """rets: np.array(ret_pct, 单位=百分点/笔)。返回 pooled 逐笔统计。"""
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return dict(n_trips=0, net_wr=None, mean_net=None, pl_ratio=None,
                    pct_ge_1=None, max_loss=None, mean_win=None, mean_loss=None)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pl = None
    if len(wins) and len(losses) and abs(losses.mean()) > 1e-12:
        pl = float(wins.mean() / abs(losses.mean()))
    return dict(n_trips=int(len(rets)),
                net_wr=float((rets > 0).mean()),
                mean_net=float(rets.mean()),
                pl_ratio=pl,
                pct_ge_1=float((rets >= 1.0).mean()),
                max_loss=float(rets.min()),
                mean_win=float(wins.mean()) if len(wins) else None,
                mean_loss=float(losses.mean()) if len(losses) else None)


def _pz(real, dist):
    """与验证器同口径：p=单侧(dist>=real 比例)；z=(real-mean)/std。"""
    dist = np.asarray(dist, dtype=float)
    dist = dist[~np.isnan(dist)]
    if real is None or len(dist) < 2:
        return None, None
    p = float((dist >= real).mean())
    mu = float(dist.mean())
    sd = float(dist.std(ddof=1))
    z = ((real - mu) / sd) if sd > 0 else (0.0 if real == mu else 1e9)
    return p, float(z)


def detect_recs(sym, days_req):
    """与验证器 validate_symbol 同构：全历史推进 pc 链，返回最近 days_req 日的
    (recs, name)；rec.date/sigs/pc + prices_by_day 原料。"""
    path = find_data_path(sym)
    if path is None:
        return None, None, f'no_data'
    days_all, name = load_days_full(path)
    recs = []
    prev_close = None
    for d in sorted(days_all.keys()):
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
        sigs = detect_signals_general(data, pc, replace(GENERAL_DEFAULT,
                                                        signal_gap=SIGNAL_GAP))
        recs.append(dict(date=d, pc=float(pc), sigs=sigs, data=data,
                         h=dd['h'], lo=dd['lo'], c=c, v=dd['v'],
                         times=dd['times']))
    recs = recs[-days_req:] if days_req > 0 else recs
    return recs, name, None


def eval_symbol(sym, days_req, m, seed, hb_default=True, verbose=True, exec_delay=0):
    """单标的：真实信号 + 一套伪样本（m 套），全部臂 paired 评估。
    返回 dict(arm -> {real: trip_stats, rand_netwr: [...], rand_meannet: [...],
                      real_rets: [...], rand_rets: [[...], ...]})。"""
    recs, name, err = detect_recs(sym, days_req)
    if err:
        return {'sym': sym, 'error': err}
    if not recs:
        return {'sym': sym, 'error': 'no_valid_days'}

    prices_by_day = []
    pre_by_day = {}
    for rec in recs:
        d = rec['date']
        segs = seg_array(rec['times'])
        dom = tradable_domain(sym, name, rec['h'], rec['lo'], rec['c'],
                              rec['v'], rec['pc'])
        pre_by_day[d] = dict(segs=segs, dom=dom,
                             quotas=quotas_from_real(rec['sigs'], segs))
        data = rec['data']
        prices_by_day.append((d, {'o': data['o'], 'h': data['h'], 'lo': data['lo'],
                                  'c': data['c'], 'atr': data['atr'],
                                  'trend': data.get('trend'), 'n': data['n'],
                                  'pc': rec['pc'], 'sym': sym, 'date': d}))
    sigs_real_by_day = [(rec['date'], rec['sigs']) for rec in recs]
    n_signals = sum(len(s) for _, s in sigs_real_by_day)

    # —— 伪样本只抽一次，全部臂复用（paired）——
    pseudo_sets = []
    for b in range(m):
        rng = np.random.default_rng(seed * 1000003 + b)
        pb = []
        for rec in recs:
            d = rec['date']
            if not rec['sigs']:
                pb.append((d, []))
                continue
            pre = pre_by_day[d]
            ps, _st = sample_pseudo_sigs(rng, pre['dom'], pre['segs'],
                                         rec['c'], pre['quotas'])
            pb.append((d, ps))
        pseudo_sets.append(pb)

    cost = cost_for_symbol(sym)
    hb = has_base_of(sym)

    arms_out = {}
    for ai, (arm, kw) in enumerate(ARMS.items()):
        cfg = _mk_cfg(kw)
        sm = simulate_position_sm(sigs_real_by_day, prices_by_day,
                                  config_long=cfg, config_short=cfg,
                                  cost=cost, has_base=hb, exec_delay_bars=exec_delay)
        real_rets = [float(t['ret_pct']) for t in sm['trips']]
        rand_netwr = np.full(m, np.nan)
        rand_meannet = np.full(m, np.nan)
        rand_rets = []
        for b in range(m):
            smb = simulate_position_sm(pseudo_sets[b], prices_by_day,
                                      config_long=cfg, config_short=cfg,
                                      cost=cost, has_base=hb, exec_delay_bars=exec_delay)
            rb = [float(t['ret_pct']) for t in smb['trips']]
            rand_rets.append(rb)
            if rb:
                ra = np.asarray(rb)
                rand_netwr[b] = (ra > 0).mean()
                rand_meannet[b] = ra.mean()
        arms_out[arm] = dict(real=trip_stats(real_rets), real_rets=real_rets,
                             rand_netwr=rand_netwr.tolist(),
                             rand_meannet=rand_meannet.tolist(),
                             rand_rets=rand_rets)
        if verbose:
            st = arms_out[arm]['real']
            print(f'    [{ai + 1}/{len(ARMS)}] {sym} {arm}: '
                  f'n={st["n_trips"]} wr={st["net_wr"] and round(st["net_wr"] * 100, 2)}%',
                  flush=True)
    return dict(sym=sym, name=name, n_days=len(recs), n_signals=n_signals,
                has_base=hb, cost=cost, arms=arms_out)


def _run():
    ap = argparse.ArgumentParser(description='G1 出场锚对齐 K1「1~3 点」trail/硬止损随机对照网格')
    ap.add_argument('--pool', default=POOL_FILE, help='标的池 JSON（默认 tune_pool_40）')
    ap.add_argument('--syms', default='', help='覆盖池（逗号分隔，冒烟用）')
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--m', type=int, default=50, help='伪样本套数（全部臂复用）')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--feishu', action='store_true', help='起跑/心跳/完赛飞书推送')
    ap.add_argument('--exec-delay', dest='exec_delay', type=int, default=1,
                    choices=(0, 1),
                    help='成交延迟：1=次根 bar open（对齐实盘，默认）；0=samebar 历史口径（偏乐观）')
    ap.add_argument('--out', default='', help='产物路径（默认 output/exit_anchor_grid_<date>.json）')
    a = ap.parse_args()

    if a.syms:
        syms = [s.strip() for s in a.syms.split(',') if s.strip()]
    else:
        with open(a.pool, encoding='utf-8') as f:
            pool = json.load(f)
        # tune_pool_40.json 实为 {generated_at, seed, min_rows, exclude, pool:[{symbol,...}]}
        if isinstance(pool, dict):
            pool = pool.get('pool', [])
        syms = [(ent['symbol'] if isinstance(ent, dict) else str(ent)) for ent in pool]

    global _FEISHU_ON
    _FEISHU_ON = bool(a.feishu)

    t0 = datetime.datetime.now()
    feishu(f'🚀 [{TASK_TAG}] G1出场网格起跑 | {len(syms)} 标的 × {len(ARMS)} 臂 '
           f'× m={a.m} × {a.days}日（samebar 口径，相对比较有效）')

    per_sym = []
    pooled_real = {arm: [] for arm in ARMS}
    pooled_rand = {arm: [[] for _ in range(a.m)] for arm in ARMS}
    for si, sym in enumerate(syms):
        print(f'== [{si + 1}/{len(syms)}] {sym} ==', flush=True)
        r = eval_symbol(sym, a.days, a.m, a.seed, exec_delay=a.exec_delay)
        if 'error' in r:
            print(f'  !! {sym}: {r["error"]}', flush=True)
            per_sym.append(r)
            continue
        per_sym.append(dict(sym=r['sym'], name=r['name'], n_days=r['n_days'],
                            n_signals=r['n_signals'], has_base=r['has_base'],
                            arms={arm: dict(real=v['real'],
                                            rand_netwr=v['rand_netwr'],
                                            rand_meannet=v['rand_meannet'])
                                  for arm, v in r['arms'].items()}))
        for arm, v in r['arms'].items():
            pooled_real[arm].extend(v['real_rets'])
            for b in range(a.m):
                pooled_rand[arm][b].extend(v['rand_rets'][b])
        beat(f'[{si + 1}/{len(syms)}] {sym} 完成')
        if (si + 1) % 10 == 0:
            feishu(f'⏳ [{TASK_TAG}] 心跳 {si + 1}/{len(syms)} 标的完成，'
                   f'已耗时 {(datetime.datetime.now() - t0).total_seconds() / 60:.1f} 分钟')

    # —— pooled 判定 ——
    base = trip_stats(pooled_real[BASELINE])
    pooled = {}
    for arm in ARMS:
        st = trip_stats(pooled_real[arm])
        rw = np.array([trip_stats(pooled_rand[arm][b])['net_wr']
                       if pooled_rand[arm][b] else np.nan for b in range(a.m)])
        rm = np.array([trip_stats(pooled_rand[arm][b])['mean_net']
                       if pooled_rand[arm][b] else np.nan for b in range(a.m)])
        p_wr, z_wr = _pz(st['net_wr'], rw)
        p_mn, z_mn = _pz(st['mean_net'], rm)
        d_wr = ((st['net_wr'] - base['net_wr']) * 100.0
                if st['net_wr'] is not None and base['net_wr'] is not None else None)
        ok_rand = ((p_wr is not None and p_wr < P_PASS and z_wr >= Z_PASS) or
                   (p_mn is not None and p_mn < P_PASS and z_mn >= Z_PASS))
        pl_up = (st['pl_ratio'] is not None and base['pl_ratio'] is not None
                 and st['pl_ratio'] > base['pl_ratio'])
        g1_up = (st['pct_ge_1'] is not None and base['pct_ge_1'] is not None
                 and st['pct_ge_1'] > base['pct_ge_1'])
        if st['n_trips'] < N_MIN_TRIPS:
            verdict = 'INSUFFICIENT'
        elif arm == BASELINE:
            verdict = 'BASELINE'
        elif ok_rand and d_wr is not None and d_wr >= DELTA_WR_PASS and pl_up and g1_up:
            verdict = 'PASS_GATE'
        else:
            verdict = 'FAIL_GATE'
        pooled[arm] = dict(stats=st, p=dict(net_wr=p_wr, mean_net=p_mn),
                           z=dict(net_wr=z_wr, mean_net=z_mn),
                           delta_wr_pp=d_wr, pl_improved=pl_up, ge1_improved=g1_up,
                           verdict=verdict)

    # —— 汇总表 ——
    print('\n================ G1 出场网格 pooled 汇总 ================')
    print(f'{"臂":<20} {"n":>5} {"净胜率":>7} {"Δpp":>6} {"笔均净":>7} {"盈亏比":>6} '
          f'{"≥+1%":>6} {"max亏":>7} {"z_wr":>6} {"判定":<12}')
    for arm, r in pooled.items():
        s = r['stats']
        def _f(x, k=100.0, d=2):
            return f'{x * k:.{d}f}' if x is not None else '--'
        print(f'{arm:<20} {s["n_trips"]:>5} '
              f'{_f(s["net_wr"]):>7} '
              f'{("%+.2f" % r["delta_wr_pp"]) if r["delta_wr_pp"] is not None else "--":>6} '
              f'{("%+.3f" % s["mean_net"]) if s["mean_net"] is not None else "--":>7} '
              f'{("%.3f" % s["pl_ratio"]) if s["pl_ratio"] is not None else "--":>6} '
              f'{_f(s["pct_ge_1"]):>6} '
              f'{("%.2f" % s["max_loss"]) if s["max_loss"] is not None else "--":>7} '
              f'{("%.2f" % r["z"]["net_wr"]) if r["z"]["net_wr"] is not None else "--":>6} '
              f'{r["verdict"]:<12}')

    out_fp = a.out or os.path.join(
        OUT, f'exit_anchor_grid_{datetime.date.today().strftime("%Y%m%d")}.json')
    os.makedirs(os.path.dirname(out_fp), exist_ok=True)
    doc = dict(
        meta=dict(generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  syms=syms, days=a.days, m=a.m, seed=a.seed,
                  exec_delay=a.exec_delay,
                  arms={k: _mk_cfg(v) for k, v in ARMS.items()},
                  baseline=BASELINE,
                  gate=dict(z=Z_PASS, p=P_PASS, delta_wr_pp=DELTA_WR_PASS,
                            n_min_trips=N_MIN_TRIPS,
                            extra='盈亏比与≥+1%达成率双改善（vs A0）'),
                  watermark=('exec_delay_bars=%d 成交口径（1=次根 bar open 对齐实盘 / '
                             '0=samebar 偏乐观）；真/伪臂同 delay，跨臂相对比较有效'
                             % a.exec_delay),
                  units='net_wr/pct_ge_1=比例(0-1); mean_net/max_loss/mean_win/'
                        'mean_loss=百分点/笔; delta_wr_pp=百分点'),
        pooled=pooled, per_sym=per_sym)
    with open(out_fp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    dt = (datetime.datetime.now() - t0).total_seconds()
    print(f'\nJSON -> {out_fp}\n总耗时 {dt:.1f}s')

    # DONE_FLAG 必须先于任何慢速收尾/推送（longtask 铁律）
    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(f'done {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} '
                    f'syms={len(syms)} arms={len(ARMS)} out={out_fp}\n')
    except Exception:
        pass

    passed = [arm for arm, r in pooled.items() if r['verdict'] == 'PASS_GATE']
    b = pooled[BASELINE]['stats']
    _wr = f'{b["net_wr"] * 100:.2f}%' if b['net_wr'] is not None else '--'
    _mn = f'{b["mean_net"]:+.3f}pp' if b['mean_net'] is not None else '--'
    _pl = f'{b["pl_ratio"]:.3f}' if b['pl_ratio'] is not None else '--'
    _g1 = f'{b["pct_ge_1"] * 100:.1f}%' if b['pct_ge_1'] is not None else '--'
    feishu(f'🎉 [{TASK_TAG}] G1出场网格完赛 | {len(syms)} 标的 × {len(ARMS)} 臂，'
           f'耗时 {dt / 60:.1f} 分钟。\n'
           f'基线 A0（trail 0.4/0.6）：n={b["n_trips"]} 净胜率 {_wr} '
           f'笔均净 {_mn} 盈亏比 {_pl} ≥+1%达成 {_g1}。\n'
           f'过闸臂：{", ".join(passed) if passed else "无"}。\n'
           f'产物：{out_fp}（samebar 口径，相对比较有效）')


def main():
    try:
        _run()
    except Exception as e:
        import traceback
        feishu(f'💥 [{TASK_TAG}] 异常: {e}')
        print(traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
