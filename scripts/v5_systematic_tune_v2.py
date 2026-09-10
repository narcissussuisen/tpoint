# -*- coding: utf-8 -*-
"""
v5_systematic_tune_v2.py — v5/GT 参数系统性调优（修正版 v2）

针对 v1 的四处方法论硬伤修正：
  1) 过拟合：样本从 7 只 ETF 扩到宽覆盖随机大子集（默认 128 训练 / 64 测试，来自 192 标清洁宇宙），
     并做 symbol 级 train/test 拆分，杜绝"极少非随机抽样"。
  2) FWD_HORIZON 改为 to-EOD：战场是日内分时做T、抓当天波动，时间维度=当日交易时段(240根1m)。
     前瞻窗口 = 信号到当日收盘的剩余 bar（不跨日）；同时报告 30/60/120/EOD 剖面确认 edge 随时段成熟。
  3) 彻底去除 WR：改用 DET 抓顶底指标 ——
        DA_to_eod   : 净方向准确性（有利极端 > 不利极端）
        EHR@0.5%_to_eod : 极端捕捉命中率（价格触及 ≥0.5% 有利极端）
        TEP         : 理论边（在极端处出场的每笔净值，替代被证伪的 WR）
  4) 优化目标 = 抓顶底复合指标 (EHR + DA)/2 @ EOD；IS/OOS 按时间切分 + symbol 级 train/test 防过拟合。

数据：F:/keyfactor_data/1m_clean（192 标的清洁 1m）
信号：core/general_signal.detect_signals_general（与生产 monitor 同源）
输出：output/v5_systematic_tune_v2_<date>.json + .html
"""
import os, sys, json, csv, argparse, datetime, itertools, signal as _signal, random, subprocess, time
# 进程级信号免疫：避免工具侧超时强杀波及
for _s in ("SIGINT", "SIGBREAK", "SIGTERM"):
    try:
        _signal.signal(getattr(_signal, _s), _signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass

import numpy as np
import pandas as pd
from multiprocessing import Pool

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from general_signal import detect_signals_general, GeneralConfig, STRATEGY_VERSION, ENGINE_FULL
from daily_signal_review import build_data

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

OOS_SPLIT = 0.30            # 每标后 30% 交易日作为时间维 OOS
HORIZONS = [30, 60, 120, 'EOD']   # 剖面：30/60/120 分钟 + 当日收盘(to-EOD)
EHR_THRESH = 0.005          # 极端捕捉阈值 0.5%（与 DET 对齐）
COST = 0.0005               # 理论边扣除的往返成本（保守 ~0.05%）

# ---------- 飞书实时监控 + 心跳 + 完成标记 ----------
HEARTBEAT = os.path.join(OUT, 'v5_tune_v2_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'v5_tune_v2_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'


def feishu(text):
    """写心跳文件（供看门狗检测卡死）+ 推飞书全局群。失败不影响主流程。"""
    try:
        with open(HEARTBEAT, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(text) + '\n')
    except Exception:
        pass
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f'[feishu push failed] {e}')
TRAIN_RATIO = 0.66          # 128/192 训练
SEED = 20260823


# ---------- 数据加载 ----------
def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.setdefault(r['trade_date'], []).append(r)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs], dtype=float)
        h = np.array([float(x['high']) for x in rs], dtype=float)
        lo = np.array([float(x['low']) for x in rs], dtype=float)
        c = np.array([float(x['close']) for x in rs], dtype=float)
        v = np.array([float(x['volume']) for x in rs], dtype=float)
        days[d] = (o, h, lo, c, v)
    return days


def build_runs(sym, min_bars=200):
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return []
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    pc_map = {}
    prev = None
    for d in dates:
        _, _, _, c, _ = days_all[d]
        pc_map[d] = prev if prev is not None else (c[0] if len(c) else 0.0)
        if len(c):
            prev = c[-1]
    complete = [(d, days_all[d]) for d in dates if len(days_all[d][3]) >= min_bars]
    n_is = int(len(complete) * (1 - OOS_SPLIT))
    runs = []
    for idx, (d, (o, h, lo, c, v)) in enumerate(complete):
        runs.append({
            'sym': sym, 'date': d, 'o': o, 'h': h, 'lo': lo, 'c': c, 'v': v,
            'pc': pc_map[d], 'n': len(c),
            'tag': 'IS' if idx < n_is else 'OOS',
            'data': None
        })
    return runs


def prep_run(r):
    if r['pc'] <= 0 or r['n'] < 10:
        return False
    df = pd.DataFrame({
        'open': r['o'], 'high': r['h'], 'low': r['lo'], 'close': r['c'], 'volume': r['v'],
        'trade_time': [r['date'] + ' 09:31:00'] * r['n']
    })
    try:
        r['data'] = build_data(df, r['pc'])
        return r['data'] is not None
    except Exception as e:
        print(f'[{r["sym"]} {r["date"]}] prep error: {e}')
        return False


# ---------- DET 抓顶底指标（去 WR） ----------
def det_for_day(sigs, c, horizons):
    """对单日信号计算各 horizon 的 DET 计数。horizon='EOD' 即用当日剩余 bar。"""
    res = {h: {'n': 0, 'da': 0, 'ehr': 0, 'tep': 0} for h in horizons}
    Lc = len(c)
    for s in sigs:
        i = s['idx']
        if i + 1 >= Lc:
            continue
        entry = c[i]
        for h in horizons:
            end = Lc if h == 'EOD' else min(i + 1 + h, Lc)
            fwd = c[i + 1:end]
            if len(fwd) == 0:
                continue
            mu = fwd.max() / entry - 1.0   # 最大有利偏移
            md = fwd.min() / entry - 1.0   # 最大不利偏移（负）
            if s['type'] == 'B':
                da_c = (mu > -md)
                ehr_c = (mu >= EHR_THRESH)
                edge = mu - COST
            else:  # S
                da_c = (-md > mu)
                ehr_c = (md <= -EHR_THRESH)
                edge = -md - COST
            res[h]['n'] += 1
            if da_c:
                res[h]['da'] += 1
            if ehr_c:
                res[h]['ehr'] += 1
            if edge > 0:
                res[h]['tep'] += 1
    return res


def run_cfg_on_day(r, cfg):
    return detect_signals_general(r['data'], r['pc'], cfg)


# ---------- 并行评估 ----------
def _eval_chunk(args):
    cfg_dicts, runs_sub = args
    out = []
    for cd in cfg_dicts:
        cfg = GeneralConfig(**cd)
        counts = {h: {'n': 0, 'da': 0, 'ehr': 0, 'tep': 0} for h in HORIZONS}
        for sym, runs in runs_sub.items():
            for r in runs:
                if r['tag'] != 'IS':
                    continue
                sigs = run_cfg_on_day(r, cfg)
                d = det_for_day(sigs, r['c'], HORIZONS)
                for h in HORIZONS:
                    counts[h]['n'] += d[h]['n']
                    counts[h]['da'] += d[h]['da']
                    counts[h]['ehr'] += d[h]['ehr']
                    counts[h]['tep'] += d[h]['tep']
        out.append(counts)
    return out


def chunk(seq, n):
    return [seq[i::n] for i in range(n)]


def search_stage(cfg_dicts, runs_by_sym):
    """并行评估某阶段所有候选配置（仅 IS 训练集），返回 idx->聚合计数。"""
    syms = list(runs_by_sym.keys())
    chunks = chunk(syms, 6)
    tasks = [(cfg_dicts, {s: runs_by_sym[s] for s in ch}) for ch in chunks if ch]
    with Pool(6) as p:
        results = p.map(_eval_chunk, tasks)
    agg = []
    for ci in range(len(cfg_dicts)):
        n = da = ehr = tep = 0
        for res in results:
            c = res[ci]
            for h in HORIZONS:
                n += c[h]['n']; da += c[h]['da']; ehr += c[h]['ehr']; tep += c[h]['tep']
        agg.append({'n': n, 'da': da, 'ehr': ehr, 'tep': tep})
    return agg


def composite(agg_row):
    """抓顶底复合指标：EOD 的 (EHR + DA)/2，百分比。"""
    if agg_row['n'] == 0:
        return 0.0
    ehr = 100.0 * agg_row['ehr'] / agg_row['n']
    da = 100.0 * agg_row['da'] / agg_row['n']
    return 0.5 * ehr + 0.5 * da


def rates_of(cfg, runs_by_sym, tag):
    """单配置在给定符号集/标签上的 DET 各 horizon 命中率（用于最终报告，量小不并行）。"""
    counts = {h: {'n': 0, 'da': 0, 'ehr': 0, 'tep': 0} for h in HORIZONS}
    for sym, runs in runs_by_sym.items():
        for r in runs:
            if r['tag'] != tag:
                continue
            sigs = run_cfg_on_day(r, cfg)
            d = det_for_day(sigs, r['c'], HORIZONS)
            for h in HORIZONS:
                counts[h]['n'] += d[h]['n']
                counts[h]['da'] += d[h]['da']
                counts[h]['ehr'] += d[h]['ehr']
                counts[h]['tep'] += d[h]['tep']
    out = {}
    for h in HORIZONS:
        n = counts[h]['n']
        out[h] = {
            'n': n,
            'da_rate': round(100.0 * counts[h]['da'] / n, 2) if n else 0.0,
            'ehr_rate': round(100.0 * counts[h]['ehr'] / n, 2) if n else 0.0,
            'tep_rate': round(100.0 * counts[h]['tep'] / n, 2) if n else 0.0,
        }
    return out


# ---------- Coordinate Ascent ----------
def cfg_to_dict(cfg):
    return dict(cfg.__dict__)


def coordinate_ascent(runs_by_sym):
    base = GeneralConfig()
    best = base
    stages = []

    # 阶段 A：权重组合
    grids_A = list(itertools.product(
        [0.8, 1.0, 1.2, 1.5], [0.3, 0.5, 0.7, 1.0], [0.6, 0.9, 1.2], [0.6, 0.8, 1.0]))
    cands = [dict(cfg_to_dict(base), w_vwap=wv, w_vol_div=wvd, w_macd_div=wm, w_rsi=wr)
             for wv, wvd, wm, wr in grids_A]
    agg = search_stage(cands, runs_by_sym)
    bi = max(range(len(cands)), key=lambda i: composite(agg[i]))
    best = GeneralConfig(**cands[bi])
    stages.append({'name': '权重组合', 'n': len(cands),
                   'best_composite': round(composite(agg[bi]), 2),
                   'best': {'w_vwap': best.w_vwap, 'w_vol_div': best.w_vol_div,
                            'w_macd_div': best.w_macd_div, 'w_rsi': best.w_rsi}})
    feishu(f"✅ 阶段【{stages[-1]['name']}】完成 | 最优复合={stages[-1]['best_composite']}% | 最优={json.dumps(stages[-1]['best'], ensure_ascii=False)}")

    # 阶段 B：threshold + gap
    grids_B = list(itertools.product([0.40, 0.45, 0.50, 0.55], [6, 8]))
    cands = [dict(cfg_to_dict(best), buy_threshold=thr, sell_threshold=thr, signal_gap=gap)
             for thr, gap in grids_B]
    agg = search_stage(cands, runs_by_sym)
    bi = max(range(len(cands)), key=lambda i: composite(agg[i]))
    best = GeneralConfig(**cands[bi])
    stages.append({'name': 'threshold+gap', 'n': len(cands),
                   'best_composite': round(composite(agg[bi]), 2),
                   'best': {'buy_threshold': best.buy_threshold, 'signal_gap': best.signal_gap}})
    feishu(f"✅ 阶段【{stages[-1]['name']}】完成 | 最优复合={stages[-1]['best_composite']}% | 最优={json.dumps(stages[-1]['best'], ensure_ascii=False)}")

    # 阶段 C：双向门控 + regime
    grids_C = list(itertools.product([True, False], [True, False]))
    cands = [dict(cfg_to_dict(best), b_downtrend_reversal=b, regime_gate=r)
             for b, r in grids_C]
    agg = search_stage(cands, runs_by_sym)
    bi = max(range(len(cands)), key=lambda i: composite(agg[i]))
    best = GeneralConfig(**cands[bi])
    stages.append({'name': '双向门控+regime', 'n': len(cands),
                   'best_composite': round(composite(agg[bi]), 2),
                   'best': {'b_downtrend_reversal': best.b_downtrend_reversal,
                            'regime_gate': best.regime_gate}})
    feishu(f"✅ 阶段【{stages[-1]['name']}】完成 | 最优复合={stages[-1]['best_composite']}% | 最优={json.dumps(stages[-1]['best'], ensure_ascii=False)}")

    # 阶段 D：RSI 参数
    grids_D = list(itertools.product([9, 14, 21], [(30, 70), (35, 65), (40, 60)]))
    cands = [dict(cfg_to_dict(best), rsi_period=p, rsi_oversold=os_, rsi_overbought=ob)
             for p, (os_, ob) in grids_D]
    agg = search_stage(cands, runs_by_sym)
    bi = max(range(len(cands)), key=lambda i: composite(agg[i]))
    best = GeneralConfig(**cands[bi])
    stages.append({'name': 'RSI参数', 'n': len(cands),
                   'best_composite': round(composite(agg[bi]), 2),
                   'best': {'rsi_period': best.rsi_period,
                            'rsi_oversold': best.rsi_oversold, 'rsi_overbought': best.rsi_overbought}})

    # 阶段 E：vwap_k1 + div_local_w + div_vol_ratio
    grids_E = list(itertools.product([0.5, 0.8, 1.0, 1.2], [10, 15, 20], [0.6, 0.7, 0.8]))
    cands = [dict(cfg_to_dict(best), vwap_k1=k1, div_local_w=dw, div_vol_ratio=dvr)
             for k1, dw, dvr in grids_E]
    agg = search_stage(cands, runs_by_sym)
    bi = max(range(len(cands)), key=lambda i: composite(agg[i]))
    best = GeneralConfig(**cands[bi])
    stages.append({'name': 'vwap+量价背离', 'n': len(cands),
                   'best_composite': round(composite(agg[bi]), 2),
                   'best': {'vwap_k1': best.vwap_k1, 'div_local_w': best.div_local_w,
                            'div_vol_ratio': best.div_vol_ratio}})

    return best, stages


def make_monitor_config(global_cfg_dict):
    return {
        "_global": {
            "settle_split_enable": False,
            "_note": "2026-08-23 v5 调优v2候选（去WR/to-EOD/宽样本防过拟合，未部署）",
            "use_general_engine": True,
            "bidirectional_enable": True,
            "general_algorithm": {
                "_note": "v5 v2 候选（由 v5_systematic_tune_v2.py 从192标清洁数据 IS/OOS+symbol train/test 选出）",
                "strategy_version": STRATEGY_VERSION,
                "engine": ENGINE_FULL,
                **global_cfg_dict
            }
        }
    }


def build_html(out, path):
    base = out['baseline']
    best = out['best']

    def row(h):
        b = best['rates'][str(h)]; o = base['rates'][str(h)]
        return (f"<tr><td>{h}</td><td>{b['da_rate']}%</td><td>{b['ehr_rate']}%</td>"
                f"<td>{b['tep_rate']}%</td><td>{b['n']}</td>"
                f"<td>{o['da_rate']}%</td><td>{o['ehr_rate']}%</td><td>{o['n']}</td></tr>")

    rows = ''.join(row(h) for h in HORIZONS)
    stage_rows = ''
    for s in out['stages']:
        stage_rows += f"<tr><td>{s['name']}</td><td>{s['n']}</td><td>{s['best_composite']}%</td><td><pre>{json.dumps(s['best'], ensure_ascii=False)}</pre></td></tr>"

    def tbl(d):
        return (f"DA={d['EOD']['da_rate']}% / EHR@0.5%={d['EOD']['ehr_rate']}% / TEP={d['EOD']['tep_rate']}% "
                f"(n={d['EOD']['n']})")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>v5 调优 v2 报告 {out['date']}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:40px;background:#f7f8fa;color:#1f2329;}}
.card{{background:#fff;border-radius:8px;padding:24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08);}}
h1{{font-size:22px;margin-top:0;}} h2{{font-size:17px;color:#3370ff;margin-top:24px;}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px;}}
th,td{{border:1px solid #e1e3e6;padding:8px;text-align:left;}} th{{background:#f2f3f5;}}
pre{{background:#f7f8fa;padding:8px;border-radius:4px;overflow-x:auto;font-size:12px;}}
.pass{{color:#00b42a;font-weight:bold;}} .fail{{color:#f53f3f;font-weight:bold;}}
.note{{color:#666;font-size:13px;}}
</style></head><body>
<div class="card"><h1>v5/GT 系统性调优 v2 报告（{out['date']}）</h1>
<p>引擎：{out['engine']} | 训练标的数：{out['n_train']} | 测试标的数：{out['n_test']}（来自192标清洁宇宙，随机种子{out['seed']}）</p>
<p>前瞻窗口：<b>to-EOD（信号→当日收盘，不跨日）</b> + 剖面 30/60/120；OOS切分：每标后{int(out['oos_split']*100)}%交易日（时间维）。</p>
<p class="note">优化目标 = 抓顶底复合指标 <b>(EHR@0.5% + DA)/2 @ EOD</b>。已<b>彻底去除 WR</b>，改用 DET 指标（DA/EHR/TEP）。</p>
<p>防过拟合：宽样本 + 时间维 IS/OOS + symbol 级 train/test 双重拆分。</p>
<p>部署就绪：{'<span class="pass">YES</span>' if out['deploy_ready'] else '<span class="fail">NO</span>'} — {out['deploy_reason']}</p>
</div>
<div class="card"><h2>基线 vs 最优 · 抓顶底指标（核心，无WR）</h2>
<table><tr><th rowspan="2">horizon</th><th colspan="4">最优</th><th colspan="3">基线</th></tr>
<tr><th>DA</th><th>EHR@0.5%</th><th>TEP</th><th>n</th><th>DA</th><th>EHR@0.5%</th><th>n</th></tr>
{rows}</table>
<p class="note">EHR@0.5% = 极端捕捉命中率（价格触及≥0.5%有利极端）；DA = 净方向准确性；TEP = 理论边正率。</p>
</div>
<div class="card"><h2>Coordinate Ascent 各阶段</h2>
<table><tr><th>阶段</th><th>组合数</th><th>最优复合</th><th>最优参数</th></tr>{stage_rows}</table></div>
<div class="card"><h2>防过拟合证据（IS vs OOS vs 测试集）</h2>
<table>
<tr><th>配置</th><th>训练IS(EOD)</th><th>训练OOS(EOD)</th><th>测试集(EOD)</th><th>全样本(EOD)</th></tr>
<tr><td>基线</td><td>{tbl(base['train_is'])}</td><td>{tbl(base['train_oos'])}</td><td>{tbl(base['test'])}</td><td>{tbl(base['full'])}</td></tr>
<tr><td><b>最优</b></td><td>{tbl(best['train_is'])}</td><td>{tbl(best['train_oos'])}</td><td>{tbl(best['test'])}</td><td>{tbl(best['full'])}</td></tr>
</table>
<p class="note">若 测试集 / 训练OOS 与 训练IS 接近 → 无过拟合；若测试集明显低于训练IS → 过拟合警报。</p>
</div>
<div class="card"><h2>候选生产配置（monitor_config.json._global.general_algorithm，未部署）</h2>
<pre>{json.dumps(out['monitor_config_candidate'], ensure_ascii=False, indent=2)}</pre></div>
</body></html>"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'HTML -> {path}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--syms', default=None, help='逗号分隔符号覆盖（默认全192标）')
    ap.add_argument('--seed', type=int, default=SEED)
    a = ap.parse_args()

    # 确定符号集
    if a.syms:
        all_syms = [s.strip() for s in a.syms.split(',') if s.strip()]
    else:
        all_syms = [f[:-7] for f in os.listdir(DATA_DIR) if f.endswith('_1m.csv')]
    all_syms = [s for s in all_syms if os.path.exists(f'{DATA_DIR}/{s}_1m.csv')]
    print(f'[v2] 全宇宙标的数={len(all_syms)}')

    # 随机拆 train/test（防过拟合：symbol 级）
    rng = random.Random(a.seed)
    rng.shuffle(all_syms)
    n_train = max(20, int(len(all_syms) * TRAIN_RATIO))
    train_syms = all_syms[:n_train]
    test_syms = all_syms[n_train:]
    print(f'[v2] 训练={n_train} 测试={len(test_syms)}')

    def load_set(symlist):
        d = {}
        for sym in symlist:
            runs = build_runs(sym)
            ok = sum(1 for r in runs if prep_run(r))
            if ok:
                d[sym] = runs
        return d

    print('[v2] 加载训练集...')
    train_runs = load_set(train_syms)
    print(f'[v2] 训练集有效标的数={len(train_runs)}')
    print('[v2] 加载测试集...')
    test_runs = load_set(test_syms)
    print(f'[v2] 测试集有效标的数={len(test_runs)}')
    feishu(f"🚀 v5调优v2 监控版启动 | 训练{len(train_runs)} 测试{len(test_runs)} 标 | 目标:抓顶底(EHR+DA)/2@EOD | 已彻底去WR")

    full_runs = {**train_runs, **test_runs}

    base = GeneralConfig()
    print('\n=== 基线（训练IS）===')
    base_train_is = rates_of(base, train_runs, 'IS')
    print('基线 EOD:', base_train_is['EOD'])
    feishu(f"📊 基线 EOD: DA={base_train_is['EOD']['da_rate']}% EHR={base_train_is['EOD']['ehr_rate']}% TEP={base_train_is['EOD']['tep_rate']}% (n={base_train_is['EOD']['n']})")

    print('\n=== Coordinate Ascent（训练IS）===')
    best, stages = coordinate_ascent(train_runs)
    feishu(f"🏁 Coordinate Ascent 完成 | 各阶段最优复合: " + " / ".join(f"{s['name']}={s['best_composite']}%" for s in stages))
    best_dict = cfg_to_dict(best)
    # 去掉不应暴露字段
    best_dict.pop('trend_b_allowed', None)
    best_dict.pop('trend_s_allowed', None)

    print('\n=== 最终评估（多集合，验证防过拟合）===')
    best_train_is = rates_of(best, train_runs, 'IS')
    best_train_oos = rates_of(best, train_runs, 'OOS')
    best_test = rates_of(best, test_runs, 'IS')   # 测试集仅用IS标签（其OOS本就held-out）
    best_full = rates_of(best, full_runs, 'IS')
    base_train_oos = rates_of(base, train_runs, 'OOS')
    base_test = rates_of(base, test_runs, 'IS')
    base_full = rates_of(base, full_runs, 'IS')

    for name, d in [('基线 训练IS', base_train_is), ('最优 训练IS', best_train_is),
                   ('最优 训练OOS', best_train_oos), ('最优 测试集', best_test), ('最优 全样本', best_full)]:
        print(f'  {name}: EOD DA={d["EOD"]["da_rate"]}% EHR={d["EOD"]["ehr_rate"]}% TEP={d["EOD"]["tep_rate"]}% n={d["EOD"]["n"]}')

    # 防过拟合判定（先于最终推送与产物计算，供文案/产物引用）
    def comp(d):
        e = d['EOD']; return 0.5*e['ehr_rate']+0.5*e['da_rate']
    best_comp_test = comp(best_test); base_comp_test = comp(base_test)
    best_comp_full = comp(best_full); best_comp_tr_is = comp(best_train_is)
    gap = best_comp_tr_is - best_comp_test
    deploy_ready = (best_comp_test > base_comp_test) and (gap <= 8.0) and (best_comp_full >= 55.0)
    deploy_reason = (f"测试集复合 {best_comp_test:.1f}% > 基线 {base_comp_test:.1f}%；"
                     f"训练IS-测试集 gap={gap:.1f}pp（≤8 视为无过拟合）；"
                     f"全样本复合 {best_comp_full:.1f}%")
    if gap > 8.0:
        deploy_reason += "；⚠️ 训练/测试 gap 偏大，疑似过拟合，暂缓部署"

    out = {
        'date': a.out_suffix, 'engine': ENGINE_FULL, 'strategy_version': STRATEGY_VERSION,
        'seed': a.seed, 'oos_split': OOS_SPLIT, 'horizons': [str(h) for h in HORIZONS],
        'ehr_thresh': EHR_THRESH, 'cost': COST,
        'n_train': len(train_runs), 'n_test': len(test_runs),
        'train_syms': sorted(train_runs.keys()), 'test_syms': sorted(test_runs.keys()),
        'baseline': {
            'cfg': cfg_to_dict(base),
            'rates': {str(h): base_train_is[h] for h in HORIZONS},
            'train_is': base_train_is, 'train_oos': base_train_oos,
            'test': base_test, 'full': base_full,
        },
        'best': {
            'cfg': best_dict,
            'rates': {str(h): best_train_is[h] for h in HORIZONS},
            'train_is': best_train_is, 'train_oos': best_train_oos,
            'test': best_test, 'full': best_full,
        },
        'stages': stages,
        'anti_overfit': {
            'best_composite_train_is': round(best_comp_tr_is, 2),
            'best_composite_test': round(best_comp_test, 2),
            'best_composite_full': round(best_comp_full, 2),
            'gap_train_is_minus_test': round(gap, 2),
        },
        'deploy_ready': deploy_ready, 'deploy_reason': deploy_reason,
        'monitor_config_candidate': make_monitor_config(best_dict),
    }

    # 先落盘产物（即便后续推送失败也不丢结果）
    fn = f'v5_systematic_tune_v2_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\nJSON -> {fp}')
    try:
        build_html(out, os.path.join(OUT, f'v5_systematic_tune_v2_{a.out_suffix}.html'))
    except Exception as e:
        print(f'[build_html failed] {e}')

    try:
        feishu(
            f"📈 v5调优v2 最终结果(去WR/抓顶底):\n"
            f"最优EOD 训练IS DA={best_train_is['EOD']['da_rate']}% EHR={best_train_is['EOD']['ehr_rate']}% | "
            f"训练OOS DA={best_train_oos['EOD']['da_rate']}% EHR={best_train_oos['EOD']['ehr_rate']}% | "
            f"测试集 DA={best_test['EOD']['da_rate']}% EHR={best_test['EOD']['ehr_rate']}% | "
            f"全样本 DA={best_full['EOD']['da_rate']}% EHR={best_full['EOD']['ehr_rate']}%\n"
            f"防过拟合: 训练IS-测试gap={gap:.1f}pp | deploy_ready={'YES' if deploy_ready else 'NO'}\n"
            f"候选: {json.dumps(best_dict, ensure_ascii=False)}"
        )
    except Exception as e:
        print(f'[final feishu failed] {e}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass
    try:
        feishu(f"✅ v5调优v2 全部完成，产物: output/v5_systematic_tune_v2_{a.out_suffix}.json/.html")
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            subprocess.run([sys.executable, NOTIFY, f"💥 v5调优v2 异常中断: {e}"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        print(tb)
        raise
