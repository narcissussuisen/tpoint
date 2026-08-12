#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""daily_closed_loop.py — tpoint 每日自迭代闭环编排（2026-08-05 用户指令：报告不是终点）

在 daily_iterate.py（寻优+护栏热更）之后运行，把当日报告作为输入驱动下一轮优化：
  A 自我检验：当日实盘表现 vs 近5日基线/昨日，并验证「前一日应用的算法」今日是否有效
  B 自我排查：效果不佳环节定位（live_review opportunities + reconcile 差异）
  C 自我寻优：复用 factor_opt_<date>.json 推荐（缺失则补跑 factor_optimizer）
  D 自我回测：对「调整后因子组合」（最佳atr×最佳trail）做全历史 + 近20日 holdout 双重验证
  E 次日优化算法：落盘 output/next_day_algo_<date>.json + 推送四环结论到 a35d7f52

状态：data/closed_loop_state.json 记录每日生效配置快照 → 次日 A 步据此验证前日算法。

CLI：python scripts/daily_closed_loop.py --date 2026-08-05
"""
import os, sys, json, glob, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
OUT = os.path.join(ROOT, 'output')
STATE = os.path.join(ROOT, 'data', 'closed_loop_state.json')
CFG = os.path.join(ROOT, 'data', 'monitor_config.json')
TRACK_PARAMS = ['atr_min_pct', 'mpr_enable', 'mpr_periods', 'vol_confirm', 'ml_enable']
HOLDOUT_DAYS = 20


def push(text):
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    req = __import__('urllib.request').request.Request(
        HOOK, data=payload, headers={'Content-Type': 'application/json'})
    with __import__('urllib.request').request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', 'replace')


def load_json(p):
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None


def prev_live_review(date):
    files = sorted(glob.glob(os.path.join(OUT, 'live_review_*.json')))
    files = [f for f in files if not f.endswith(f'live_review_{date}.json')]
    return load_json(files[-1]) if files else None


def cfg_snapshot(cfg):
    return {s: {p: v.get(p) for p in TRACK_PARAMS if p in v}
            for s, v in cfg.items() if not s.startswith('_')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    a = ap.parse_args()
    date = a.date

    live = load_json(os.path.join(OUT, f'live_review_{date}.json'))
    rec = load_json(os.path.join(OUT, f'reconcile_{date}.json'))
    opt = load_json(os.path.join(OUT, f'factor_opt_{date}.json'))
    if live is None:
        push(f'⚠️ 闭环 {date}：live_review 缺失，无法检验，终止')
        sys.exit(1)
    if opt is None:  # 寻优缺失则补跑
        import subprocess
        subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'factor_optimizer.py'),
                        '--out', os.path.join(OUT, f'factor_opt_{date}.json')],
                       capture_output=True, text=True, encoding='utf-8')
        opt = load_json(os.path.join(OUT, f'factor_opt_{date}.json'))

    state = load_json(STATE) or {}
    cfg = load_json(CFG)
    snap_now = cfg_snapshot(cfg)
    snap_prev = state.get('config_snapshot', {})
    applied_today = []
    if snap_prev:  # 无昨日快照=首次建立基线，不算热更
        for sym, ps in snap_now.items():
            for p, v in ps.items():
                if snap_prev.get(sym, {}).get(p) != v:
                    applied_today.append({'sym': sym, 'param': p,
                                          'old': snap_prev.get(sym, {}).get(p), 'new': v})

    # ---------- A 自我检验 ----------
    sm = live['summary']
    base = live.get('baseline', {})
    yd = prev_live_review(date)
    chk = [f"当日实盘：推送{sm['n_pushes']} 配对{sm['n_trips']} 有效{sm['n_valid']}"
           f"（有效率{sm['valid_rate_pct']}%）净盈亏合计{sm['net_sum_pct']}%"]
    if base.get('sample_enough'):
        m = base['mean']
        chk.append(f"近{base['days']}日基线均值：有效率{m.get('valid_rate_pct')}% "
                   f"净盈亏{m.get('net_sum_pct')}% → "
                   f"{'优于' if sm['net_sum_pct'] > m.get('net_sum_pct', 0) else '劣于'}基线")
    else:
        chk.append(f"基线：{base.get('note', '样本不足')}")
    if yd:
        ys = yd['summary']
        trend = '↑' if sm['net_sum_pct'] > ys['net_sum_pct'] else ('↓' if sm['net_sum_pct'] < ys['net_sum_pct'] else '→')
        chk.append(f"环比昨日：净盈亏 {ys['net_sum_pct']}% → {sm['net_sum_pct']}%（{trend}）")
    last_applied = state.get('applied_today', [])
    if last_applied:
        for x in last_applied:
            ps = live['per_sym'].get(x['sym'], {})
            chk.append(f"前日热更验证：{x['sym']} {x['param']} {x['old']}→{x['new']}，"
                       f"今日该标的净盈亏{ps.get('net_sum_pct', '无数据')}%")
    else:
        chk.append('前一日无参数热更，今日表现为既有配置基线延续。')
    verdict = '有效' if sm['n_valid'] > 0 and sm['net_sum_pct'] > 0 else \
              ('样本不足' if sm['n_trips'] == 0 else '无效（当日配对净亏/零有效）')
    chk.insert(0, f'【检验结论】当日算法判定：{verdict}')

    # ---------- B 自我排查 ----------
    diag = []
    for o in live.get('opportunities', [])[:3]:
        diag.append(f"·[{o.get('severity', '?')}] {o.get('problem', '')[:60]} → {o.get('cause', '')[:60]}")
    if rec:
        for sym, r in rec.get('symbols', {}).items():
            lc, rn = r.get('live_counts', {}).get('total', 0), r.get('recalc_n_signals', 0)
            if lc == 0 and rn >= 2:
                diag.append(f'·⚠️ {sym} 实盘0推送 vs 复算{rn}信号（差-{rn}）疑似落盘断流/首扫抑制')
    if not diag:
        diag.append('·未发现显著异常环节')

    # ---------- B+ 无效日升级诊断：零推送标的闸门消融（区分"算法卡死"vs"生产侧抑制"） ----------
    if verdict != '有效' and rec:
        import subprocess
        zero_syms = [s for s, r in rec.get('symbols', {}).items()
                     if r.get('live_counts', {}).get('total', 0) == 0
                     and r.get('recalc_n_signals', 0) >= 2]
        if zero_syms:
            abl_out = os.path.join(OUT, f'gate_ablation_{date}.json')
            subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'gate_ablation.py'),
                            '--date', date, '--syms', ','.join(zero_syms), '--out', abl_out],
                           capture_output=True, text=True, encoding='utf-8')
            abl = load_json(abl_out) or {}
            diag.append('■ 升级诊断（零推送标的闸门消融，gate_ablation.py）：')
            for s in zero_syms:
                r0 = abl.get('symbols', {}).get(s, {})
                if 'error' in r0 or not r0:
                    diag.append(f'·{s} 消融失败：{r0.get("error", "无结果")[:50]}')
                    continue
                full_open = r0.get('cells', {}).get('全放开', {}).get('n', 0)
                if full_open > r0.get('baseline_signals', 0):
                    diag.append(f'·{s} 主卡死={r0["main_block_gate"]}（放开后{r0["baseline_signals"]}→{full_open}信号）'
                                f'→ 建议评审放宽该闸门')
                else:
                    diag.append(f'·{s} 全闸门放开信号量不变（{full_open}=复算量）'
                                f'→ 闸门无卡死，实盘零推送属生产侧抑制（首扫/断流），非算法失效')

    # ---------- C+D 寻优复用 + 组合回测 ----------
    import factor_optimizer as FO
    wl = load_json(FO.WATCHLIST)
    bt_lines, next_algo = [], {'date': date, 'generated_at': datetime.datetime.now().strftime('%F %T'),
                               'effective_tomorrow': {}, 'pending_review': []}
    recs = {r['sym']: r for r in opt.get('recommendations', [])} if opt else {}
    for sym in wl:
        symrep = (opt or {}).get('symbols', {}).get(sym, {})
        if 'error' in symrep or not symrep:
            continue
        base_wr = symrep.get('baseline', {}).get('win_rate')
        thin = symrep.get('thin_sample')
        gate = FO.GATE_PP_THIN if thin else FO.GATE_PP
        # 从网格挑最优 atr / trail（达标才换）
        atr_best, trail_best = FO.CUR_ATR, FO.CUR_TRAIL
        ag, tg = symrep.get('atr_grid', {}), symrep.get('trail_grid', {})
        atr_c = [(float(k), m) for k, m in ag.items()
                 if k != str(FO.CUR_ATR) and m['n'] >= FO.MIN_TRIPS and base_wr is not None and m['win_rate'] >= base_wr + gate]
        if atr_c:
            atr_best = max(atr_c, key=lambda x: x[1]['win_rate'])[0]
        tr_c = [(tuple(map(float, k.split('/'))), m) for k, m in tg.items()
                if k != f'{FO.CUR_TRAIL[0]}/{FO.CUR_TRAIL[1]}' and m['n'] >= FO.MIN_TRIPS and base_wr is not None and m['win_rate'] >= base_wr + gate]
        if tr_c:
            trail_best = max(tr_c, key=lambda x: x[1]['win_rate'])[0]
        proposed_changed = (atr_best != FO.CUR_ATR) or (trail_best != FO.CUR_TRAIL)
        eff_atr = cfg.get(sym, {}).get('atr_min_pct', FO.CUR_ATR)
        entry = {'atr_min_pct': eff_atr, 'trail': f'{FO.CUR_TRAIL[0]}/{FO.CUR_TRAIL[1]}',
                 'vol_confirm': cfg.get(sym, {}).get('vol_confirm', False)}
        if sym in recs:
            r0 = recs[sym]
            next_algo['pending_review'].append(
                {'sym': sym, 'param': r0['param'], 'value': r0['value'], 'delta_pp': r0['delta_pp']})
        next_algo['effective_tomorrow'][sym] = entry
        if not proposed_changed:
            bt_lines.append(f'·{sym}：无达标组合，维持 atr={eff_atr} trail=0.4/0.6')
            continue
        # 组合回测：当前 vs  proposed（全历史 + 近20日）
        try:
            days = FO.sym_days(sym)
            for d, data, g in days:
                data['sym'] = sym
            sig_cur = FO.day_signals(sym, wl[sym], days, eff_atr)
            sig_new = FO.day_signals(sym, wl[sym], days, atr_best)
            cur_full = FO.metrics_of(FO.eval_config(sig_cur, *FO.CUR_TRAIL))
            new_full = FO.metrics_of(FO.eval_config(sig_new, *trail_best))
            cur_h = FO.metrics_of(FO.eval_config(sig_cur[-HOLDOUT_DAYS:], *FO.CUR_TRAIL))
            new_h = FO.metrics_of(FO.eval_config(sig_new[-HOLDOUT_DAYS:], *trail_best))
            bt_lines.append(
                f'·{sym} 组合 atr={atr_best}+trail={trail_best[0]}/{trail_best[1]}：'
                f'全历史WR {cur_full["win_rate"]}%→{new_full["win_rate"]}% '
                f'收益{cur_full["total_ret"]}%→{new_full["total_ret"]}%｜'
                f'近20日WR {cur_h["win_rate"]}%→{new_h["win_rate"]}%（n={new_h["n"]}）')
        except Exception as e:
            bt_lines.append(f'·{sym} 组合回测失败：{str(e)[:60]}')

    out_algo = os.path.join(OUT, f'next_day_algo_{date}.json')
    json.dump(next_algo, open(out_algo, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    # ---------- E 状态 + 推送 ----------
    json.dump({'last_run': date, 'applied_today': applied_today, 'config_snapshot': snap_now},
              open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    lines = [f'🔄 tpoint 自迭代闭环 {date}｜检验={verdict}｜寻优{len(recs)}项｜次日算法已落盘', '',
             '一、自我检验（当日效果 vs 基线/前日算法）'] + chk + ['',
             '二、自我排查（效果不佳环节定位）'] + diag + ['',
             '三、关键因子寻优 + 组合回测（全历史+近20日holdout）'] + (
        [f"·{r['sym']} {r['param']}→{r['value']}（+{r['delta_pp']}pp, n={r['n_trips']}）"
         for r in opt.get('recommendations', [])] or ['·今日无达标候选']) + bt_lines + ['',
             '四、次日优化算法（明日开盘生效配置）',
             json.dumps(next_algo['effective_tomorrow'], ensure_ascii=False),
             f'待评审{len(next_algo["pending_review"])}项（锁定参数走周五评审）；落盘 {out_algo}']
    if applied_today:
        lines.insert(1, f'今日已热更{len(applied_today)}项：'
                        + '；'.join(f"{x['sym']}.{x['param']} {x['old']}→{x['new']}" for x in applied_today))
    push('\n'.join(lines))
    print('\n'.join(lines[:8]))
    print(f'[ok] {out_algo}')


if __name__ == '__main__':
    main()
