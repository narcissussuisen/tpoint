# -*- coding: utf-8 -*-
r"""daily_agent.py — tpoint AI 自主闭环（L1 决策层）的 L0 机械执行器（2026-09-28，阶段2）

定位：WB 每日自动化（交易日盘后）唤起的 AI 会话按 docs/ai_loop_agenda.md 议程做「判断」，
本脚本做「确定性机械」，五个子命令：

  guard          交易日 + 幂等闸门。exit 0=放行；77=静默跳过（非交易日/今日已闭环）
  collect        权威源汇总 → data/ai_decisions/<date>.digest.json + 控制台紧凑摘要
  review-merges  ≤5 个交易日内 ai_loop/daily_iterate 合入的降效复核
                 （池级 net_wr 降 >3pp → 自动回滚 + 飞书告警；K7「做错要认」机械化）
  apply          配置变更唯一合规通道的 AI 入口（白名单 + 证据 + 频控 三重闸门，
                 内部走 effect_ledger.apply_config_change + git 提交）
  finalize       落决策 JSON（status=done）+ 飞书 [AI闭环] 摘要 + 连续维持强制升级

硬规则（与 docs/knowledge_alignment_map.md 判读纪律一致）：
- 随机对照 = 一票否决：apply 要求 --random-z >= 1.0 且 --delta-pp >= +1.0（n>=30 由 AI 在议程中核实）。
- AI 可自动合入白名单 = AUTO_MERGE_PARAMS（per-symbol GT 五参 + has_base）；其余一律人审提案。
- 24h 最多 1 次 ai_loop 合入（账本扫描强制）。
- 连续 5 个交易日「维持」→ finalize 强制 escalation（飞书升级 + backlog P1），打破无限维持。
"""
import argparse
import datetime
import glob
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from trading_calendar import is_trading_day  # noqa: E402

DATA = os.path.join(ROOT, 'data')
OUT = os.path.join(ROOT, 'output')
DEC_DIR = os.path.join(DATA, 'ai_decisions')
PROP_DIR = os.path.join(DATA, 'ai_proposals')
LEDGER = os.path.join(DATA, 'effect_ledger.jsonl')
BACKLOG = os.path.join(DATA, 'feedback_backlog.jsonl')
MON_CFG = os.path.join(DATA, 'monitor_config.json')
RECHECK = os.path.join(DATA, 'ai_recheck.json')
VOL_SHADOW_HIST = os.path.join(DATA, 'vol_shadow_history.json')

# backlog 状态机（任务9/阶段3：消除「只写不消费」病根）：
# open → triaged（已审，给出去向判断）→ proposal（已转提案）/ closed（关闭，须写关闭理由）
BACKLOG_STATES = ('open', 'triaged', 'proposal', 'closed')
VOL_SHADOW_PROMOTE_DAYS = 10   # vol_regime_gate promote 判据：shadow 累积 ≥10 交易日

SKIP = 77  # 与 ds_benchmark 的 SKIPPED 语义一致（静默跳过，非错误）

# AI 可自动合入的 per-symbol 白名单（GT 五参 + has_base）；其余一律人审
AUTO_MERGE_PARAMS = {'buy_threshold', 'sell_threshold', 'signal_gap',
                     'min_hist_diff', 'vol_ratio_b_max', 'has_base'}
ROLLBACK_WINDOW_DAYS = 5      # 合入后复核窗口（交易日）
ROLLBACK_DROP_PP = 3.0        # 池级 net_wr 降效阈值（pp）
ROLLBACK_MIN_AFTER_DAYS = 2   # 至少 2 个变更后交易日才判降效
MERGE_RATE_LIMIT_H = 24       # ai_loop 合入频控（小时）
MAINTAIN_ESCALATE_AFTER = 5   # 连续「维持」强制升级阈值（交易日）


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _le_core():
    """惰性加载 loop_engine/core.py（git/push/lock 基建复用；importlib 避免模块名冲突）。"""
    spec = importlib.util.spec_from_file_location(
        'le_core', os.path.join(ROOT, 'scripts', 'loop_engine', 'core.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_json(fp, default=None):
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _jsonl(fp):
    out = []
    try:
        with open(fp, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _as_pct(v):
    """net_wr 单位归一到百分数（账本里 0-1 比例与百分数并存过）。"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v * 100.0 if abs(v) <= 1.5 else v


# --------------------------------------------------------------------------- #
# guard：交易日 + 幂等
# --------------------------------------------------------------------------- #
def cmd_guard(_a):
    d = datetime.date.today()
    if not is_trading_day(d):
        print(f'[guard] {d} 非交易日，静默退出')
        sys.exit(SKIP)
    st = _load_json(os.path.join(DEC_DIR, f'{_today()}.json'))
    if st and st.get('status') == 'done':
        print(f'[guard] {_today()} 决策已落盘（status=done），幂等退出')
        sys.exit(SKIP)
    print(f'[guard] {d} 交易日且今日未闭环 → 放行')


# --------------------------------------------------------------------------- #
# collect：权威源汇总 digest
# --------------------------------------------------------------------------- #
def _latest(glob_pat):
    fs = sorted(glob.glob(glob_pat))
    return fs[-1] if fs else None


def _maintain_streak():
    streak = 0
    for fp in sorted(glob.glob(os.path.join(DEC_DIR, '????-??-??.json')), reverse=True):
        st = _load_json(fp) or {}
        if st.get('status') != 'done':
            continue
        if st.get('verdict') == 'maintain':
            streak += 1
        else:
            break
    return streak


def _random_control_latest():
    """每标的取最新判决，分两条：baseline（无 cfg_overrides 的生产口径锚点）+
    latest_override（最新扫描臂）。基线是 AI 诊断的头锚，扫描臂只作旁证。"""
    def _entry(fp, d, meta):
        return {'file': os.path.basename(fp), 'meta': meta,
                'verdict': d.get('verdict'), 'n_signals': d.get('n_signals'),
                'n_trips': d.get('n_trips'), 'real': d.get('real'),
                'p': d.get('p'), 'z': d.get('z'),
                'cfg_overrides': meta.get('cfg_overrides')}

    out = {}
    for fp in sorted(glob.glob(os.path.join(OUT, 'random_control_*.json'))):
        d = _load_json(fp) or {}
        meta = d.get('meta') or {}
        sym = meta.get('sym')
        if not sym or meta.get('self_test'):
            continue
        slot = out.setdefault(sym, {'baseline': None, 'latest_override': None})
        # 实验臂 = cfg_overrides 或 min_hist_diff 任一非空；两臂皆空才是真·生产口径基线
        key = 'latest_override' if (meta.get('cfg_overrides') or meta.get('min_hist_diff')) else 'baseline'
        cur = slot[key]
        if cur is None or str(meta.get('generated_at', '')) > str(cur['meta'].get('generated_at', '')):
            slot[key] = _entry(fp, d, meta)
    return {s: {k: v for k, v in slot.items() if v} for s, slot in out.items()}


def _vol_shadow(days_back=5):
    """vol_regime_gate shadow 计数（生产 GT 配置口径，最近 N 个有数据日）：
    回答「若开启低波动门，每天会少推几条」。"""
    try:
        from dataclasses import replace
        import pandas as pd
        from random_control_validator import find_data_path, load_days_full
        from daily_signal_review import build_data
        import general_signal as gs
        wl = _load_json(os.path.join(DATA, 'watchlist.json')) or {}
        ga = ((_load_json(MON_CFG) or {}).get('_global') or {}).get('general_algorithm') or {}
        res = {}
        for sym in wl:
            path = find_data_path(sym)
            if not path:
                res[sym] = {'error': 'no_data'}
                continue
            days_all, _name = load_days_full(path)
            dates = sorted(days_all.keys())
            recent = set(dates[-days_back:])
            cfg = gs.GENERAL_DEFAULT
            for k, v in ga.items():
                if not str(k).startswith('_') and hasattr(cfg, k):
                    cfg = replace(cfg, **{k: v})
            prev_close = None
            tot = {'days': 0, 'signals': 0, 'would_suppress_lowvol': 0}
            for d in dates:
                dd = days_all[d]
                c = dd['c']
                if len(c) < 20:
                    continue
                pc = prev_close if prev_close is not None else c[0]
                prev_close = c[-1]
                if d not in recent:
                    continue
                df = pd.DataFrame({'open': dd['o'], 'high': dd['h'], 'low': dd['lo'],
                                   'close': c, 'volume': dd['v'],
                                   'trade_time': [d + ' 09:31:00'] * len(c)})
                data = build_data(df, pc)
                if data is None:
                    continue
                sigs = gs.detect_signals_general(data, pc, cfg)
                st = dict(gs.LAST_DETECTION_STATS)
                tot['days'] += 1
                tot['signals'] += len(sigs)
                tot['would_suppress_lowvol'] += int(st.get('would_suppress_lowvol', 0))
            tot['shadow_note'] = '若开启 vol_regime_gate 则少推 would_suppress_lowvol 条（口径=生产 GT 配置）'
            res[sym] = tot
        return res
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


def cmd_collect(_a):
    os.makedirs(DEC_DIR, exist_ok=True)
    os.makedirs(PROP_DIR, exist_ok=True)
    date = _today()

    lr_fp = _latest(os.path.join(OUT, 'live_review_*.json'))
    v4_fp = _latest(os.path.join(OUT, 'v4_gray_compare_*.json'))
    ledger = _jsonl(LEDGER)
    backlog_open = [e for e in _jsonl(BACKLOG)
                    if str(e.get('status', 'open')).lower() in ('open', 'pending', 'new', '')]
    cfg = _load_json(MON_CFG) or {}
    gt_cfg = {
        '_global': ((cfg.get('_global') or {}).get('general_algorithm') or {}),
        'per_symbol': {s: (v or {}).get('general_algorithm')
                       for s, v in cfg.items()
                       if not str(s).startswith('_') and isinstance(v, dict) and v.get('general_algorithm')},
        'has_base': {s: v.get('has_base') for s, v in cfg.items()
                     if not str(s).startswith('_') and isinstance(v, dict) and 'has_base' in v},
    }
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime('%Y-%m-%d')
    recent_changes = [e for e in ledger
                      if e.get('type') == 'config_change' and str(e.get('ts', ''))[:10] >= cutoff]

    digest = {
        'date': date, 'generated_at': _now(),
        'knowledge_map': 'docs/knowledge_alignment_map.md（K1-K7 条款对照真源，诊断必读）',
        'live_review_latest': {'file': os.path.basename(lr_fp) if lr_fp else None,
                               'content': _load_json(lr_fp) if lr_fp else None},
        'effect_ledger_tail20': ledger[-20:],
        'backlog_open_count': len(backlog_open),
        'backlog_open': backlog_open[-30:],
        'random_control_latest': _random_control_latest(),
        'monitor_gt_config': gt_cfg,
        'v4_gray_latest': {'file': os.path.basename(v4_fp) if v4_fp else None,
                           'content': _load_json(v4_fp) if v4_fp else None},
        'push_audit_tail5': _jsonl(os.path.join(DATA, 'push_audit.jsonl'))[-5:],
        'maintain_streak': _maintain_streak(),
        'recent_config_changes_10d': recent_changes,
        'vol_regime_shadow_5d': _vol_shadow(5),
        'recheck_queue': _load_json(RECHECK, default={'items': []}),
        'pending_proposals': [os.path.basename(p) for p in sorted(
            glob.glob(os.path.join(PROP_DIR, '*.json')))][-10:],
    }
    # [任务9] vol_shadow 历史累积：每日落一份计数，promote 判据「≥10 交易日」的数据源
    hist = _load_json(VOL_SHADOW_HIST, default={}) or {}
    if isinstance(digest['vol_regime_shadow_5d'], dict) and 'error' not in digest['vol_regime_shadow_5d']:
        hist[date] = digest['vol_regime_shadow_5d']
        try:
            with open(VOL_SHADOW_HIST, 'w', encoding='utf-8') as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    digest['vol_shadow_days_accumulated'] = len(hist)
    digest['vol_shadow_promote_hint'] = (
        f'shadow 已累积 {len(hist)} 日'
        + (f'（≥{VOL_SHADOW_PROMOTE_DAYS} 日：满足样本量门槛，可评估 promote 提案——'
           f'用 random_control_validator --set vol_regime_gate=true 做 ON/OFF 配对 A/B 判 suppressed 子集净贡献）'
           if len(hist) >= VOL_SHADOW_PROMOTE_DAYS
           else f'（未满 {VOL_SHADOW_PROMOTE_DAYS} 日门槛）'))

    fp = os.path.join(DEC_DIR, f'{date}.digest.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    # 控制台紧凑摘要（AI 会话主要看这个）
    print(f'=== AI 闭环 digest {date} ===')
    lr = digest['live_review_latest']
    print(f"[live_review] {lr['file'] or '无'}")
    if isinstance(lr['content'], dict):
        summ = lr['content'].get('summary') or {}
        if summ:
            print(f'  summary: {json.dumps(summ, ensure_ascii=False)[:400]}')
    rc = digest['random_control_latest']
    for s, slot in rc.items():
        for kind, v in slot.items():
            zw = (v.get('z') or {}).get('net_wr')
            print(f"[随机对照/{kind}] {s}: {v.get('verdict')} n_sig={v.get('n_signals')} "
                  f"z_netwr={None if zw is None else round(zw, 2)} ({v.get('file')})"
                  + (f" overrides={v.get('cfg_overrides')}" if v.get('cfg_overrides') else ''))
    vs = digest['vol_regime_shadow_5d']
    print(f'[vol_shadow 5d] {json.dumps(vs, ensure_ascii=False)[:300]}')
    print(f"[backlog] open={digest['backlog_open_count']} | [maintain_streak]={digest['maintain_streak']}"
          f" | [recent_changes 10d]={len(recent_changes)}")
    print(f"[vol_shadow] {digest['vol_shadow_promote_hint']}")
    print(f"[gt_config._global] {json.dumps(gt_cfg['_global'], ensure_ascii=False)[:300]}")
    print(f'DIGEST_JSON -> {fp}')


# --------------------------------------------------------------------------- #
# review-merges：合入降效复核 → 自动回滚（K7）
# --------------------------------------------------------------------------- #
def _trading_days_lookback(n):
    days = []
    d = datetime.date.today()
    while len(days) < n + 1:
        if is_trading_day(d):
            days.append(d.strftime('%Y-%m-%d'))
        d -= datetime.timedelta(days=1)
    return days


def cmd_review_merges(_a):
    import effect_ledger
    le = _le_core()
    window = set(_trading_days_lookback(ROLLBACK_WINDOW_DAYS + 3))
    ledger = _jsonl(LEDGER)
    changes = [e for e in ledger
               if e.get('type') == 'config_change'
               and str(e.get('ts', ''))[:10] in window
               and str(e.get('source', '')).startswith(('ai_loop', 'daily_iterate'))]
    # 过滤已被回滚过的（存在引用其 change_id 的 auto_rollback 条目）
    rolled_ids = {e.get('note', '').split('rollback-of:')[-1].split()[0]
                  for e in ledger if 'rollback-of:' in str(e.get('note', ''))}
    daily = [e for e in ledger if e.get('type') == 'daily' and _as_pct(e.get('net_wr')) is not None]
    wr_by_date = {}
    for e in daily:
        wr_by_date.setdefault(e.get('date'), []).append(_as_pct(e.get('net_wr')))

    results = []
    for ch in changes:
        cid = ch.get('change_id')
        if cid in rolled_ids:
            continue
        cdate = str(ch.get('ts', ''))[:10]
        before = sorted(d for d in wr_by_date if d < cdate)[-3:]
        after = sorted(d for d in wr_by_date if d > cdate)[:3]
        rec = {'change_id': cid, 'sym': ch.get('sym'), 'param': ch.get('param'),
               'old': ch.get('old'), 'new': ch.get('new'), 'date': cdate,
               'before_days': before, 'after_days': after}
        if len(after) < ROLLBACK_MIN_AFTER_DAYS:
            rec['verdict'] = 'observing'
            results.append(rec)
            continue
        b_wr = sum(sum(wr_by_date[d]) / len(wr_by_date[d]) for d in before) / max(1, len(before)) if before else None
        a_wr = sum(sum(wr_by_date[d]) / len(wr_by_date[d]) for d in after) / len(after)
        rec['before_net_wr'] = b_wr
        rec['after_net_wr'] = a_wr
        if b_wr is not None and a_wr < b_wr - ROLLBACK_DROP_PP:
            drop = b_wr - a_wr
            try:
                effect_ledger.apply_config_change(
                    ch.get('sym'), ch.get('param'), ch.get('old'),
                    source='ai_loop:auto_rollback',
                    note=f'rollback-of:{cid} 降效 {drop:.1f}pp（{b_wr:.1f}→{a_wr:.1f}）自动回滚（K7 做错要认）')
                rec['verdict'] = 'rolled_back'
                rec['drop_pp'] = round(drop, 2)
                le.push(f"⚠️ [AI闭环] 自动回滚 {ch.get('sym')}.{ch.get('param')}: "
                        f"{ch.get('new')} → {ch.get('old')}（合入 {cdate} 后池级净胜率降 {drop:.1f}pp，K7）")
            except Exception as e:
                rec['verdict'] = 'rollback_failed'
                rec['error'] = f'{type(e).__name__}: {e}'
        else:
            rec['verdict'] = 'hold'
        results.append(rec)

    fp = os.path.join(DEC_DIR, f'{_today()}.review.json')
    os.makedirs(DEC_DIR, exist_ok=True)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump({'date': _today(), 'generated_at': _now(), 'results': results},
                  f, ensure_ascii=False, indent=2)
    n_rb = sum(1 for r in results if r.get('verdict') == 'rolled_back')
    print(f'[review-merges] 复核 {len(results)} 项合入：回滚 {n_rb} / 维持 {sum(1 for r in results if r.get("verdict") == "hold")} '
          f'/ 观察中 {sum(1 for r in results if r.get("verdict") == "observing")} → {fp}')


# --------------------------------------------------------------------------- #
# apply：AI 自动合入唯一入口（白名单 + 证据 + 频控三重闸门）
# --------------------------------------------------------------------------- #
def cmd_apply(a):
    import effect_ledger
    param_leaf = str(a.param).split('.')[-1]
    if param_leaf not in AUTO_MERGE_PARAMS:
        print(f'[apply] 拒绝：{param_leaf} 不在 AI 自动合入白名单 {sorted(AUTO_MERGE_PARAMS)} → 走人审提案')
        sys.exit(2)
    if a.random_z is None or a.random_z < 1.0:
        print(f'[apply] 拒绝：随机对照 z={a.random_z} < 1.0（一票否决，fail-closed）')
        sys.exit(2)
    if a.delta_pp is None or a.delta_pp < 1.0:
        print(f'[apply] 拒绝：寻优增益 {a.delta_pp}pp < +1.0pp')
        sys.exit(2)
    # 频控：24h 内已有 ai_loop 合入则拒绝
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=MERGE_RATE_LIMIT_H)).strftime('%Y-%m-%d %H:%M:%S')
    recent = [e for e in _jsonl(LEDGER)
              if e.get('type') == 'config_change'
              and str(e.get('source', '')).startswith('ai_loop')
              and str(e.get('ts', '')) >= cutoff]
    if recent:
        print(f'[apply] 拒绝：{MERGE_RATE_LIMIT_H}h 内已有 ai_loop 合入（{recent[-1].get("change_id")}），频控拒绝')
        sys.exit(2)
    try:
        value = json.loads(a.value)
    except Exception:
        value = a.value
    entry = effect_ledger.apply_config_change(
        a.sym, a.param, value,
        source=f'ai_loop:{_today()}', proposal_id=a.proposal_id,
        note=a.note or f'random_z={a.random_z} delta_pp=+{a.delta_pp}')
    print(f"[apply] 已合入 {a.sym}.{a.param}: {entry.get('old')} → {entry.get('new')} "
          f"(change_id={entry.get('change_id')})")
    # git 提交（loose-ref 防线；push 尽力而为，失败不阻断热重载）
    try:
        le = _le_core()
        le.git('add', 'data/monitor_config.json', 'data/effect_ledger.jsonl')
        rc, _out, err = le.git('commit', '-m',
                               f'chore(ai_loop): {_today()} 自动合入 {a.sym}.{a.param}={value}'
                               f'（z={a.random_z} +{a.delta_pp}pp）')
        if rc == 0:
            le.ensure_ref_after_commit()
            rc2, log_out, _ = le.git('log', '--oneline', '-1')
            print(f'[apply] git commit ok: {log_out.strip()}')
            rc3, _o3, _e3 = le.git('push', 'origin', 'HEAD:refs/heads/feat/intraday-capture-v10.2.0')
            print(f'[apply] git push rc={rc3}' + ('' if rc3 == 0 else '（失败不阻断，次日会话补推）'))
        else:
            print(f'[apply] WARN git commit rc={rc}: {err[:200]}')
    except Exception as e:
        print(f'[apply] WARN git 提交异常（配置已热重载，账本已落）: {e!r}')


# --------------------------------------------------------------------------- #
# finalize：落决策 + 飞书摘要 + 连续维持强制升级
# --------------------------------------------------------------------------- #
def cmd_finalize(a):
    le = _le_core()
    date = _today()
    draft = _load_json(a.decisions)
    if not isinstance(draft, dict):
        print(f'[finalize] 拒绝：决策文件无效 {a.decisions}')
        sys.exit(2)
    verdict = draft.get('verdict') or ('changed' if draft.get('applied') else 'maintain')
    streak = _maintain_streak() + (1 if verdict == 'maintain' else 0)

    dec = dict(draft)
    dec.update({'date': date, 'finalized_at': _now(), 'status': 'done',
                'verdict': verdict, 'maintain_streak': streak})
    fp = os.path.join(DEC_DIR, f'{date}.json')
    os.makedirs(DEC_DIR, exist_ok=True)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(dec, f, ensure_ascii=False, indent=2)

    n_diag = len(draft.get('diagnoses') or [])
    n_prop = len(draft.get('proposals') or [])
    n_applied = len(draft.get('applied') or [])
    n_human = len(draft.get('human_review') or [])
    summary = (f'[AI闭环 {date}] 诊断{n_diag} 提案{n_prop} 合入{n_applied} 人审{n_human} '
               f'维持{1 if verdict == "maintain" else 0}(连续{streak}日)')
    lines = [summary]
    if draft.get('maintain_reason'):
        lines.append(f'维持原因: {draft["maintain_reason"]}')
    for p in (draft.get('human_review') or [])[:3]:
        lines.append(f'人审: {p.get("title") or p.get("proposal_id") or json.dumps(p, ensure_ascii=False)[:120]}')
    le.push_safe('\n'.join(lines))
    print(f'[finalize] {summary} → {fp}')

    # 连续维持强制升级（逃逸机制：打破 7 周无限维持）
    if verdict == 'maintain' and streak >= MAINTAIN_ESCALATE_AFTER:
        esc = {'id': f'P1-{date.replace("-", "")}-maintain-escalation',
               'severity': 'P1', 'status': 'open', 'source': 'ai_loop',
               'created': _now(),
               'title': f'连续 {streak} 个交易日「维持」，强制结构性诊断升级人审',
               'evidence': [f'data/ai_decisions 最近 {streak} 条 verdict=maintain'],
               'proposed_fix': '必须产出超参数空间的结构性提案（escalation: true），不允许继续维持'}
        with open(BACKLOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(esc, ensure_ascii=False) + '\n')
        le.push(f'🚨 [AI闭环 {date}] 连续 {streak} 日维持 → 已升级人审（backlog {esc["id"]}），'
                f'明日议程必须产出结构性提案')
        print(f'[finalize] 连续维持 {streak} 日 → 已升级（backlog + 飞书）')


# --------------------------------------------------------------------------- #
# backlog：状态机推进（消除「只写不消费」病根）
# --------------------------------------------------------------------------- #
def cmd_backlog(a):
    entries = _jsonl(BACKLOG)
    if a.list:
        n = 0
        for e in entries:
            st = str(e.get('status', 'open')).lower()
            if a.status and st != a.status:
                continue
            n += 1
            print(f"  [{st:<8}] {e.get('id')}: {str(e.get('title'))[:80]}")
        print(f'共 {n} 条（{a.status or "全部"}）/ 总 {len(entries)} 条')
        return
    if not a.id:
        print('[backlog] 推进状态需要 --id（或用 --list 查看）')
        sys.exit(2)
    if a.status not in BACKLOG_STATES:
        print(f'[backlog] 非法状态 {a.status}（合法：{BACKLOG_STATES}）')
        sys.exit(2)
    if a.status == 'closed' and not a.note:
        print('[backlog] 关闭必须写 --note 关闭理由（判读纪律：关闭要有证据）')
        sys.exit(2)
    hit = next((e for e in entries if e.get('id') == a.id), None)
    if hit is None:
        print(f'[backlog] 未找到 id={a.id}')
        sys.exit(2)
    old_status = str(hit.get('status', 'open')).lower()
    if old_status == 'closed':
        print(f'[backlog] {a.id} 已 closed，不可再变更（关闭不可逆）')
        sys.exit(2)
    hit['status'] = a.status
    hit['updated'] = _now()
    hit.setdefault('history', []).append(
        {'ts': _now(), 'from': old_status, 'to': a.status, 'actor': a.actor, 'note': a.note or ''})
    tmp = BACKLOG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    os.replace(tmp, BACKLOG)
    print(f'[backlog] {a.id}: {old_status} → {a.status}（{a.note or "无备注"}）')


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('guard')
    sub.add_parser('collect')
    sub.add_parser('review-merges')
    ap_apply = sub.add_parser('apply')
    ap_apply.add_argument('--sym', required=True)
    ap_apply.add_argument('--param', required=True,
                          help='点路径，如 general_algorithm.buy_threshold（叶子键须在白名单）')
    ap_apply.add_argument('--value', required=True, help='JSON 可解析值（数字/布尔/null）或字符串')
    ap_apply.add_argument('--random-z', type=float, default=None)
    ap_apply.add_argument('--delta-pp', type=float, default=None)
    ap_apply.add_argument('--proposal-id', default=None)
    ap_apply.add_argument('--note', default='')
    ap_fin = sub.add_parser('finalize')
    ap_fin.add_argument('--decisions', required=True, help='AI 写好的决策 draft JSON 路径')
    ap_bl = sub.add_parser('backlog')
    ap_bl.add_argument('--list', action='store_true')
    ap_bl.add_argument('--status', default=None, help=f'list 过滤 / 推进目标状态 {BACKLOG_STATES}')
    ap_bl.add_argument('--id', default=None)
    ap_bl.add_argument('--note', default='')
    ap_bl.add_argument('--actor', default='ai_loop')
    a = ap.parse_args()
    {'guard': cmd_guard, 'collect': cmd_collect, 'review-merges': cmd_review_merges,
     'apply': cmd_apply, 'finalize': cmd_finalize, 'backlog': cmd_backlog}[a.cmd](a)


if __name__ == '__main__':
    main()
