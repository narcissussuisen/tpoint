# -*- coding: utf-8 -*-
"""
p2_generalization.py —— P2 个股泛化复现：逐标 DET v2.0 分布与泛化性分析

核心问题：DET v2.0 三轴评价体系在 187 标上是否「普适」（不是只靠少数活跃标的撑起）？
用户已定位：信号等权综合分 46.7% > 标的等权 42.8%，差异几乎全在 Precision（65.4% vs 59.0%），
Capture 无差异（71.4% vs 72.6%）→ 少数活跃标的贡献更高抓顶底精度。

本脚本输出：
  1. 逐标 Precision/Capture/综合分 + 信号数（活跃度代理）。
  2. 分布（中位数/p10/p25/p75/p90/均值/std）。
  3. 泛化率：Precision ≥ 门槛的标的占比（多数标的确有抓顶底能力）。
  4. 活跃度相关性：按信号数分桶看 Precision 分化，计算相关系数。
  5. 信号等权 vs 标的等权差异（P2 核心验收）。
"""
import sys, os, json, glob, datetime, subprocess

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import evaluate_signal_validity_v2 as ev
import numpy as np

HEARTBEAT = os.path.join(ev.OUT, 'p2_generalization_heartbeat.txt')
DONE_FLAG = os.path.join(ev.OUT, 'p2_generalization_done.flag')
NOTIFY = ev.NOTIFY


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


def _rate(ok, tot):
    return (ok / tot) if tot else None


def _dist(vals):
    """返回分布摘要（vals 为 list of float，非 None）。"""
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    a = np.asarray(vals)
    return dict(n=len(vals), median=float(np.median(a)), mean=float(a.mean()),
                p10=float(np.percentile(a, 10)), p25=float(np.percentile(a, 25)),
                p75=float(np.percentile(a, 75)), p90=float(np.percentile(a, 90)),
                std=float(a.std()))


def _frac_above(vals, thresh):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return float(np.mean([1 if v >= thresh else 0 for v in vals]))


def main():
    files = sorted(glob.glob(f'{ev.DATA_DIR}/*_1m.csv'))
    feishu(f'🚀 P2 个股泛化分析启动：{len(files)} 标的，逐标 DET v2.0 分布')

    rows = []
    # 信号等权累加器（原始计数，供 signal_weighted 对比）
    sw_cnt = dict(b_hit=0, b_tot=0, s_hit=0, s_tot=0, b_cap=0, s_cap=0, b_eod=0, s_eod=0)
    for idx, path in enumerate(files):
        sym = os.path.basename(path).replace('_1m.csv', '')
        r = ev.evaluate_symbol(sym, path)
        if 'error' in r:
            continue
        eod = r['acc']['EOD']
        n_sig = eod['b_tot'] + eod['s_tot']
        prec_b = _rate(r['prec']['b_hit'], r['prec']['b_tot'])
        prec_s = _rate(r['prec']['s_hit'], r['prec']['s_tot'])
        sw_cnt['b_hit'] += r['prec']['b_hit']; sw_cnt['b_tot'] += r['prec']['b_tot']
        sw_cnt['s_hit'] += r['prec']['s_hit']; sw_cnt['s_tot'] += r['prec']['s_tot']
        sw_cnt['b_cap'] += eod['b_cap']; sw_cnt['s_cap'] += eod['s_cap']
        sw_cnt['b_eod'] += eod['b_tot']; sw_cnt['s_eod'] += eod['s_tot']
        cap_b = _rate(eod['b_cap'], eod['b_tot'])
        cap_s = _rate(eod['s_cap'], eod['s_tot'])
        att_b = _rate(eod['b_att'], eod['b_tot'])
        att_s = _rate(eod['s_att'], eod['s_tot'])
        tep_b = _rate(eod['b_tep'], eod['b_tot'])
        tep_s = _rate(eod['s_tep'], eod['s_tot'])
        comp_b = (prec_b * cap_b) if (prec_b is not None and cap_b is not None) else None
        comp_s = (prec_s * cap_s) if (prec_s is not None and cap_s is not None) else None
        rows.append(dict(sym=sym, days=r['days'], n_sig=n_sig,
                         prec_b=prec_b, prec_s=prec_s, cap_b=cap_b, cap_s=cap_s,
                         att_b=att_b, att_s=att_s, tep_b=tep_b, tep_s=tep_s,
                         comp_b=comp_b, comp_s=comp_s))
        if (idx + 1) % 50 == 0:
            feishu(f'⏳ P2 进度 {idx+1}/{len(files)} 标的')

    # —— 分布 ——
    def _col(key):
        return [r[key] for r in rows]

    dist = dict(
        prec_b=_dist(_col('prec_b')), prec_s=_dist(_col('prec_s')),
        cap_b=_dist(_col('cap_b')), cap_s=_dist(_col('cap_s')),
        comp_b=_dist(_col('comp_b')), comp_s=_dist(_col('comp_s')),
    )

    # —— 泛化率（Precision 门槛）——
    gen_rate = dict(
        prec_b_ge50=_frac_above(_col('prec_b'), 0.50),
        prec_b_ge55=_frac_above(_col('prec_b'), 0.55),
        prec_s_ge50=_frac_above(_col('prec_s'), 0.50),
        prec_s_ge55=_frac_above(_col('prec_s'), 0.55),
        comp_b_ge40=_frac_above(_col('comp_b'), 0.40),
        comp_b_ge50=_frac_above(_col('comp_b'), 0.50),
    )

    # —— 活跃度相关性：按信号数分桶 ——
    sigs = np.asarray([r['n_sig'] for r in rows])
    precs = np.asarray([r['prec_b'] for r in rows if r['prec_b'] is not None])
    # 用信号数分位数分 4 桶，看每桶 prec_b 均值
    qs = [0, 25, 50, 75, 100]
    bucket_edges = np.percentile(sigs, qs)
    buckets = []
    for i in range(4):
        lo, hi = bucket_edges[i], bucket_edges[i + 1]
        idxs = [j for j in range(len(rows)) if lo <= sigs[j] <= hi]
        b_prec = [rows[j]['prec_b'] for j in idxs if rows[j]['prec_b'] is not None]
        b_comp = [rows[j]['comp_b'] for j in idxs if rows[j]['comp_b'] is not None]
        buckets.append(dict(
            q=f'Q{i+1}', n_sig_range=[round(lo, 1), round(hi, 1)],
            n_syms=len(idxs),
            prec_b_mean=(float(np.mean(b_prec)) if b_prec else None),
            comp_b_mean=(float(np.mean(b_comp)) if b_comp else None),
        ))
    # 相关系数（信号数 vs prec_b，Spearman 用 rank）
    valid = [(r['n_sig'], r['prec_b']) for r in rows if r['prec_b'] is not None]
    if len(valid) > 2:
        xs = np.asarray([v[0] for v in valid])
        ys = np.asarray([v[1] for v in valid])
        corr_pearson = float(np.corrcoef(xs, ys)[0, 1]) if xs.std() > 0 and ys.std() > 0 else None
        corr_spearman = float(np.corrcoef(np.argsort(np.argsort(xs)), np.argsort(np.argsort(ys)))[0, 1])
    else:
        corr_pearson = corr_spearman = None

    # —— 信号等权 vs 标的等权（P2 核心验收）——
    def _sw(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return float(np.mean(vals)) if vals else None
    sw = dict(prec_b=_sw('prec_b'), prec_s=_sw('prec_s'), cap_b=_sw('cap_b'), cap_s=_sw('cap_s'),
              comp_b=_sw('comp_b'), comp_s=_sw('comp_s'))
    # 信号等权（原始计数 sum/sum）
    sig_prec_b = _rate(sw_cnt['b_hit'], sw_cnt['b_tot'])
    sig_prec_s = _rate(sw_cnt['s_hit'], sw_cnt['s_tot'])
    sig_cap_b = _rate(sw_cnt['b_cap'], sw_cnt['b_eod'])
    sig_cap_s = _rate(sw_cnt['s_cap'], sw_cnt['s_eod'])
    sig_comp_b = (sig_prec_b * sig_cap_b) if (sig_prec_b is not None and sig_cap_b is not None) else None
    sig_comp_s = (sig_prec_s * sig_cap_s) if (sig_prec_s is not None and sig_cap_s is not None) else None
    signal_weighted = dict(prec_b=sig_prec_b, prec_s=sig_prec_s, cap_b=sig_cap_b, cap_s=sig_cap_s,
                           comp_b=sig_comp_b, comp_s=sig_comp_s)
    gap = dict(
        prec_b_pp=round((sig_prec_b - sw['prec_b']) * 100, 2) if (sig_prec_b and sw['prec_b']) else None,
        comp_b_pp=round((sig_comp_b - sw['comp_b']) * 100, 2) if (sig_comp_b and sw['comp_b']) else None,
    )

    out = dict(
        meta=dict(n_syms=len(rows), date=datetime.date.today().strftime('%Y-%m-%d'),
                  note='P2 个股泛化复现：逐标 DET v2.0 分布'),
        distribution=dist,
        generalization_rate=gen_rate,
        activity_buckets=buckets,
        activity_corr=dict(pearson=corr_pearson, spearman=corr_spearman, note='n_sig vs prec_b'),
        signal_weighted=signal_weighted,
        symbol_weighted=sw,
        gap_signal_vs_symbol=gap,
        symbols=rows,   # 逐标明细（供定位异常标的）
    )
    fn = 'p2_generalization_2026-08-23.json'
    fp = os.path.join(ev.OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print(f'\n=== P2 个股泛化（{len(rows)} 标的）===')
    print(f'Precision B 分布: 中位={dist["prec_b"]["median"]*100:.1f}% p25={dist["prec_b"]["p25"]*100:.1f}% '
          f'p75={dist["prec_b"]["p75"]*100:.1f}% std={dist["prec_b"]["std"]*100:.1f}pp')
    print(f'泛化率: prec_B≥50% = {gen_rate["prec_b_ge50"]*100:.1f}% 标的 | '
          f'prec_B≥55% = {gen_rate["prec_b_ge55"]*100:.1f}% 标的')
    print(f'活跃度分桶 prec_B 均值: ' + ' | '.join(
        f'Q{i+1}[{b["n_sig_range"][0]:.0f}-{b["n_sig_range"][1]:.0f}信号]={b["prec_b_mean"]*100:.1f}%'
        if b["prec_b_mean"] else f'Q{i+1}=n/a' for i, b in enumerate(buckets)))
    print(f'信号数 vs prec_B 相关: Pearson={corr_pearson:.3f} Spearman={corr_spearman:.3f}')
    print(f'标的等权: prec_B={sw["prec_b"]*100:.1f}% cap_B={sw["cap_b"]*100:.1f}% comp_B={sw["comp_b"]*100:.1f}%')
    print(f'信号等权: prec_B={sig_prec_b*100:.1f}% cap_B={sig_cap_b*100:.1f}% comp_B={sig_comp_b*100:.1f}%')
    print(f'信号-标的 gap: prec_B={gap["prec_b_pp"]:.2f}pp comp_B={gap["comp_b_pp"]:.2f}pp')
    print(f'JSON -> {fp}')

    feishu(f'✅ P2 个股泛化分析完成（{len(rows)}标）：\n'
           f'Precision B 中位={dist["prec_b"]["median"]*100:.1f}% 标的等权={sw["prec_b"]*100:.1f}%/信号等权={sig_prec_b*100:.1f}%；\n'
           f'泛化率 prec_B≥50%={gen_rate["prec_b_ge50"]*100:.1f}% 标的；活跃度相关 Spearman={corr_spearman:.3f}\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 P2 个股泛化分析异常: {e}')
        raise
