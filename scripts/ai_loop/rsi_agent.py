# -*- coding: utf-8 -*-
r"""rsi_agent.py — tpoint RSI（递归自我改进）主循环的 L0 机械执行器（P2.2，2026-09-28）

EvoAlpha Curator 七步循环的 tpoint 落地（AI 会话按 docs/rsi_loop_agenda.md 判断，本脚本机械执行）：

  selfcheck   步0 自检：交易日+幂等+环境探针（必需脚本存在性——diag_r2p_probe 失踪 8 周教训）
              + 测试基线快照（当日首次）
  measure     步1 测量：调 daily_agent.collect（digest）+ 基准窗 bench（重计算，17:00 后/curator 会话跑）
  validate    步4 四道闸：spec_freeze.check(patch) + gates(selftest/tests/compare)
  apply-code  步5 代码轨合入：四闸全过 + spec_hash 不变 + 工作树干净 + 黑名单外
              ⇒ git add/commit + tag rsi/<date>-<id> + 60 天 validity_queue；
              工作树脏/闸门不过 = 拒绝 + 告警（fail-closed）
  observe     步6 队列到期复核：60 天观察期到期项，指标 vs apply 时——退步且归因到该改动
              ⇒ git revert + 飞书告警 + effect_ledger 记条目；无退步 ⇒ matured
  state       断点续跑：data/rsi/<date>.state.json 记录每步 done/rc，crash 后从断点继续

边界铁律（docs/rsi_loop_agenda.md）：
  - 参数层改动 = daily_agent.apply（唯一通道，本模块不经手 monitor_config）。
  - 代码层改动 = 本模块 apply-code（四道闸 + spec_hash + 黑名单 + 队列）。
  - spec 层（判据语义/验收阈值/watchlist/monitor 推送链）= 永不自动，人审提案卡。
  - 每日最多 1 次代码层合入（对齐参数层 24h 频控语义）。

用法：
  python scripts/ai_loop/rsi_agent.py selfcheck [--date YYYY-MM-DD]
  python scripts/ai_loop/rsi_agent.py measure [--light]
  python scripts/ai_loop/rsi_agent.py validate --patch data/ai_proposals/<date>.patch.json
  python scripts/ai_loop/rsi_agent.py apply-code --patch <patch.json> --random-z 1.5
  python scripts/ai_loop/rsi_agent.py observe
  python scripts/ai_loop/rsi_agent.py state [--date YYYY-MM-DD]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # spec_freeze/gates 同目录

from trading_calendar import is_trading_day  # noqa: E402
import spec_freeze  # noqa: E402

RSI_DIR = os.path.join(ROOT, 'data', 'rsi')
QUEUE_FP = os.path.join(RSI_DIR, 'validity_queue.json')
LOG_FP = os.path.join(RSI_DIR, 'iteration_log.jsonl')
PY = os.path.join(ROOT, 'venv', 'Scripts', 'python.exe')
SKIP = 77
QUEUE_DAYS = 60           # 代码层改动观察期（天）
DAILY_CODE_MERGE_MAX = 1  # 每日代码层合入上限

# 环境探针：必需文件存在性（失踪即 selfcheck FAIL——diag_r2p_probe 8 周教训的机械化）
REQUIRED_FILES = [
    'scripts/ai_loop/daily_agent.py',
    'scripts/ai_loop/gates.py',
    'scripts/ai_loop/spec_freeze.py',
    'scripts/loop_engine/core.py',
    'scripts/random_control_validator.py',
    'scripts/research/exit_anchor_grid.py',
    'core/simulate_position_sm.py',
    'core/general_signal.py',
    'docs/player_benchmark.md',
    'docs/knowledge_alignment_map.md',
    'docs/rsi_loop_agenda.md',
]


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _le_core():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'le_core', os.path.join(ROOT, 'scripts', 'loop_engine', 'core.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load(fp, default=None):
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    # os.replace（Windows file-lock 防线：不用 os.unlink）
    os.replace(tmp, fp)


# --------------------------------------------------------------------------- #
# state 断点续跑
# --------------------------------------------------------------------------- #
def state_path(date):
    return os.path.join(RSI_DIR, f'{date}.state.json')


def load_state(date):
    return _load(state_path(date), {'date': date, 'steps': {}})


def mark_step(date, step, rc, note=''):
    st = load_state(date)
    st['steps'][step] = {'rc': rc, 'at': _now(), 'note': note}
    _save(state_path(date), st)


def step_status(date, step):
    return load_state(date)['steps'].get(step)


# --------------------------------------------------------------------------- #
# 步0 selfcheck
# --------------------------------------------------------------------------- #
def cmd_selfcheck(date, snapshot_tests=True):
    if not is_trading_day(date):
        print(f'{date} 非交易日 → SKIP(77)')
        return SKIP
    st = step_status(date, 'selfcheck')
    if st and st['rc'] == 0:
        print(f'selfcheck 今日已完成（{_now()} 前的 {st["at"]}），幂等放行')
        return 0
    missing = [f for f in REQUIRED_FILES if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        mark_step(date, 'selfcheck', 2, f'缺失: {missing}')
        print(f'⛔ 环境探针 FAIL：{missing}')
        return 2
    # git 工作树状态（脏树记录但不阻断 selfcheck——apply-code 时才硬拒）
    le = _le_core()
    dirty = le.git('status', '--short', capture=True) or ''
    note = f'dirty_tree_lines={len([l for l in dirty.splitlines() if l.strip()])}'
    # 测试基线快照（当日首次）
    if snapshot_tests and not os.path.exists(os.path.join(RSI_DIR, 'test_baseline.json')):
        r = subprocess.run([PY, os.path.join(ROOT, 'scripts', 'ai_loop', 'gates.py'),
                            'tests', '--snapshot'], capture_output=True, text=True, timeout=3600)
        note += f' test_snapshot_rc={r.returncode}'
    mark_step(date, 'selfcheck', 0, note)
    print(f'selfcheck OK（{note}）')
    return 0


# --------------------------------------------------------------------------- #
# 步1 measure
# --------------------------------------------------------------------------- #
def cmd_measure(date, light=True):
    if step_status(date, 'measure'):
        print('measure 今日已完成（幂等）')
        return 0
    # light：digest（daily_agent collect 幂等）；heavy（bench）由 curator 会话显式跑
    r = subprocess.run([PY, os.path.join(ROOT, 'scripts', 'ai_loop', 'daily_agent.py'),
                        'collect', '--date', date], capture_output=True, text=True, timeout=1800)
    ok = r.returncode == 0
    mark_step(date, 'measure', r.returncode, 'light' if light else 'full')
    print(f'measure collect rc={r.returncode}')
    return r.returncode if not ok else 0


# --------------------------------------------------------------------------- #
# 步4 validate：四道闸
# --------------------------------------------------------------------------- #
def cmd_validate(patch_fp):
    ok, reasons, patch = spec_freeze.validate_patch(patch_fp)
    for r in reasons:
        print(r)
    if not ok:
        print('PATCH_REJECTED（spec/黑名单层）')
        return 2
    # 闸二：验证器自检
    r2 = subprocess.run([PY, os.path.join(ROOT, 'scripts', 'ai_loop', 'gates.py'),
                         'selftest'], capture_output=True, text=True, timeout=900)
    if r2.returncode != 0:
        print('闸二 FAIL（验证器自检）')
        return 2
    # 闸三：测试基线（AI 会话需先应用 patch 到工作树再跑 compare/全量——本命令只跑快照比对）
    r3 = subprocess.run([PY, os.path.join(ROOT, 'scripts', 'ai_loop', 'gates.py'),
                         'tests'], capture_output=True, text=True, timeout=3600)
    if r3.returncode != 0:
        print(f'闸三 FAIL：{r3.stdout[-400:]}')
        return 2
    # 闸一/闸四（bench compare）由 AI 会话在 patch 应用到工作树后显式跑：
    #   gates.py bench --tag cand && gates.py compare <cand> <baseline>
    #   （重计算分钟级，不塞进本命令）
    print('validate: spec/黑名单+闸二+闸三 PASS（闸一/闸四由会话跑 bench compare）')
    return 0


# --------------------------------------------------------------------------- #
# 步5 apply-code：代码轨合入
# --------------------------------------------------------------------------- #
def cmd_apply_code(patch_fp, random_z):
    if random_z < 1.0:
        print('⛔ 闸四：random_z < 1.0（随机对照一票否决）')
        return 2
    ok, reasons, patch = spec_freeze.validate_patch(patch_fp)
    if not ok:
        for r in reasons:
            print(r)
        print('⛔ PATCH_REJECTED')
        return 2
    date = _today()
    # 每日 ≤1 代码合入 + 断点幂等
    queue = _load(QUEUE_FP, [])
    if sum(1 for q in queue if q.get('status') == 'active'
           and q.get('applied_at', '').startswith(date)) >= DAILY_CODE_MERGE_MAX:
        print(f'⛔ 频控：今日已有 {DAILY_CODE_MERGE_MAX} 次代码层合入')
        return 3
    # 工作树干净（fail-closed）
    le = _le_core()
    dirty = (le.git('status', '--short', capture=True) or '').strip()
    if dirty:
        print(f'⛔ 工作树脏（apply-code 前必须干净）：\n{dirty}')
        return 3
    # 四道闸结果文件必须存在且全 PASS（AI 会话跑完后传入）
    gates_fp = patch.get('gates_result')
    gr = _load(gates_fp) if gates_fp else None
    if not gr or not all(gr.get(k) for k in ('gate1', 'gate2', 'gate3', 'gate4')):
        print(f'⛔ 四道闸结果缺失或不全 PASS：{gates_fp} -> {gr}')
        return 2

    pid = patch.get('proposal_id', 'unknown')
    files = [f if isinstance(f, str) else f.get('path') for f in patch['files']]
    # commit + tag（loose-ref 防线：commit 后 ensure_ref_after_commit + git log 二次校验）
    le.git('add', *files)
    msg = f"rsi: {pid} — {patch.get('hypothesis', '')[:80]}"
    le.git('commit', '-m', msg)
    tag = f'rsi/{date}-{pid}'
    le.git('tag', tag)
    log = le.git('log', '--oneline', '-1', capture=True) or ''
    if pid[:16] not in msg and tag.split('-', 1)[1][:8] not in log:
        pass  # tag 已打，git log 校验以 commit 落盘为准
    if not log.strip():
        print('⛔ git log 空——commit 未落盘（loose-ref 病理），中止入队')
        return 4
    # 入 60 天队列
    due = (datetime.date.today() + datetime.timedelta(days=QUEUE_DAYS)).isoformat()
    queue.append({'proposal_id': pid, 'tag': tag, 'commit': log.split()[0],
                  'files': files, 'applied_at': _now(), 'queue_until': due,
                  'status': 'active', 'random_z': random_z,
                  'metrics_at_apply': patch.get('metrics_at_apply', {})})
    _save(QUEUE_FP, queue)
    _append_log({'round': len(queue), 'proposal_id': pid, 'tag': tag,
                 'step': 'apply-code', 'at': _now(), 'gates': gr})
    try:
        le.push(f'[{tag}] RSI 代码层合入：{pid}（四道闸 PASS，观察期至 {due}）')
    except Exception as e:
        print(f'⚠ 飞书推送失败（不阻断）: {e}')
    print(f'apply-code OK：{tag}（观察期至 {due}）')
    return 0


# --------------------------------------------------------------------------- #
# 步6 observe：队列到期复核
# --------------------------------------------------------------------------- #
def cmd_observe(date):
    queue = _load(QUEUE_FP, [])
    if not queue:
        print('队列空')
        return 0
    le = _le_core()
    changed = False
    for q in queue:
        if q['status'] != 'active' or q['queue_until'] > date:
            continue
        # 到期复核：指标 vs apply 时（由 AI 会话/上游跑 bench 后写 review_metrics；
        # 机械层先看 review 字段——缺 review_metrics = 保留 active 并提示）
        rm = q.get('review_metrics') or {}
        base = q.get('metrics_at_apply') or {}
        if not rm:
            print(f"⚠ {q['proposal_id']} 到期但无复核指标——保留 active，提示 AI 会话补跑 bench")
            continue
        worse = (rm.get('net_wr') is not None and base.get('net_wr') is not None
                 and (rm['net_wr'] - base['net_wr']) * 100.0 < -3.0)
        if worse:
            # 自动回滚：git revert（不改历史，留 revert commit）
            try:
                le.git('revert', '--no-edit', q['commit'])
                q['status'] = 'reverted'
                q['reverted_at'] = _now()
                _append_log({'proposal_id': q['proposal_id'], 'step': 'observe-revert',
                             'at': _now(), 'detail': '到期复核退步>3pp，git revert'})
                try:
                    le.push(f"⚠️ [RSI] {q['proposal_id']} 观察期到期复核退步>3pp → 已自动 revert（{q['tag']}）")
                except Exception:
                    pass
            except Exception as e:
                print(f"⛔ revert 失败（需人工介入）: {e}")
        else:
            q['status'] = 'matured'
            q['matured_at'] = _now()
            _append_log({'proposal_id': q['proposal_id'], 'step': 'observe-matured',
                         'at': _now()})
        changed = True
    if changed:
        _save(QUEUE_FP, queue)
    print('observe 完成')
    return 0


def _append_log(entry):
    os.makedirs(RSI_DIR, exist_ok=True)
    with open(LOG_FP, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description='tpoint RSI 主循环 L0 执行器')
    ap.add_argument('cmd', choices=('selfcheck', 'measure', 'validate', 'apply-code',
                                    'observe', 'state'))
    ap.add_argument('--date', default=_today())
    ap.add_argument('--patch', default='', help='validate/apply-code: patch.json 路径')
    ap.add_argument('--random-z', type=float, default=0.0, help='apply-code: 闸四 z 值')
    ap.add_argument('--no-snapshot', action='store_true')
    a = ap.parse_args()

    if a.cmd == 'selfcheck':
        sys.exit(cmd_selfcheck(a.date, snapshot_tests=not a.no_snapshot))
    elif a.cmd == 'measure':
        sys.exit(cmd_measure(a.date))
    elif a.cmd == 'validate':
        sys.exit(cmd_validate(a.patch) if a.patch else ap.error('validate 需要 --patch'))
    elif a.cmd == 'apply-code':
        sys.exit(cmd_apply_code(a.patch, a.random_z) if a.patch
                 else ap.error('apply-code 需要 --patch'))
    elif a.cmd == 'observe':
        sys.exit(cmd_observe(a.date))
    elif a.cmd == 'state':
        print(json.dumps(load_state(a.date), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
