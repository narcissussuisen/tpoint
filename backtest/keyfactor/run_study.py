#!/usr/bin/env python3
"""
Phase 3-5 编排器 (autoresearch 轻量 harness 形态A):
  输入: 一批 1m CSV (默认 keyfactor_data/1m/, 可用 --indir 指向 seed 目录做验证)
  流程:
    Phase 2 标准化: load_1m (会话感知留待, 前向收益按 bar 计)
    Phase 3 归因: 全开配置跑引擎 -> 每信号 factors + 前向收益 -> 因子边际/方向正确率
    Phase 4 消融: 关 gravity / vol_div / macd_div 各一次 -> 指标(符号调整后 skill) 下降幅度 = 因子重要性
    Phase 5 阈值扫描: VWAP_DEV(0.6/0.8/1.0) × VOL_EXPAND/SHRINK × RESONANCE(1/2/3) -> 找灵敏/鲁棒参数
  输出: keyfactor_results.csv (逐配置) + keyfactor_summary.json (排名/结论)
  飞书里程碑在调用方(download 完成后跑此脚本, 50/75/100% 由外层推)。

  skill 定义 (符号调整后前向收益): B信号 fwd>0 为好, S信号 fwd<0 为好
    skill = fwd if type=='B' else -fwd ; 越高=信号质量越好
"""
import sys, os, glob, json, argparse
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kf_utils as K
import miji_engine as ME

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'keyfactor_data')
DEF_IN = os.path.join(DATA, '1m')
OUT_CSV = os.path.join(DATA, 'keyfactor_results.csv')
OUT_JSON = os.path.join(DATA, 'keyfactor_summary.json')
HORIZONS = [6, 12, 24]

# --- P5 跨时段 OOS (新增, 2026-07-17) ---
# 训练/测试按"同票不同时段"切分: 前 OOS_SPLIT 段训练(样本内), 后段测试(OOS)。
# 配置冻结(P0-P4 baked), 检测跑全序列(指标需 lookback), 仅按信号 idx 归因,
# 泄漏守卫: 训练信号要求 idx+24<=split; 测试信号 idx>=split。
OOS_SPLIT = 0.66
OUT_OOS_CSV = os.path.join(DATA, 'keyfactor_oos_time.csv')
OUT_OOS_JSON = os.path.join(DATA, 'keyfactor_oos_time.json')
# --- 逐日修正版输出(不覆盖旧结果, 便于对比) ---
OUT_CSV_DAILY = os.path.join(DATA, 'keyfactor_results_daily.csv')
OUT_JSON_DAILY = os.path.join(DATA, 'keyfactor_results_daily.json')
OUT_OOS_CSV_DAILY = os.path.join(DATA, 'keyfactor_oos_time_daily.csv')
OUT_OOS_JSON_DAILY = os.path.join(DATA, 'keyfactor_oos_time_daily.json')
# --- OOS 归因 (RESONANCE=1 单因子 + 时段桶 + 分布, 2026-07-17) ---
OUT_OOS_ATTR_CSV_DAILY = os.path.join(DATA, 'keyfactor_oos_attribution_daily.csv')
OUT_OOS_ATTR_JSON_DAILY = os.path.join(DATA, 'keyfactor_oos_attribution_daily.json')

class Patch:
    """临时 monkeypatch miji_engine 模块常量, 退出即还原。"""
    def __init__(self, **kv):
        self.kv = kv; self.saved = {}
    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = getattr(ME, k, None)
            setattr(ME, k, v)
        return self
    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(ME, k, v)

def precompute(files):
    """一次算指标(阈值无关), 缓存 data/pc/df。Phase5 阈值扫描只重跑检测。

    竞态容错(B 决策, 2026-07-17): 下载引擎与 study 并发写/读同一 1m/ 目录时,
    个别文件可能在 glob 之后、读取之前被引擎改名/删除, 或正处于写入中途(半截 CSV)。
    此处对单个文件读取失败做 skip, 保证 study 在"边下载边跑"下不崩溃。
    """
    cache = []
    skipped = 0
    for fp in files:
        try:
            df = K.load_1m(fp)
        except (FileNotFoundError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
            skipped += 1
            if skipped <= 20:
                print(f"[precompute] skip {os.path.basename(fp)}: {type(e).__name__}", flush=True)
            continue
        except Exception as e:
            skipped += 1
            if skipped <= 20:
                print(f"[precompute] skip {os.path.basename(fp)}: {type(e).__name__}: {e}", flush=True)
            continue
        o = df['open'].values.astype(float)
        h = df['high'].values.astype(float)
        lo = df['low'].values.astype(float)
        c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float)
        has_vol = float(np.sum(v)) > 0
        pc = float(c[0]) if len(c) > 0 else 0.0
        data = ME.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
        cache.append({'df': df, 'data': data, 'pc': pc, 'fp': fp})
    return cache

# ========== 逐日检测 (B 修正: 日内/隔日语义, 2026-07-17) ==========
# 原 precompute 把 6 个月 1m 当一条连续数组喂 detect_miji_signals:
#   - compute_vwap 跨日累积 -> 几天后 VWAP 拉平为常数 -> dev_pct 趋零 -> 重力信号消失
#   - max_b/max_s=12 为全序列限额 -> 每票仅 ~12 个信号, 且全挤在前几天
#   => 原 P0-P5 的 +4.89% 是在"每只票仅头几天有信号"的非代表性切片上算的。
# 修正: 按 trade_date 切分, 每交易日独立跑检测(VWAP 日内重置 + 昨收按日 + max_b/max_s 按日),
#   信号 idx 重映射回整段位置(供 fwd_rets/build_attr_rows 复用)。这才符合"miji 日内/隔日使用"。

def _segment_days(df):
    """返回 [(start, end), ...] 按 trade_date 分组的连续区间(时间升序)。
    无 trade_date 列时整段作为一日。"""
    if 'trade_date' not in df.columns:
        return [(0, len(df))]
    dates = df['trade_date'].tolist()
    groups = []
    prev = None; start = 0
    for i, d in enumerate(dates):
        if prev is None:
            prev = d
        if d != prev:
            groups.append((start, i))
            start = i; prev = d
    groups.append((start, len(df)))
    return groups

def detect_miji_signals_daily(df, enable=(True, True, True), min_resonance=2,
                               b_trend_filter=False, allow_reverse=True,
                               require_macd=False):
    """逐日检测: 每交易日 VWAP 重置 + 昨收按上一日收盘 + max_b/max_s 按日。
    返回信号 list, idx 已映射到整段 df 位置(供 fwd_rets 用)。
    注: MACD/ATR 也按日重算(日内指标语义); 每日起始 session 态清空(pos_ctx=0)。
    """
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    n = len(c)
    if n == 0:
        return []
    groups = _segment_days(df)
    ordered = sorted(groups, key=lambda x: x[0])   # 按时间顺序
    prev_close = None
    all_sigs = []
    for (gs, ge) in ordered:
        # 当日昨收: 上一日最后一根收盘(首日无则用当日首根近似)
        pc = float(prev_close) if (prev_close is not None and prev_close > 0) else float(c[gs])
        o_d = o[gs:ge]; h_d = h[gs:ge]; lo_d = lo[gs:ge]; c_d = c[gs:ge]
        v_d = v[gs:ge] if v is not None else None
        has_vol = v_d is not None and float(np.sum(v_d)) > 0
        data = ME.compute_miji_indicators(o_d, h_d, lo_d, c_d, v_d, pc, has_vol=has_vol)
        sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_resonance,
                                       b_trend_filter=b_trend_filter, allow_reverse=allow_reverse,
                                       require_macd=require_macd, enable=enable)
        for s in sigs:
            s2 = dict(s)
            s2['idx'] = gs + s['idx']   # 重映射回整段
            all_sigs.append(s2)
        prev_close = float(c[ge - 1])
    return all_sigs

def attr_for_config(cache, enable, min_res, daily=False, require_macd=False, **monkey):
    with Patch(**monkey):
        rows = []
        nsig = 0
        for item in cache:
            df = item['df']
            if daily:
                sigs = detect_miji_signals_daily(df, enable=enable, min_resonance=min_res,
                                                  require_macd=require_macd)
            else:
                data = item['data']; pc = item['pc']
                sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_res,
                                              b_trend_filter=False, allow_reverse=True,
                                              require_macd=require_macd, enable=enable)
            sigs = K.fwd_rets(df, sigs)
            rows.extend(K.build_attr_rows(sigs, df))
            nsig += len(sigs)
    return rows, nsig

def skill_stats(rows):
    out = {}
    for hh in HORIZONS:
        vals = []
        for r in rows:
            f = r[f'fwd{hh}']
            if f is None:
                continue
            vals.append(f if r['type'] == 'B' else -f)
        # 空 vals (某配置在某 horizon 无有效前向收益) -> 均值记 0.0 (非 None),
        # 避免下游打印/聚合 TypeErr。语义: 无有效信号=0 前向收益。
        out[hh] = (float(np.mean(vals)), len(vals)) if vals else (0.0, 0)
    return out

def factor_marginal(rows):
    """每个因子(在其方向)的边际: 参与 vs 不参与的 24根 skill 差。"""
    res = {}
    for fac in ('g', 'vd', 'md'):
        on24, off24 = [], []
        for r in rows:
            f = r['fwd24']
            if f is None:
                continue
            sk = f if r['type'] == 'B' else -f
            if r[fac] == 1:
                on24.append(sk)
            else:
                off24.append(sk)
        res[fac] = {
            'n_on': len(on24), 'n_off': len(off24),
            'skill24_on': float(np.mean(on24)) if on24 else None,
            'skill24_off': float(np.mean(off24)) if off24 else None,
        }
    return res

def load_cache(indir):
    """glob 1m 文件 + 预计算指标 (供 run_all / 外部编排调用)。"""
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    if not files:
        return []
    print(f"=== 加载 {len(files)} 只 1m (indir={indir}) ===")
    cache = precompute(files)
    print(f"  指标预计算完成 (阈值无关, 缓存 {len(cache)} 只)")
    return cache

def phase3(cache, daily=False, require_macd=False):
    """Phase 3 基线 + 归因。返回 (base_rows, n_base, base_skill, marginal)。"""
    tag = ' [daily]' if daily else ''
    tag += ' [macd-req]' if require_macd else ''
    base_rows, n_base = attr_for_config(cache, (True, True, True), 2, daily=daily,
                                         require_macd=require_macd)
    base_skill = skill_stats(base_rows)
    marginal = factor_marginal(base_rows)
    print(f"  [Phase3{tag}] baseline 信号数={n_base}  skill(6/12/24)= "
          f"{base_skill[6][0]:+.4f}/{base_skill[12][0]:+.4f}/{base_skill[24][0]:+.4f}%")
    return base_rows, n_base, base_skill, marginal

def phase4(cache, daily=False, require_macd=False):
    """Phase 4 消融: 关 gravity/vol_div/macd_div。返回 abl dict。"""
    tag = ' [daily]' if daily else ''
    tag += ' [macd-req]' if require_macd else ''
    abl = {}
    for name, en in [('all', (True, True, True)),
                     ('no_gravity', (False, True, True)),
                     ('no_vol_div', (True, False, True)),
                     ('no_macd_div', (True, True, False))]:
        r, n = attr_for_config(cache, en, 2, daily=daily, require_macd=require_macd)
        sk = skill_stats(r)
        abl[name] = {'n': n, 'skill': {h: sk[h][0] for h in HORIZONS}}
        print(f"  [Phase4{tag}] 消融 {name:12s} n={n:5d} skill24={sk[24][0]:+.4f}%")
    return abl

def phase5(cache, daily=False, require_macd=False):
    """Phase 5 阈值扫描: VWAP_DEV×VOL_EXPAND×VOL_SHRINK + RESONANCE。返回 sweep_df。"""
    tag = ' [daily]' if daily else ''
    tag += ' [macd-req]' if require_macd else ''
    sweep = []
    for dev in (0.6, 0.8, 1.0):
        for ve in (1.1, 1.2, 1.3):
            for vs in (0.7, 0.8, 0.9):
                r, n = attr_for_config(cache, (True, True, True), 2, daily=daily,
                                       require_macd=require_macd,
                                       VWAP_DEV_BUY=dev, VWAP_DEV_SELL=dev,
                                       VOL_EXPAND_RATIO=ve, VOL_SHRINK_RATIO=vs)
                sk = skill_stats(r)
                sweep.append({'VWAP_DEV': dev, 'VOL_EXPAND': ve, 'VOL_SHRINK': vs,
                             'n': n,
                             'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    for mr in (1, 2, 3):
        r, n = attr_for_config(cache, (True, True, True), mr, daily=daily,
                               require_macd=require_macd)
        sk = skill_stats(r)
        sweep.append({'RESONANCE': mr, 'n': n,
                      'skill6': sk[6][0], 'skill12': sk[12][0], 'skill24': sk[24][0]})
    print(f"  [Phase5{tag}] 阈值扫描 {len(sweep)} 组完成")
    return pd.DataFrame(sweep)

def write_outputs(cache, base_rows, n_base, base_skill, marginal, abl, sweep_df,
                 seedtest=False, out_csv=OUT_CSV, out_json=OUT_JSON):
    sweep_df.to_csv(out_csv, index=False, encoding='utf-8-sig')
    imp = {}
    for fac, key in [('gravity', 'no_gravity'), ('vol_div', 'no_vol_div'), ('macd_div', 'no_macd_div')]:
        drop = (abl['all']['skill'][24] or 0) - (abl[key]['skill'][24] or 0)
        imp[fac] = round(drop, 5)
    ranking = sorted(imp.items(), key=lambda x: -abs(x[1]))
    summary = {
        'n_symbols': len(cache),
        'n_signals_baseline': n_base,
        'baseline_skill': {h: base_skill[h][0] for h in HORIZONS},
        'ablation': abl,
        'factor_importance_drop_skill24': imp,
        'factor_ranking': [k for k, _ in ranking],
        'marginal_24': marginal,
        'threshold_sweep_best': {
            'vwap_best': sweep_df[sweep_df['VWAP_DEV'].notna()].loc[
                sweep_df[sweep_df['VWAP_DEV'].notna()]['skill24'].idxmax()].to_dict()
                if sweep_df['VWAP_DEV'].notna().any() else None,
            'resonance_best': sweep_df[sweep_df['RESONANCE'].notna()].loc[
                sweep_df[sweep_df['RESONANCE'].notna()]['skill24'].idxmax()].to_dict()
                if sweep_df['RESONANCE'].notna().any() else None,
        },
        'seedtest': seedtest,
    }
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n=== 因子重要性 (关掉后 skill24 变化, 单位%) ===")
    for k, v in ranking:
        print(f"  {k:12s} drop={v:+.5f}")
    print(f"\n  落地: {out_csv}")
    print(f"  落地: {out_json}")

def _attr_rows_one(item, min_res=2, enable=(True, True, True), daily=False, require_macd=False):
    """单只票: 冻结配置跑检测 -> 含 idx + fwd 的 rows。"""
    df = item['df']
    if daily:
        sigs = detect_miji_signals_daily(df, enable=enable, min_resonance=min_res,
                                          require_macd=require_macd)
    else:
        data, pc = item['data'], item['pc']
        sigs = ME.detect_miji_signals(data, pc, start_idx=2, min_resonance=min_res,
                                      b_trend_filter=False, allow_reverse=True,
                                      require_macd=require_macd, enable=enable)
    sigs = K.fwd_rets(df, sigs)
    return K.build_attr_rows(sigs, df)

def phase_oos_time(cache, split_ratio=OOS_SPLIT, daily=False, require_macd=False):
    """跨时段 OOS: 每只票前 split_ratio 为训练(样本内), 后为测试(OOS)。
    返回 (per_stock_list, agg_dict)。daily=True 时检测用逐日路径。"""
    per, sk_tr, sk_te, n_pos_te = [], [], [], 0
    for item in cache:
        df = item['df']; N = len(df)
        if N < 200:
            continue
        split = int(split_ratio * N)
        if split < 50 or (N - split) < 50:
            continue
        rows = _attr_rows_one(item, daily=daily, require_macd=require_macd)
        train = [r for r in rows if r['idx'] < split and (r['idx'] + 24) <= split]
        test = [r for r in rows if r['idx'] >= split]
        tr = skill_stats(train)[24]
        te = skill_stats(test)[24]
        sym = os.path.basename(item.get('fp', '?')).replace('_1m.csv', '')
        per.append({'sym': sym, 'n_bars': N, 'split': split,
                    'n_train': tr[1],
                    'skill24_train': round(tr[0], 4) if tr[0] is not None else None,
                    'n_test': te[1],
                    'skill24_test': round(te[0], 4) if te[0] is not None else None})
        if tr[0] is not None:
            sk_tr.append(tr[0])
        if te[0] is not None:
            sk_te.append(te[0])
            if te[0] > 0:
                n_pos_te += 1
    agg = {
        'split_ratio': split_ratio,
        'n_stocks': len(per),
        'mean_skill24_train': round(float(np.mean(sk_tr)), 4) if sk_tr else None,
        'mean_skill24_test': round(float(np.mean(sk_te)), 4) if sk_te else None,
        'n_stocks_with_test': len(sk_te),
        'frac_test_positive': round(n_pos_te / len(sk_te), 3) if sk_te else None,
        'config': f'frozen P0-P4 (RESONANCE=2, vol_div off, VWAP_DEV=0.6, macd_div swap, require_macd={require_macd})',
    }
    if sk_tr and sk_te:
        agg['delta_test_minus_train'] = round(float(np.mean(sk_te)) - float(np.mean(sk_tr)), 4)
    print(f"  [OOS-time] {len(per)} 只: train skill24={agg['mean_skill24_train']} "
          f"test(OOS) skill24={agg['mean_skill24_test']} "
          f"frac_test_positive={agg['frac_test_positive']}")
    return per, agg

def write_oos_time(per, agg, out_csv=OUT_OOS_CSV, out_json=OUT_OOS_JSON):
    pd.DataFrame(per).to_csv(out_csv, index=False, encoding='utf-8-sig')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(agg, f, ensure_ascii=False, indent=2)
    print(f"  落地: {out_csv}")
    print(f"  落地: {out_json}")


# ========== OOS 归因 (RESONANCE=1 单因子 + 时段桶 + 分布, 2026-07-17) ==========
# 目的: 找跨时段OOS退化(-0.35%逐股均值)的病灶——哪个因子/组合/时段在拖累。
# 方法: 用 min_resonance=1 跑(让单因子信号通过), vol_div 已禁用故只剩 gravity/macd,
#       按 (gravity,macd) 组合分组算 test skill24; test 段再切早/晚桶查 regime 漂移;
#       输出每股 test skill 分布(p10..p90 + n_neg/n_neg2/n_pos2)。
def phase_oos_attribution(cache, split_ratio=OOS_SPLIT, daily=False):
    per = []
    g_only_te, m_only_te, both_te = [], [], []    # test 段按因子组的 skill 值(跨股池化)
    g_only_tr, m_only_tr, both_tr = [], [], []    # train 段对照
    te_early_vals, te_late_vals = [], []           # 每股 test 早/晚 skill(池化)

    def _sk(rs):
        v = [(r['fwd24'] if r['type'] == 'B' else -r['fwd24'])
             for r in rs if r.get('fwd24') is not None]
        return (float(np.mean(v)), len(v)) if v else (None, 0)

    for item in cache:
        df = item['df']; N = len(df)
        if N < 200:
            continue
        split = int(split_ratio * N)
        if split < 50 or (N - split) < 50:
            continue
        # RESONANCE=1: 单因子信号也通过 (归因用, 非冻结配置)
        rows = _attr_rows_one(item, min_res=1, enable=(True, True, True), daily=daily)
        train = [r for r in rows if r['idx'] < split and (r['idx'] + 24) <= split]
        test = [r for r in rows if r['idx'] >= split]
        mid = split + (N - split) // 2
        tr = _sk(train); te = _sk(test)
        te_e = _sk([r for r in test if r['idx'] < mid])
        te_l = _sk([r for r in test if r['idx'] >= mid])
        sym = os.path.basename(item.get('fp', '?')).replace('_1m.csv', '')
        per.append({'sym': sym, 'n_bars': N, 'split': split,
                    'n_train': tr[1],
                    'skill24_train': round(tr[0], 4) if tr[0] is not None else None,
                    'n_test': te[1],
                    'skill24_test': round(te[0], 4) if te[0] is not None else None,
                    'skill24_test_early': round(te_e[0], 4) if te_e[0] is not None else None,
                    'skill24_test_late': round(te_l[0], 4) if te_l[0] is not None else None})
        for r in test:
            if r.get('fwd24') is None:
                continue
            sk = r['fwd24'] if r['type'] == 'B' else -r['fwd24']
            if r['g'] == 1 and r['md'] == 0:
                g_only_te.append(sk)
            elif r['g'] == 0 and r['md'] == 1:
                m_only_te.append(sk)
            elif r['g'] == 1 and r['md'] == 1:
                both_te.append(sk)
        for r in train:
            if r.get('fwd24') is None:
                continue
            sk = r['fwd24'] if r['type'] == 'B' else -r['fwd24']
            if r['g'] == 1 and r['md'] == 0:
                g_only_tr.append(sk)
            elif r['g'] == 0 and r['md'] == 1:
                m_only_tr.append(sk)
            elif r['g'] == 1 and r['md'] == 1:
                both_tr.append(sk)
        if te_e[0] is not None:
            te_early_vals.append(te_e[0])
        if te_l[0] is not None:
            te_late_vals.append(te_l[0])

    te_skills = [p['skill24_test'] for p in per if p['skill24_test'] is not None]

    def _pct(q):
        if not te_skills:
            return None
        s = sorted(te_skills); k = (len(s) - 1) * q; f = int(k)
        c = min(f + 1, len(s) - 1)
        return round(s[f] + (s[c] - s[f]) * (k - f), 4)

    def _gmean(v):
        return round(float(np.mean(v)), 4) if v else None

    agg = {
        'config': 'RESONANCE=1 (single-factor pass) for attribution; vol_div off',
        'split_ratio': split_ratio, 'n_stocks': len(per),
        'mean_skill24_test': _gmean(te_skills),
        'median_skill24_test': round(float(np.median(te_skills)), 4) if te_skills else None,
        'mean_skill24_test_early': _gmean(te_early_vals),
        'mean_skill24_test_late': _gmean(te_late_vals),
        'factor_group_test': {
            'gravity_only': {'n': len(g_only_te), 'skill24': _gmean(g_only_te)},
            'macd_only':    {'n': len(m_only_te), 'skill24': _gmean(m_only_te)},
            'both':         {'n': len(both_te), 'skill24': _gmean(both_te)},
        },
        'factor_group_train': {
            'gravity_only': {'n': len(g_only_tr), 'skill24': _gmean(g_only_tr)},
            'macd_only':    {'n': len(m_only_tr), 'skill24': _gmean(m_only_tr)},
            'both':         {'n': len(both_tr), 'skill24': _gmean(both_tr)},
        },
        'distribution_test': {
            'p10': _pct(0.1), 'p25': _pct(0.25), 'p50': _pct(0.5),
            'p75': _pct(0.75), 'p90': _pct(0.9),
            'n_neg': int(sum(1 for x in te_skills if x < 0)),
            'n_neg2': int(sum(1 for x in te_skills if x < -0.02)),
            'n_pos2': int(sum(1 for x in te_skills if x > 0.02)),
        },
    }
    print(f"  [OOS-attrib] {len(per)}只 RESONANCE=1: test mean={agg['mean_skill24_test']} "
          f"median={agg['median_skill24_test']}")
    print(f"    因子组(test): gravity_only={agg['factor_group_test']['gravity_only']} "
          f"macd_only={agg['factor_group_test']['macd_only']} "
          f"both={agg['factor_group_test']['both']}")
    print(f"    因子组(train): gravity_only={agg['factor_group_train']['gravity_only']} "
          f"macd_only={agg['factor_group_train']['macd_only']} both={agg['factor_group_train']['both']}")
    print(f"    时段桶(test): early={agg['mean_skill24_test_early']} late={agg['mean_skill24_test_late']}")
    print(f"    分布(test): {agg['distribution_test']}")
    return per, agg


def write_oos_attribution(per, agg, out_csv=OUT_OOS_ATTR_CSV_DAILY, out_json=OUT_OOS_ATTR_JSON_DAILY):
    pd.DataFrame(per).to_csv(out_csv, index=False, encoding='utf-8-sig')
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(agg, f, ensure_ascii=False, indent=2)
    print(f"  落地: {out_csv}")
    print(f"  落地: {out_json}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--indir', default=DEF_IN)
    ap.add_argument('--seedtest', action='store_true', help='指向 backtest_data 的 7 seed 做验证')
    ap.add_argument('--oos-time', action='store_true',
                    help='P5 跨时段 OOS: 前66%%训练/后34%%测试, 配置冻结, 只度量不调参')
    ap.add_argument('--daily', action='store_true',
                    help='B修正: 逐日检测(VWAP日内重置+昨收按日+max按日), 输出 _daily 文件, 不覆盖旧结果')
    ap.add_argument('--oos-attrib', action='store_true',
                    help='OOS归因: RESONANCE=1单因子+时段桶+分布, 找退化病灶')
    ap.add_argument('--require-macd', action='store_true',
                    help='T1.5: macd-required门控(B需macd投票,排除gravity-only), 生产默认')
    args = ap.parse_args()
    indir = os.path.join(HERE, '..', '..', 'backtest', 'backtest_data') if args.seedtest else args.indir
    files = sorted(glob.glob(os.path.join(indir, '*_1m.csv')))
    if not files:
        print(f"⚠️ {indir} 无 1m 文件")
        return
    cache = load_cache(indir)
    if args.oos_time:
        per, agg = phase_oos_time(cache, daily=args.daily, require_macd=args.require_macd)
        write_oos_time(per, agg,
                       out_csv=OUT_OOS_CSV_DAILY if args.daily else OUT_OOS_CSV,
                       out_json=OUT_OOS_JSON_DAILY if args.daily else OUT_OOS_JSON)
        return
    if args.oos_attrib:
        per, agg = phase_oos_attribution(cache, daily=args.daily)
        write_oos_attribution(per, agg)
        return
    base_rows, n_base, base_skill, marginal = phase3(cache, daily=args.daily,
                                                       require_macd=args.require_macd)
    abl = phase4(cache, daily=args.daily, require_macd=args.require_macd)
    sweep_df = phase5(cache, daily=args.daily, require_macd=args.require_macd)
    write_outputs(cache, base_rows, n_base, base_skill, marginal, abl, sweep_df,
                 seedtest=args.seedtest,
                 out_csv=OUT_CSV_DAILY if args.daily else OUT_CSV,
                 out_json=OUT_JSON_DAILY if args.daily else OUT_JSON)

if __name__ == '__main__':
    main()
