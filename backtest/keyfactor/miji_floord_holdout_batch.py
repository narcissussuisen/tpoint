# -*- coding: utf-8 -*-
"""批量盲 holdout: 在 in-sample 同一 61 天窗口上, 对一批先验确定的新鲜 T0 ETF/LOF
(调参时不可见) 跑 V15(原参数不重调) + V1 对照, 统计 PF>1 占比, 与 in-sample 8 只对比。

目的: 回答"edge 是否泛化到新标的" —— 若盲池绝大多数 PF<1, 则原 2 幸存者=过拟合/运气。
评估窗口严格 = in-sample 的 common(61天), 所有标的同日历比较。
"""
import os
import sys
import json

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from miji_floord_mtf import (build_symbol_series_mtf, simulate_overnight,
                             agg_trips, bucket_agg, TF_LISTS, CONFIGS, COST,
                             STOP_PCT, MIN_HOLD, MAX_HOLD_BARS)
from pivot_walkforward_p0 import all_dates

DATA_DIR = r'F:/keyfactor_data/1m'
OUT = os.path.join(ROOT, 'output', 'miji_floord_holdout_batch')
os.makedirs(OUT, exist_ok=True)

# in-sample 8 只(对照, 已知)
IN_SAMPLE = [
    ('600519.SH', '贵州茅台', 'bidirectional'),
    ('000001.SZ', '平安银行', 'bidirectional'),
    ('300750.SZ', '宁德时代', 'bidirectional'),
    ('600036.SH', '招商银行', 'bidirectional'),
    ('688347.SH', '华虹', 'bidirectional'),
    ('603659.SH', '璞泰来', 'bidirectional'),
    ('513310.SH', '中韩半导体ETF', 'longonly'),
    ('161129.SZ', '原油LOF', 'longonly'),
]

# 盲 holdout 池: 全部 T+0 ETF/LOF, 均不在 in-sample 8 中(513040/518880/159985 为缓存中未参与调参者)
BLIND_POOL = [
    ('159985.SZ', '豆粕ETF', 'longonly'),
    ('513040.SH', '跨境ETF', 'longonly'),
    ('518880.SH', '黄金ETF', 'longonly'),
    ('513100.SH', '纳指ETF', 'longonly'),
    ('513500.SH', '标普500ETF', 'longonly'),
    ('513090.SH', '中概互联ETF', 'longonly'),
    ('513030.SH', '德国30ETF', 'longonly'),
    ('513080.SH', '法国CAC40ETF', 'longonly'),
    ('513520.SH', '日经225ETF', 'longonly'),
    ('513300.SH', '纳斯达克100ETF', 'longonly'),
    ('513180.SH', '恒生科技ETF', 'longonly'),
    ('513660.SH', '恒生ETF', 'longonly'),
    ('513550.SH', '港股通50ETF', 'longonly'),
    ('513060.SH', '恒生医疗ETF', 'longonly'),
    ('513120.SH', '港股创新药ETF', 'longonly'),
    ('513010.SH', '恒生科技ETF2', 'longonly'),
    ('513000.SH', '日经225ETF2', 'longonly'),
    ('159920.SZ', '恒生ETF', 'longonly'),
    ('159941.SZ', '纳指ETF', 'longonly'),
    ('159605.SZ', '中概互联ETF', 'longonly'),
    ('159607.SZ', '中概互联ETF2', 'longonly'),
    ('159892.SZ', '恒生医药ETF', 'longonly'),
    ('518800.SH', '黄金ETF2', 'longonly'),
    ('518850.SH', '黄金ETF3', 'longonly'),
    ('518660.SH', '黄金ETF4', 'longonly'),
    ('159934.SZ', '黄金ETF5', 'longonly'),
    ('159937.SZ', '黄金ETF6', 'longonly'),
    ('159980.SZ', '有色ETF', 'longonly'),
    ('159981.SZ', '能源化工ETF', 'longonly'),
    ('162411.SZ', '华宝油气LOF', 'longonly'),
    ('164824.SZ', '印度基金LOF', 'longonly'),
    ('160723.SZ', '嘉实原油LOF', 'longonly'),
    ('501018.SH', '南方原油LOF', 'longonly'),
    ('161226.SZ', '国投白银LOF', 'longonly'),
    ('160140.SZ', '美国REITLOF', 'longonly'),
    ('161815.SZ', '抗通胀LOF', 'longonly'),
    ('165513.SZ', '信诚商品LOF', 'longonly'),
    ('160216.SZ', '国泰商品LOF', 'longonly'),
    ('160416.SZ', '华安石油LOF', 'longonly'),
    ('164701.SZ', '黄金LOF', 'longonly'),
]

MIN_DAYS = 30  # 窗口内至少 30 个交易日才纳入统计


def eval_sym(sym, name, model, common_window):
    avail = set(all_dates(sym))
    common = [d for d in common_window if d in avail]
    if len(common) < MIN_DAYS:
        return None
    tfs = sorted({tf for _, (tl, _) in TF_LISTS.items() for tf in tl})
    res = {}
    for cfg, (thr, basis) in CONFIGS.items():
        series = build_symbol_series_mtf(sym, common, thr, basis, tfs)
        res[cfg] = {}
        for vname, (gate, tf_list, lb) in [('V1', (False, [], 0)),
                                            ('V15', (True, [15], 240))]:
            trips, _, _ = simulate_overnight(
                series, model, gate, tf_list, lb, MIN_HOLD,
                STOP_PCT / 100.0, MAX_HOLD_BARS, COST[model])
            pnls = [t['pnl'] for t in trips]
            gw = sum(p for p in pnls if p > 0)
            gl = -sum(p for p in pnls if p < 0)
            n = len(trips)
            pf = (gw / gl) if gl > 0 else (99.0 if gw > 0 else 0.0)
            wr = (sum(1 for p in pnls if p > 0) / n * 100.0) if n else 0.0
            res[cfg][vname] = {
                'n': n,
                'pf': float(pf),
                'pf_inf': (gl <= 0 and gw > 0),
                'gw': float(gw), 'gl': float(gl),
                'net_pct': float(gw + gl),
                'win_rate': float(wr),
                'n_days': len(common),
            }
    return res


def main():
    # 严格复用 in-sample 评估窗口
    with open(os.path.join(ROOT, 'output', 'miji_floord_mtf', 'metrics.json'), encoding='utf-8') as f:
        ins_common = json.load(f)['common']
    print(f"评估窗口(严格=in-sample common): {len(ins_common)} 天 {ins_common[0]}..{ins_common[-1]}")

    insample_res = {}
    blind_res = {}
    blind_missing = []

    for sym, name, model in IN_SAMPLE:
        r = eval_sym(sym, name, model, ins_common)
        if r:
            insample_res[sym] = {'name': name, 'model': model, **r}
            print(f"  [in-sample] {sym} {name} P0-A+B V15 PF={r['P0-A+B']['V15']['pf']:.2f} "
                  f"n={r['P0-A+B']['V15']['n']}")
        else:
            print(f"  [in-sample] {sym} 数据不足, 跳过")

    for sym, name, model in BLIND_POOL:
        r = eval_sym(sym, name, model, ins_common)
        if r:
            blind_res[sym] = {'name': name, 'model': model, **r}
            print(f"  [blind] {sym} {name} P0-A+B V15 PF={r['P0-A+B']['V15']['pf']:.2f} "
                  f"n={r['P0-A+B']['V15']['n']}")
        else:
            blind_missing.append(sym)

    # 统计
    def pf_v15(d):
        return d['P0-A+B']['V15']['pf']

    blind_list = list(blind_res.values())
    n_blind = len(blind_list)
    n_pf_gt1 = sum(1 for d in blind_list if pf_v15(d) > 1.0)
    n_pf_gt1_15 = sum(1 for d in blind_list if pf_v15(d) > 1.5)
    gw_all = sum(d['P0-A+B']['V15']['gw'] for d in blind_list)
    gl_all = sum(d['P0-A+B']['V15']['gl'] for d in blind_list)
    pooled_pf = (gw_all / gl_all) if gl_all > 0 else (99.0 if gw_all > 0 else 0.0)
    ins_pf_gt1 = sum(1 for d in insample_res.values() if pf_v15(d) > 1.0)

    print("\n=== 盲 holdout 汇总 ===")
    print(f"盲池有效标的: {n_blind} (缺失/不足: {len(blind_missing)} {blind_missing})")
    print(f"盲池 V15 PF>1: {n_pf_gt1}/{n_blind} ({100*n_pf_gt1/max(n_blind,1):.0f}%)")
    print(f"盲池 V15 PF>1.5: {n_pf_gt1_15}/{n_blind}")
    print(f"盲池池化 PF(P0-A+B/V15): {pooled_pf:.3f}")
    print(f"in-sample 8 只 V15 PF>1: {ins_pf_gt1}/8")

    dump = {
        'eval_window': ins_common,
        'insample': insample_res,
        'blind': blind_res,
        'blind_missing': blind_missing,
        'summary': {
            'n_blind': n_blind, 'n_pf_gt1': n_pf_gt1, 'n_pf_gt1_15': n_pf_gt1_15,
            'pooled_pf_v15': pooled_pf, 'ins_pf_gt1': ins_pf_gt1,
        },
        'configs': CONFIGS,
    }
    with open(os.path.join(OUT, 'batch_metrics.json'), 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=2,
                  default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else o)
    _write_html(dump)
    print('\nDONE ->', OUT)


def _write_html(dump):
    blind = dump['blind']
    ins = dump['insample']
    s = dump['summary']
    rows = []
    for sym, d in blind.items():
        v15 = d['P0-A+B']['V15']
        v1 = d['P0-A+B']['V1']
        pf15 = 'inf' if v15['pf_inf'] else f"{v15['pf']:.2f}"
        pf1 = 'inf' if v1['pf_inf'] else f"{v1['pf']:.2f}"
        color = '#1a7f37' if v15['pf'] > 1.0 else '#b3261e'
        rows.append(f"<tr><td>{sym}</td><td>{d['name']}</td>"
                    f"<td style='color:{color};font-weight:bold'>{pf15}</td>"
                    f"<td>{pf1}</td><td>{v15['net_pct']:+.1f}%</td>"
                    f"<td>{v15['n']}</td><td>{v15['win_rate']:.0f}%</td></tr>")
    ins_rows = []
    for sym, d in ins.items():
        v15 = d['P0-A+B']['V15']
        pf15 = 'inf' if v15['pf_inf'] else f"{v15['pf']:.2f}"
        ins_rows.append(f"<tr><td>{sym}</td><td>{d['name']}</td>"
                        f"<td>{pf15}</td><td>{v15['net_pct']:+.1f}%</td>"
                        f"<td>{v15['n']}</td></tr>")
    html = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>miji 盲 holdout 批量检验</title>
<style>body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;margin:24px;color:#222}}
h1{{font-size:20px}} table{{border-collapse:collapse;font-size:13px;margin:8px 0}}
th,td{{border:1px solid #ddd;padding:4px 8px;text-align:right}} th{{background:#f5f5f5}}
td:first-child,td:nth-child(2){{text-align:left}} .sum{{background:#eef;padding:10px;border-radius:6px;margin:12px 0}}
.warn{{color:#b3261e}}</style></head><body>
<h1>miji 多时间框架共振 (V15) 盲 holdout 批量检验</h1>
<p>评估窗口严格 = in-sample 同一 61 天; V15 参数(15m/240)原样不重调. 盲池 = 调参时不可见的新鲜 T0 ETF/LOF.</p>
<div class='sum'>
<b>盲池有效标的:</b> {s['n_blind']} (缺失/不足: {len(dump['blind_missing'])} {dump['blind_missing']})<br>
<b>盲池 V15 PF&gt;1:</b> {s['n_pf_gt1']}/{s['n_blind']} ({100*s['n_pf_gt1']/max(s['n_blind'],1):.0f}%)<br>
<b>盲池 V15 PF&gt;1.5:</b> {s['n_pf_gt1_15']}/{s['n_blind']}<br>
<b>盲池池化 PF (V15, 盈亏额加权):</b> {s['pooled_pf_v15']:.3f}<br>
<b>in-sample 8 只 V15 PF&gt;1:</b> {s['ins_pf_gt1']}/8
</div>
<h2>盲 holdout 池 (P0-A+B: V15 vs V1 对照)</h2>
<table><tr><th>代码</th><th>名称</th><th>V15 PF</th><th>V1 PF</th><th>净额%</th><th>笔数</th><th>胜率</th></tr>
{''.join(rows)}</table>
<h2>in-sample 8 只 (对照, V15)</h2>
<table><tr><th>代码</th><th>名称</th><th>V15 PF</th><th>净额%</th><th>笔数</th></tr>
{''.join(ins_rows)}</table>
<p class='warn'>判读: 若盲池绝大多数 PF&lt;1, 则原 2 幸存者属过拟合/运气, 策略无泛化 edge.</p>
<p style='color:#888;font-size:12px'>⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。</p>
</body></html>"""
    with open(os.path.join(OUT, 'batch_report.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print('HTML ->', os.path.join(OUT, 'batch_report.html'))


if __name__ == '__main__':
    main()
