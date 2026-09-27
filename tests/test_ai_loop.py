# -*- coding: utf-8 -*-
r"""test_ai_loop.py — AI 自主闭环机械层回归（2026-09-28，阶段2）

覆盖 daily_agent 的判定逻辑（全 monkeypatch，不碰真实 config/账本/飞书）：
  T1 review-merges：降效 >3pp → rolled_back + apply_config_change(old) + push 告警
  T2 review-merges：降幅 ≤3pp → hold（不回滚）
  T3 review-merges：变更后交易日 <2 → observing
  T4 apply：非白名单参数 → SystemExit(2)
  T5 apply：random_z<1.0 → SystemExit(2)（一票否决 fail-closed）
  T6 apply：24h 频控（已有 ai_loop 合入）→ SystemExit(2)
  T7 apply：全闸门通过 → apply_config_change 被调用 + git commit 尝试
  T8 finalize：连续 5 日维持 → escalation（backlog 追加 + 升级 push）
  T9 guard：已 done → SystemExit(77)（幂等）

运行：venv\Scripts\python.exe tests\test_ai_loop.py
"""
import datetime
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, os.path.join(ROOT, 'scripts', 'ai_loop'))

import daily_agent as da  # noqa: E402

_JSONL_ORIG = da._jsonl  # 真·读文件版（_patch_env 会替换 da._jsonl，T10/T11 需恢复）

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS {name}')
    else:
        FAIL += 1
        print(f'  FAIL {name}  {detail}')


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _stub_le(calls):
    """loop_engine/core.py 的替身：git/push 全记录，不真执行。"""
    m = types.SimpleNamespace()
    m.push = lambda text, hook=None: calls.append(('push', text))
    m.push_safe = lambda text, hook=None: calls.append(('push_safe', text))
    m.git = lambda *a, **k: calls.append(('git', a)) or (0, 'ok', '')
    m.ensure_ref_after_commit = lambda: calls.append(('ensure_ref',))
    return m


def _patch_env(tmp, ledger_entries, calls, apply_ret=None):
    """统一 monkeypatch：账本数据源 + le_core 替身 + effect_ledger 替身。"""
    da._jsonl_orig = da._jsonl
    da._jsonl = lambda fp: ledger_entries if 'effect_ledger' in str(fp) else []
    da._le_core = lambda: _stub_le(calls)
    applied = []

    class FakeEL:
        @staticmethod
        def apply_config_change(sym, param, value, source, proposal_id=None, note=''):
            applied.append(dict(sym=sym, param=param, value=value, source=source, note=note))
            return apply_ret or {'change_id': 'chg-fake', 'old': 0.45, 'new': value}

    sys.modules['effect_ledger'] = FakeEL
    da.DEC_DIR = tmp
    return applied


def _dates():
    """⚠️ 必须用真实交易日（自然日偏移会踩中节假日——2026-09-25 中秋节曾被误当周五工作日）。
    返回最近交易日列表，[0]=最近一个交易日。"""
    return sorted(da._trading_days_lookback(12), reverse=True)


def t1_rollback_triggers():
    with tempfile.TemporaryDirectory() as tmp:
        d = _dates()
        # 变更日=d[2]，变更前=d[3]/d[4]（60%），变更后=d[0]/d[1]（50%，降 10pp）
        ledger = [
            {'type': 'config_change', 'change_id': 'chg-t1', 'ts': f'{d[2]} 15:50:00',
             'sym': '300010.SZ', 'param': 'general_algorithm.buy_threshold',
             'old': 0.45, 'new': 0.55, 'source': f'ai_loop:{d[2]}'},
            {'type': 'daily', 'date': d[4], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[3], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[1], 'net_wr': 50.0},
            {'type': 'daily', 'date': d[0], 'net_wr': 50.0},
        ]
        calls = []
        applied = _patch_env(tmp, ledger, calls)
        da.cmd_review_merges(Args())
        check('T1 降效>3pp 触发回滚', len(applied) == 1 and applied[0]['value'] == 0.45,
              f'applied={applied}')
        check('T1 回滚来源标记', applied and applied[0]['source'] == 'ai_loop:auto_rollback')
        check('T1 飞书告警', any(c[0] == 'push' and '自动回滚' in str(c[1]) for c in calls))


def t2_small_drop_holds():
    with tempfile.TemporaryDirectory() as tmp:
        d = _dates()
        # 变更前 60% → 变更后 58.5%（降 1.5pp ≤3 → hold）
        ledger = [
            {'type': 'config_change', 'change_id': 'chg-t2', 'ts': f'{d[2]} 15:50:00',
             'sym': '300010.SZ', 'param': 'general_algorithm.buy_threshold',
             'old': 0.45, 'new': 0.55, 'source': f'ai_loop:{d[2]}'},
            {'type': 'daily', 'date': d[4], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[3], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[1], 'net_wr': 58.5},
            {'type': 'daily', 'date': d[0], 'net_wr': 58.5},
        ]
        applied = _patch_env(tmp, ledger, [])
        da.cmd_review_merges(Args())
        check('T2 降幅≤3pp 不回滚', len(applied) == 0, f'applied={applied}')


def t3_observing_when_thin():
    with tempfile.TemporaryDirectory() as tmp:
        d = _dates()
        # 变更日=d[1]（最近交易日的前一个），变更后仅 d[0] 一天 → observing
        ledger = [
            {'type': 'config_change', 'change_id': 'chg-t3', 'ts': f'{d[1]} 15:50:00',
             'sym': '300010.SZ', 'param': 'general_algorithm.buy_threshold',
             'old': 0.45, 'new': 0.55, 'source': f'ai_loop:{d[1]}'},
            {'type': 'daily', 'date': d[3], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[2], 'net_wr': 60.0},
            {'type': 'daily', 'date': d[0], 'net_wr': 50.0},
        ]
        applied = _patch_env(tmp, ledger, [])
        da.cmd_review_merges(Args())
        check('T3 变更后<2 日只观察', len(applied) == 0, f'applied={applied}')


def _expect_exit(name, fn, code):
    try:
        fn()
        check(name, False, '未退出')
    except SystemExit as e:
        check(name, e.code == code, f'exit={e.code}')


def t4_apply_rejects_nonwhitelist():
    with tempfile.TemporaryDirectory() as tmp:
        _patch_env(tmp, [], [])
        _expect_exit('T4 非白名单拒绝', lambda: da.cmd_apply(Args(
            sym='300010.SZ', param='general_algorithm.w_vwap', value='1.4',
            random_z=3.0, delta_pp=5.0, proposal_id=None, note='')), 2)


def t5_apply_rejects_low_z():
    with tempfile.TemporaryDirectory() as tmp:
        _patch_env(tmp, [], [])
        _expect_exit('T5 z<1.0 一票否决', lambda: da.cmd_apply(Args(
            sym='300010.SZ', param='general_algorithm.buy_threshold', value='0.55',
            random_z=0.5, delta_pp=5.0, proposal_id=None, note='')), 2)


def t6_apply_rate_limit():
    with tempfile.TemporaryDirectory() as tmp:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ledger = [{'type': 'config_change', 'change_id': 'chg-24h', 'ts': now,
                   'sym': '300010.SZ', 'param': 'general_algorithm.buy_threshold',
                   'old': 0.45, 'new': 0.5, 'source': 'ai_loop:x'}]
        _patch_env(tmp, ledger, [])
        _expect_exit('T6 24h 频控拒绝', lambda: da.cmd_apply(Args(
            sym='300010.SZ', param='general_algorithm.buy_threshold', value='0.55',
            random_z=3.0, delta_pp=5.0, proposal_id=None, note='')), 2)


def t7_apply_success_path():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        applied = _patch_env(tmp, [], calls)
        da.cmd_apply(Args(sym='300010.SZ', param='general_algorithm.buy_threshold',
                          value='0.55', random_z=3.0, delta_pp=5.0,
                          proposal_id='P1', note='t7'))
        check('T7 全闸门通过→合入', len(applied) == 1 and applied[0]['value'] == 0.55,
              f'applied={applied}')
        check('T7 git 提交尝试', any(c[0] == 'git' and 'commit' in c[1] for c in calls))


def t8_finalize_escalation():
    with tempfile.TemporaryDirectory() as tmp:
        d = _dates()
        for k in range(1, 6):  # 预置连续 5 日 maintain
            with open(os.path.join(tmp, f'{d[k]}.json'), 'w', encoding='utf-8') as f:
                json.dump({'status': 'done', 'verdict': 'maintain'}, f)
        calls = []
        _patch_env(tmp, [], calls)
        backlog_fp = os.path.join(tmp, 'backlog.jsonl')
        da.BACKLOG = backlog_fp
        draft_fp = os.path.join(tmp, 'draft.json')
        with open(draft_fp, 'w', encoding='utf-8') as f:
            json.dump({'verdict': 'maintain', 'maintain_reason': 't8', 'diagnoses': []}, f)
        da.cmd_finalize(Args(decisions=draft_fp))
        bl = open(backlog_fp, encoding='utf-8').read() if os.path.exists(backlog_fp) else ''
        check('T8 连续 5+1 日维持→backlog 升级', 'maintain-escalation' in bl, f'backlog={bl[:100]}')
        check('T8 升级飞书推送', any(c[0] == 'push' and '连续' in str(c[1]) for c in calls))


def t9_guard_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        da.DEC_DIR = tmp
        with open(os.path.join(tmp, f'{da._today()}.json'), 'w', encoding='utf-8') as f:
            json.dump({'status': 'done'}, f)
        try:
            da.cmd_guard(Args())
            # 今日若是交易日且未退出 = 幂等失效；非交易日会 77 退出（也符合预期分支）
            check('T9 已 done 幂等退出', not da.is_trading_day(datetime.date.today()),
                  '交易日已 done 却放行')
        except SystemExit as e:
            check('T9 已 done 幂等退出', e.code == 77, f'exit={e.code}')


def t10_backlog_transitions():
    da._jsonl = _JSONL_ORIG  # 恢复真·读文件（前面用例的 _patch_env 替换过）
    with tempfile.TemporaryDirectory() as tmp:
        bl_fp = os.path.join(tmp, 'backlog.jsonl')
        with open(bl_fp, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'id': 'X-1', 'status': 'open', 'title': 't10 甲'}, ensure_ascii=False) + '\n')
            f.write(json.dumps({'id': 'X-2', 'status': 'open', 'title': 't10 乙'}, ensure_ascii=False) + '\n')
        da.BACKLOG = bl_fp
        da.cmd_backlog(Args(list=False, id='X-1', status='triaged', note='已审', actor='t10'))
        e = [json.loads(ln) for ln in open(bl_fp, encoding='utf-8') if ln.strip()]
        x1 = next(x for x in e if x['id'] == 'X-1')
        check('T10 open→triaged 推进', x1['status'] == 'triaged' and x1['history'][-1]['from'] == 'open')
        _expect_exit('T10 关闭无理由拒绝', lambda: da.cmd_backlog(Args(
            list=False, id='X-1', status='closed', note='', actor='t10')), 2)
        da.cmd_backlog(Args(list=False, id='X-1', status='closed', note='证据:X', actor='t10'))
        e = [json.loads(ln) for ln in open(bl_fp, encoding='utf-8') if ln.strip()]
        x1 = next(x for x in e if x['id'] == 'X-1')
        check('T10 带理由关闭成功', x1['status'] == 'closed')
        _expect_exit('T10 closed 不可逆', lambda: da.cmd_backlog(Args(
            list=False, id='X-1', status='open', note='重开', actor='t10')), 2)
        _expect_exit('T10 非法状态拒绝', lambda: da.cmd_backlog(Args(
            list=False, id='X-2', status='bogus', note='', actor='t10')), 2)
        _expect_exit('T10 未知 id 拒绝', lambda: da.cmd_backlog(Args(
            list=False, id='X-999', status='closed', note='x', actor='t10')), 2)
        e = [json.loads(ln) for ln in open(bl_fp, encoding='utf-8') if ln.strip()]
        check('T10 未触碰条目不受影响', next(x for x in e if x['id'] == 'X-2')['status'] == 'open')


def t11_vol_shadow_history():
    da._jsonl = _JSONL_ORIG  # 恢复真·读文件
    with tempfile.TemporaryDirectory() as tmp:
        da.DEC_DIR = tmp
        da.PROP_DIR = tmp
        hist_fp = os.path.join(tmp, 'vol_shadow_history.json')
        da.VOL_SHADOW_HIST = hist_fp
        da._vol_shadow_orig = da._vol_shadow
        da._vol_shadow = lambda n: {'300010.SZ': {'days': n, 'signals': 43,
                                                  'would_suppress_lowvol': 2}}
        da.cmd_collect(Args())
        hist = json.load(open(hist_fp, encoding='utf-8'))
        digest = json.load(open(os.path.join(tmp, f'{da._today()}.digest.json'), encoding='utf-8'))
        check('T11 shadow 历史落盘', da._today() in hist
              and hist[da._today()]['300010.SZ']['would_suppress_lowvol'] == 2)
        check('T11 digest 含累积日数与提示', digest.get('vol_shadow_days_accumulated') == len(hist)
              and 'shadow 已累积' in str(digest.get('vol_shadow_promote_hint')))
        da._vol_shadow = da._vol_shadow_orig


def _run(fn):
    """单用例崩溃不拖垮全量（意外 SystemExit/异常 = FAIL）。"""
    global FAIL
    try:
        fn()
    except SystemExit as e:
        FAIL += 1
        print(f'  FAIL {fn.__name__} 意外 SystemExit({e.code})')
    except Exception as e:
        FAIL += 1
        print(f'  FAIL {fn.__name__} 异常 {type(e).__name__}: {e}')


def main():
    print('=== test_ai_loop：AI 闭环机械层回归 ===')
    _run(t1_rollback_triggers)
    _run(t2_small_drop_holds)
    _run(t3_observing_when_thin)
    _run(t4_apply_rejects_nonwhitelist)
    _run(t5_apply_rejects_low_z)
    _run(t6_apply_rate_limit)
    _run(t7_apply_success_path)
    _run(t8_finalize_escalation)
    _run(t9_guard_idempotent)
    _run(t10_backlog_transitions)
    _run(t11_vol_shadow_history)
    print(f'\n=== 结果: {PASS} PASS / {FAIL} FAIL ===')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
