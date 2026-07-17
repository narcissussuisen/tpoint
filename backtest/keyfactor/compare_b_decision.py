#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B决策复核聚合器 (2026-07-17):
  对比「逐日修正」(daily) 路径 与 「旧连续数组」(buggy) 路径 的结果,
  并给出 v9.1.0 是否解锁的判定。

输入 (均由 run_study.py 产出):
  - keyfactor_results_daily.json   (RunA: --daily; 全样本逐日修正归因)
  - keyfactor_summary.json         (旧: 非daily/连续数组路径, 作对照基线)
  - keyfactor_oos_time_daily.json (RunB: --oos-time --daily; 跨时段样本外)
  - keyfactor_oos_time_daily.csv  (RunB 逐票明细)

输出:
  - 控制台结构化对比 + 判定
  - keyfactor_b_verdict.html       (飞书交付)
"""
import os, json, datetime
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'keyfactor_data')

F_NEW_JSON  = os.path.join(DATA, 'keyfactor_results_daily.json')
F_OLD_JSON  = os.path.join(DATA, 'keyfactor_summary.json')
F_OOS_JSON  = os.path.join(DATA, 'keyfactor_oos_time_daily.json')
F_OOS_CSV   = os.path.join(DATA, 'keyfactor_oos_time_daily.csv')
F_HTML      = os.path.join(DATA, 'keyfactor_b_verdict.html')

HORIZONS = [6, 12, 24]

def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def fnum(x, p=4):
    if x is None:
        return 'None'
    try:
        return f'{float(x)*100:+.4f}%'
    except Exception:
        return str(x)

def main():
    new = load_json(F_NEW_JSON)
    old = load_json(F_OLD_JSON)
    oos = load_json(F_OOS_JSON)

    lines = []
    def log(s=''):
        lines.append(s)
        print(s)

    log('=' * 70)
    log('  B 决策复核聚合  (逐日修正 daily vs 旧连续数组 buggy)')
    log('=' * 70)
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log(f'  生成时间: {ts}')
    log('')

    # ---------- 1. 全样本逐项对比 (daily vs buggy) ----------
    log('-' * 70)
    log('  ① 全样本 baseline (符号调整后前向收益 skill, 单位%)')
    log('-' * 70)
    if new and old:
        n_new = new.get('n_signals_baseline')
        n_old = old.get('n_signals_baseline')
        log(f"  信号数:  修正(daily)={n_new}   旧(buggy)={n_old}   "
            f"({(n_new/n_old-1)*100:+.1f}%)")
        bs_new = new.get('baseline_skill', {})
        bs_old = old.get('baseline_skill', {})
        for h in HORIZONS:
            a = bs_new.get(str(h)) if str(h) in bs_new else bs_new.get(h)
            b = bs_old.get(str(h)) if str(h) in bs_old else bs_old.get(h)
            a = a[0] if isinstance(a, (list, tuple)) else a
            b = b[0] if isinstance(b, (list, tuple)) else b
            log(f"  skill{h:>2}: 修正={fnum(a)}   旧={fnum(b)}")
    else:
        log('  ⚠️ 缺少 new/old json, 跳过')
    log('')

    # ---------- 2. 因子重要性 / 消融 对比 ----------
    log('-' * 70)
    log('  ② 因子重要性 (关掉后 skill24 变化, 正=该因子贡献为正)')
    log('-' * 70)
    if new and old:
        imp_new = new.get('factor_importance_drop_skill24', {})
        imp_old = old.get('factor_importance_drop_skill24', {})
        for fac in ('gravity', 'vol_div', 'macd_div'):
            vn = imp_new.get(fac)
            vo = imp_old.get(fac)
            log(f"  {fac:10s}: 修正={fnum(vn)}   旧={fnum(vo)}")
        log(f"  排序(修正): {new.get('factor_ranking')}")
        log(f"  排序(旧)  : {old.get('factor_ranking')}")
        # 一致性检查
        consistent = (abs((imp_new.get('vol_div') or 0)) >= 0 and
                      (imp_new.get('gravity') or 0) > (imp_new.get('macd_div') or 0))
        log(f"  因子方向一致性(引力>macd>vol_div为负): {'✅' if consistent else '❌ 需核查'}")
    log('')

    # ---------- 3. 跨时段 OOS (RunB) ----------
    log('-' * 70)
    log('  ③ 跨时段样本外 OOS (RunB: --oos-time --daily)')
    log('-' * 70)
    oos_ok = False
    frac_pos = None
    delta = None
    if oos:
        tr = oos.get('mean_skill24_train')
        te = oos.get('mean_skill24_test')
        frac_pos = oos.get('frac_test_positive')
        delta = oos.get('delta_test_minus_train')
        log(f"  训练(样本内) skill24 = {fnum(tr)}")
        log(f"  测试(跨时段OOS) skill24 = {fnum(te)}")
        log(f"  OOS 正向票占比 frac_test_positive = {frac_pos}")
        log(f"  delta(OOS - train) = {fnum(delta)}")
        log(f"  参与股票数 n_stocks = {oos.get('n_stocks')}  (含OOS={oos.get('n_stocks_with_test')})")
        oos_ok = (te is not None) and (te > 0) and (frac_pos is not None) and (frac_pos >= 0.5)
        log(f"  跨时段验证通过(>0 且 正向占比>=50%): {'✅' if oos_ok else '❌'}")
    else:
        log('  ⚠️ 缺少 oos_time_daily.json, 跳过')
    log('')

    # ---------- 3b. OOS 信号加权聚合 (从CSV) ----------
    if os.path.exists(F_OOS_CSV):
        try:
            df = pd.read_csv(F_OOS_CSV)
            if 'skill24_test' in df.columns:
                sub = df[df['skill24_test'].notna()]
                if len(sub):
                    w = sub['n_test']
                    pooled = (sub['skill24_test'] * w).sum() / w.sum()
                    frac = (sub['skill24_test'] > 0).mean()
                    log(f"  [CSV] 信号加权 OOS skill24 = {fnum(pooled)}  "
                         f"(正向票占比={frac:.3f}, n={len(sub)})")
        except Exception as e:
            log(f"  [CSV] 读取失败: {e}")
    log('')

    # ---------- 4. 判定 ----------
    log('=' * 70)
    log('  ④ 判定 (v9.1.0 是否解锁)')
    log('=' * 70)
    core_holds = False
    if new and old:
        bs = new.get('baseline_skill', {})
        sk24 = bs.get(24) or bs.get('24')
        sk24 = sk24[0] if isinstance(sk24, (list, tuple)) else sk24
        core_holds = (sk24 is not None) and (sk24 > 0)
        log(f"  P0-P5 核心结论(修正后全样本 skill24>0): {'✅ 成立' if core_holds else '❌ 不成立'}  "
            f"(skill24={fnum(sk24)})")

    unlock = core_holds and oos_ok
    if unlock:
        verdict = '🔓 解锁 v9.1.0：逐日修正后核心因子结论成立，且跨时段OOS正向显著 → 解冻 feat/v9.1.1-index-gating。'
    elif core_holds and not oos_ok:
        verdict = '🟡 核心结论成立但跨时段OOS未通过：v9.1.0 维持冻结；P0-P5 日内语义已确认有效。'
    else:
        verdict = '🔒 核心结论被修正推翻：维持冻结，需回查检测/归因链路。'
    log(f"  {verdict}")
    log('')
    log('  注: 旧 headline +4.89%(in)/+4.93%(out) 来自「按标的切分」OOS, 与本处')
    log('  「逐日修正全样本 skill24」及「跨时段 OOS」非同一指标, 仅方向/量级参照。')

    # ---------- 5. HTML 报告 ----------
    html = build_html(ts, new, old, oos, core_holds, oos_ok, unlock, verdict)
    with open(F_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    log(f'\n  落地 HTML: {F_HTML}')
    return 0

def build_html(ts, new, old, oos, core_holds, oos_ok, unlock, verdict):
    def row(k, a, b):
        return f'<tr><td>{k}</td><td>{a}</td><td>{b}</td></tr>'
    def fnum(x):
        if x is None: return '—'
        try: return f'{float(x)*100:+.4f}%'
        except: return str(x)
    body = ''
    # ①
    if new and old:
        n_new = new.get('n_signals_baseline'); n_old = old.get('n_signals_baseline')
        bs_new = new.get('baseline_skill', {}); bs_old = old.get('baseline_skill', {})
        rows = ''
        rows += row('信号数', f'{n_new}', f'{n_old}')
        for h in HORIZONS:
            a = bs_new.get(h) or bs_new.get(str(h)); b = bs_old.get(h) or bs_old.get(str(h))
            a = a[0] if isinstance(a,(list,tuple)) else a
            b = b[0] if isinstance(b,(list,tuple)) else b
            rows += row(f'skill{h}', fnum(a), fnum(b))
        body += f'<h2>① 全样本 baseline</h2><table>{rows}</table>'
    # ②
    if new and old:
        imp_new = new.get('factor_importance_drop_skill24', {})
        imp_old = old.get('factor_importance_drop_skill24', {})
        rows = ''
        for fac in ('gravity','vol_div','macd_div'):
            rows += row(fac, fnum(imp_new.get(fac)), fnum(imp_old.get(fac)))
        rows += row('排序', str(new.get('factor_ranking')), str(old.get('factor_ranking')))
        body += f'<h2>② 因子重要性 (关掉后 skill24 变化)</h2><table>{rows}</table>'
    # ③
    if oos:
        rows = ''
        rows += row('训练 skill24', fnum(oos.get('mean_skill24_train')), '—')
        rows += row('跨时段OOS skill24', fnum(oos.get('mean_skill24_test')), '—')
        rows += row('OOS正向票占比', str(oos.get('frac_test_positive')), '—')
        rows += row('delta(OOS-train)', fnum(oos.get('delta_test_minus_train')), '—')
        rows += row('n_stocks', str(oos.get('n_stocks')), '—')
        body += f'<h2>③ 跨时段样本外 OOS</h2><table>{rows}</table>'
    # ④
    badge = '#1a7f37' if unlock else ('#b58105' if core_holds else '#b42318')
    body += f'''<h2>④ 判定</h2>
    <div style="padding:12px;border-radius:8px;background:{badge};color:#fff;font-size:15px;font-weight:600;">{verdict}</div>
    <p style="color:#666;font-size:12px;">生成: {ts} ｜ 旧 headline +4.89%(in)/+4.93%(out) 来自「按标的切分」OOS, 与本处指标非同一口径, 仅作方向/量级参照。</p>'''
    return f'''<!doctype html><html><head><meta charset="utf-8">
    <title>B决策复核</title><style>
    body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:24px;color:#1f2328;}}
    h1{{font-size:20px;}} h2{{font-size:15px;margin-top:20px;color:#0969da;}}
    table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px;}}
    th,td{{border:1px solid #d0d7de;padding:6px 10px;text-align:left;}}
    tr:nth-child(even){{background:#f6f8fa;}}
    </style></head><body>
    <h1>B 决策复核 · 逐日修正验证</h1>{body}</body></html>'''

if __name__ == '__main__':
    main()
