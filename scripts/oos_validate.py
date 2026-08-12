# -*- coding: utf-8 -*-
r"""oos_validate.py — 样本外(OOS)防过拟合检验器（2026-08-11 晚补，外部资料研判回灌）

## 为什么需要它
factor_optimizer.py 是「F盘全历史一次性全样本网格」，auto_tune.py 直接取全样本
best_cell 写生产配置 —— 这是典型的**样本内寻优直接上生产**，无任何 IS/OOS 切分。
国金《DeepSeek V4 Flash 金融投研测评》(2026-08-11) 对三个大模型回测的共性批评之一
即「未使用样本外检验或 walk-forward 等防过拟合手段」，tpoint 同构中招。

本脚本把候选参数放到**时间切分**下复核：
  IS 段（前 split 比例交易日）用于「假装寻优」，OOS 段（后 1-split）用于**独立验证**。
只有候选在 OOS 段仍优于基线，才认为该参数是真实规律而非历史噪音拟合。

## 判定规则（与 auto_tune 护栏同源：total_ret 优先 + wr 不降）
  PASS        : OOS 段 n >= MIN_TRIPS_OOS，且 OOS total_ret 改善 > RET_MIN_IMPROVE，且 wr 不降超 WR_TOL
  FAIL        : OOS 段样本充足但候选未改善/恶化 → 判定为样本内过拟合，禁止上生产（已上则应回滚）
  INCONCLUSIVE: OOS 段样本不足（薄样本标的常见）→ **不得视为通过**，按「无法确认」拒绝出手

## CLI
  python scripts/oos_validate.py --sym 513310.SH --param trail --value 0.3/0.5 [--split 0.7]
  python scripts/oos_validate.py --from-report 2026-08-11        # 复核报告里全部候选
  python scripts/oos_validate.py --audit-state                   # 复核 auto_tune 已落地的历史变更
输出：output/oos_validate_<date>.json + stdout；--push 则推自迭代群。
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

OUT = os.path.join(ROOT, 'output')
CFG = os.path.join(ROOT, 'data', 'monitor_config.json')
STATE = os.path.join(ROOT, 'data', 'auto_tune_state.json')
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d"

SPLIT = 0.7             # IS 占比
MIN_TRIPS_OOS = 20      # OOS 段最小 trip 数（低于此判 INCONCLUSIVE，不是 PASS）
# 与 auto_tune 同步收紧：用户 2026-08-05 硬约束是「wr 不降」，2.0pp 容差会让
# 513310 0.3/0.5（OOS wr -2.0pp）蒙混过关 → 仅留 0.5pp 数值噪音容差。
WR_TOL = 0.5            # OOS wr 允许下降上限(pp)
RET_MIN_IMPROVE = 0.2   # OOS total_ret 至少改善(pp)


def push(text):
    try:
        import urllib.request
        req = urllib.request.Request(HOOK, data=json.dumps(
            {"msg_type": "text", "content": {"text": text}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"POST_FAIL:{e}"


def load_json(p):
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None


def parse_trail(val):
    a, b = (float(x) for x in str(val).split('/'))
    return a, b


def validate_one(sym, param, value, split=SPLIT, cache={}, baseline=None):
    """对单个 (sym, param, value) 做 IS/OOS 切分验证。返回 dict。

    baseline:
      None      —— 取该标的**真实生产配置**为基线。用于「新候选放行」判定（auto_tune /
                   weekly_review 走这条），语义是「改成 value 比现在更好吗」。
      显式传值  —— 用于「已落地变更的事后复核」（--audit-state），基线必须是**变更前**
                   的值。若此时仍取生产值，则基线 == 候选（因为该变更已经上线），
                   检验退化为自我对比 —— 与本轮清算的 gate_ablation 同义反复同一种病。
    """
    import factor_optimizer as FO
    import monitor as M

    wl = load_json(os.path.join(ROOT, 'data', 'watchlist.json')) or {}
    name = wl.get(sym, sym)

    # 取全历史（缓存，避免同标的重复加载 F 盘）
    if sym not in cache:
        try:
            days = FO.sym_days(sym)
        except Exception as e:
            return {'sym': sym, 'param': param, 'value': value,
                    'verdict': 'ERROR', 'error': f'加载F盘数据失败: {e}'}
        for d, data, g in days:
            data['sym'] = sym
        cache[sym] = days
    days = cache[sym]
    n_days = len(days)
    if n_days < 10:
        return {'sym': sym, 'param': param, 'value': value, 'n_days': n_days,
                'verdict': 'INCONCLUSIVE', 'reason': f'交易日仅 {n_days} 天，无法切分'}

    days = sorted(days, key=lambda x: x[0])
    cut = int(n_days * split)
    is_days, oos_days = days[:cut], days[cut:]

    # 基线与候选：trail 走出场侧（信号固定当前 atr）；atr_min_pct 走信号侧
    # [2026-08-11 P0 修复] 基线原为模块级硬编码 FO.CUR_TRAIL=(0.4,0.6)，而各标的自
    # 08-05 v10.0.1 起已 per-symbol 分化（161129=0.5/0.6、513310=0.3/0.5 …）。
    # 本函数是**生产放行的最后一道闸**，基线取错 = 拿候选去跟一个从未上线的配置比改善，
    # OOS 的 Δret/Δwr 全部失真。改取该标的真实生产值。
    base_trail, base_atr = FO.prod_trail(sym), FO.CUR_ATR
    baseline_src = 'prod_config'
    if baseline is not None:
        baseline_src = 'explicit(pre-change)'
        if param == 'trail':
            base_trail = parse_trail(baseline)
        elif param == 'atr_min_pct':
            base_atr = float(baseline)
    orig_atr = M.PER_SYMBOL_CFG.get(sym, {}).get('atr_min_pct')
    orig_ta = M.PER_SYMBOL_CFG.get(sym, {}).get('trail_activate_pct')
    orig_tp = M.PER_SYMBOL_CFG.get(sym, {}).get('trail_pct')
    try:
        if param == 'trail':
            cand_trail, cand_atr = parse_trail(value), base_atr
        elif param == 'atr_min_pct':
            cand_trail, cand_atr = base_trail, float(value)
        else:
            return {'sym': sym, 'param': param, 'value': value,
                    'verdict': 'ERROR', 'error': f'不支持的参数 {param}'}

        # 同义反复守卫（2026-08-11）：基线与候选相同时 Δ 恒为 0，检验没有任何信息量，
        # 却会被下游读成「未改善 → FAIL」或「样本不足 → INCONCLUSIVE」，形成假结论。
        # 本轮清算 gate_ablation 时吃过这个教训，此处显式拦截而不是静默算完。
        if (tuple(cand_trail), cand_atr) == (tuple(base_trail), base_atr):
            return {'sym': sym, 'name': name, 'param': param, 'value': value,
                    'baseline_src': baseline_src, 'verdict': 'TAUTOLOGY',
                    'reason': (f'候选与基线完全相同（trail={list(cand_trail)} atr={cand_atr}）'
                               f'→ 自我对比，Δ 恒为 0，无信息量。'
                               f'事后复核请显式传 baseline=变更前的值。')}

        # ⚠️ 配置状态泄漏修复（2026-08-11 晚）：core/monitor.py:1188 的信号重放会读
        # exit_param(sym,'trail_activate_pct'/'trail_pct') 生成**出场提示信号**，
        # 因此若只在 simulate_day 侧换 trail、而 PER_SYMBOL_CFG 仍是生产值，
        # 得到的是「生产 trail 的信号 + 网格 trail 的出场」混合口径 —— 结果既不可复现
        # （随当时 monitor_config 而变，实测同参数两跑 n=33/31、Δwr -2.0/+3.2pp 相反），
        # 基线与候选的信号口径也不一致。故此处信号侧与出场侧必须使用同一组 trail。
        def seg_metrics(seg, atr_v, trail):
            # setdefault 而非 `if sym in`：不在 monitor_config 的标的原先会静默跳过写回，
            # 退化成泄漏口径（信号用默认 trail、出场用网格 trail）。
            _c = M.PER_SYMBOL_CFG.setdefault(sym, {})
            _c['trail_activate_pct'] = trail[0]
            _c['trail_pct'] = trail[1]
            sig = FO.day_signals(sym, name, seg, atr_v)
            return FO.metrics_of(FO.eval_config(sig, *trail))

        res = {}
        for tag, seg in (('is', is_days), ('oos', oos_days)):
            res[f'{tag}_base'] = seg_metrics(seg, base_atr, base_trail)
            res[f'{tag}_cand'] = seg_metrics(seg, cand_atr, cand_trail)
    finally:
        # 还原进入时的真实生产配置，避免污染同进程后续调用
        if sym in M.PER_SYMBOL_CFG:
            for k, v in (('atr_min_pct', orig_atr), ('trail_activate_pct', orig_ta),
                         ('trail_pct', orig_tp)):
                if v is None:
                    M.PER_SYMBOL_CFG[sym].pop(k, None)
                else:
                    M.PER_SYMBOL_CFG[sym][k] = v

    ob, oc = res['oos_base'], res['oos_cand']
    ib, ic = res['is_base'], res['is_cand']
    d_ret_oos = round(oc['total_ret'] - ob['total_ret'], 2)
    d_wr_oos = round(oc['win_rate'] - ob['win_rate'], 2)
    d_ret_is = round(ic['total_ret'] - ib['total_ret'], 2)

    if oc['n'] < MIN_TRIPS_OOS:
        verdict, reason = 'INCONCLUSIVE', (
            f"OOS 段 trip={oc['n']} < {MIN_TRIPS_OOS}，样本不足无法证伪 → 按纪律不得视为通过")
    elif d_wr_oos < -WR_TOL:
        verdict, reason = 'FAIL', f'OOS wr 恶化 {d_wr_oos}pp（超 -{WR_TOL}pp 容忍）'
    elif d_ret_oos <= RET_MIN_IMPROVE:
        verdict, reason = 'FAIL', (
            f'OOS total_ret 未改善（Δ{d_ret_oos}pp <= {RET_MIN_IMPROVE}pp）'
            f'；IS 段 Δ{d_ret_is}pp → 样本内优势未能外推，疑似过拟合')
    else:
        verdict, reason = 'PASS', (
            f'OOS total_ret Δ{d_ret_oos}pp、wr Δ{d_wr_oos}pp（n={oc["n"]}），样本外仍成立')

    # 可复算指纹（对齐「底稿可复算性」要求：区间 + 源数据版本必须落盘）
    try:
        csvp = os.path.join(FO.F_DATA, f'{sym}_1m.csv')
        st = os.stat(csvp)
        fp = {'file': csvp, 'size': st.st_size,
              'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}
    except Exception:
        fp = None

    return {'sym': sym, 'name': name, 'param': param, 'value': value,
            'n_days': n_days, 'split': split,
            'is_days': len(is_days), 'oos_days': len(oos_days),
            'is_range': [str(is_days[0][0]), str(is_days[-1][0])],
            'oos_range': [str(oos_days[0][0]), str(oos_days[-1][0])],
            'data_fingerprint': fp,
            'signal_exit_same_param': True,
            'is_base': ib, 'is_cand': ic, 'oos_base': ob, 'oos_cand': oc,
            'd_ret_is': d_ret_is, 'd_ret_oos': d_ret_oos, 'd_wr_oos': d_wr_oos,
            'verdict': verdict, 'reason': reason}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sym')
    ap.add_argument('--param', choices=['trail', 'atr_min_pct'])
    ap.add_argument('--value')
    ap.add_argument('--baseline', default=None,
                    help='对照基线（如 0.4/0.6）。省略=用当前生产配置（新候选放行口径）；'
                         '复核已上线的变更时应显式传变更前的值，否则基线==候选（同义反复）')
    ap.add_argument('--split', type=float, default=SPLIT)
    ap.add_argument('--from-report', dest='from_report',
                    help='复核 output/factor_opt_<date>.json 中全部推荐候选')
    ap.add_argument('--audit-state', action='store_true',
                    help='复核 auto_tune_state.json 中已落地(applied)的历史变更')
    ap.add_argument('--push', action='store_true')
    a = ap.parse_args()

    jobs = []
    if a.audit_state:
        st = load_json(STATE) or {}
        cfgnow = load_json(CFG) or {}
        for h in st.get('history', []):
            if h.get('action') != 'applied':
                continue
            if h.get('sym') not in cfgnow:
                continue
            def _fmt(v):
                if h['param'] == 'trail':
                    return '/'.join(str(x) for x in v)
                return str(v[0] if isinstance(v, list) else v)
            val = _fmt(h['new'])
            # 事后复核的基线必须是**变更前**的值：该变更已上线，若基线取生产值就等于
            # 拿它跟自己比（同义反复）。h['old'] 缺失则 None → 退回生产值并由守卫拦截。
            old = _fmt(h['old']) if h.get('old') is not None else None
            jobs.append((h['sym'], h['param'], val, h.get('date'), old))
    elif a.from_report:
        rep = load_json(os.path.join(OUT, f'factor_opt_{a.from_report}.json'))
        if not rep:
            print(f'[oos] factor_opt_{a.from_report}.json 缺失'); sys.exit(1)
        cfgnow = load_json(CFG) or {}
        for r in rep.get('recommendations', []):
            if r['sym'] in cfgnow:      # 与 auto_tune 同纪律：只看已监控标的
                # 新候选放行：基线 = 当前生产配置（baseline=None）
                jobs.append((r['sym'], r['param'], r['value'], a.from_report, None))
    elif a.sym and a.param and a.value:
        jobs.append((a.sym, a.param, a.value, None, a.baseline))
    else:
        print('用法：--sym/--param/--value 或 --from-report <date> 或 --audit-state'); sys.exit(2)

    today = datetime.date.today().strftime('%Y-%m-%d')
    results, cache = [], {}
    for sym, param, val, src, base in jobs:
        print(f'[oos] 检验 {sym} {param}={val} '
              f'(基线={base or "生产配置"}, split={a.split}) ...', flush=True)
        r = validate_one(sym, param, val, a.split, cache, baseline=base)
        r['applied_date'] = src
        results.append(r)
        print(f"  → {r['verdict']}: {r.get('reason') or r.get('error')}")

    out = {'date': today, 'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'split': a.split, 'min_trips_oos': MIN_TRIPS_OOS,
           'mode': 'audit-state' if a.audit_state else ('from-report' if a.from_report else 'single'),
           'results': results}
    p = os.path.join(OUT, f'oos_validate_{today}.json')
    json.dump(out, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'[ok] {p}')

    n_fail = sum(1 for r in results if r['verdict'] == 'FAIL')
    n_pass = sum(1 for r in results if r['verdict'] == 'PASS')
    n_inc = sum(1 for r in results if r['verdict'] == 'INCONCLUSIVE')
    n_taut = sum(1 for r in results if r['verdict'] == 'TAUTOLOGY')
    if a.push:
        lines = [f'🧪 [tpoint 样本外(OOS)防过拟合检验 {today}]｜IS/OOS={a.split:.0%}/{1-a.split:.0%}',
                 f'■ 结论：PASS {n_pass} / FAIL {n_fail} / 无法确认 {n_inc}'
                 + (f' / 同义反复 {n_taut}' if n_taut else '')]
        for r in results:
            icon = {'PASS': '✅', 'FAIL': '❌', 'INCONCLUSIVE': '⚠️',
                    'TAUTOLOGY': '🔁'}.get(r['verdict'], '⁉️')
            lines.append(f"{icon} {r['sym']} {r['param']}={r['value']}：{r.get('reason') or r.get('error')}")
        lines.append('纪律：仅 PASS 允许写入生产配置；FAIL 视为样本内过拟合应回滚；无法确认=不出手。')
        push('\n'.join(lines))
    sys.exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
