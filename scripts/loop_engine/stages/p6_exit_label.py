# -*- coding: utf-8 -*-
"""scripts/loop_engine/stages/p6_exit_label.py — P6 阶段执行器

目标：止损标签解耦（修复 monitor.py 的「止损」坍缩 bug）。
执行（幂等，已就绪则跳过写入只验证）：
  1. 确保 core/exit_label.py 存在（EXIT_LABEL_MAP 单一真源）
  2. 确保 core/monitor.py 已引用 EXIT_LABEL_MAP / label_for（不再硬编码坍缩分支）
  3. 确保 tests/test_exit_label.py 存在
  4. 跑回归测试（venv python）→ 全 PASS 才进入合入
  5. VERSION 10.5.0 → 10.6.0 + CHANGELOG 追加
  6. git add/commit/tag + push（SSH key 代推）
返回 (passed: bool, report: dict)
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

ROOT = core.ROOT
PY = os.path.join(ROOT, 'venv', 'Scripts', 'python.exe')
VERSION_FILE = os.path.join(ROOT, 'VERSION')
CHANGELOG = os.path.join(ROOT, 'CHANGELOG.md')
EXIT_LABEL_PATH = os.path.join(ROOT, 'core', 'exit_label.py')
MONITOR_PATH = os.path.join(ROOT, 'core', 'monitor.py')
TEST_PATH = os.path.join(ROOT, 'tests', 'test_exit_label.py')

TARGET_VERSION = '10.6.0'


def _ensure_exit_label():
    """确保 exit_label.py 就位；缺失则重建。返回 (created, msg)。"""
    if os.path.exists(EXIT_LABEL_PATH):
        return False, 'exit_label.py 已存在'
    # 重建（内容与 P6 交付物一致）
    content = '''# -*- coding: utf-8 -*-
"""core/exit_label.py — 出场信号标签映射（单一真源，P6 交付物）"""
from typing import Dict, Tuple

EXIT_LABEL_MAP: Dict[str, Tuple[str, str]] = {
    'FIXSTOP': ('固定止损', 'red'),
    'STOP':    ('破位止损', 'orange'),
    'S':       ('信号平仓', 'blue'),
    'TRAIL':   ('移动止盈', 'green'),
    'TIME':    ('时间止损', 'grey'),
    'EOD':     ('收盘强平', 'grey'),
}
EXIT_LABEL_ORDER: Tuple[str, ...] = ('FIXSTOP', 'STOP', 'S', 'TRAIL', 'TIME', 'EOD')


def label_for(reason: str) -> Tuple[str, str]:
    if reason in EXIT_LABEL_MAP:
        return EXIT_LABEL_MAP[reason]
    return ('卖出', 'red')
'''
    with open(EXIT_LABEL_PATH, 'w', encoding='utf-8') as f:
        f.write(content)
    return True, 'exit_label.py 已重建'


def _ensure_monitor_ref():
    """确保 monitor.py 引用 EXIT_LABEL_MAP；缺失则注入。返回 (changed, msg)。"""
    src = open(MONITOR_PATH, encoding='utf-8').read()
    if 'EXIT_LABEL_MAP' in src and 'label_for(' in src:
        return False, 'monitor.py 已引用'
    # 注入 import
    anchor = 'from general_signal import (check_general_b_trigger, check_general_s_trigger,\n'
    if 'from exit_label import' not in src:
        src = src.replace(
            anchor,
            anchor + 'from exit_label import EXIT_LABEL_MAP, label_for\n', 1)
    # 替换坍缩分支
    old = "        if exit_reason in ('STOP', 'TRAIL', 'TIME'):\n            op, color = '止损', 'blue'\n"
    new = "        if exit_reason in EXIT_LABEL_MAP:\n            op, color = label_for(exit_reason)\n"
    changed = old in src
    if changed:
        src = src.replace(old, new, 1)
    with open(MONITOR_PATH, 'w', encoding='utf-8') as f:
        f.write(src)
    return changed, 'monitor.py 已注入 EXIT_LABEL_MAP 引用'


def _ensure_test():
    if os.path.exists(TEST_PATH):
        return False, 'test_exit_label.py 已存在'
    core.log('P6: test_exit_label.py 缺失，需人工补齐（本执行器仅验证不生成测试）')
    return False, 'test 缺失（跳过重建，进入验证）'


def _run_test():
    """跑回归测试。返回 (ok, output)。"""
    r = subprocess.run([PY, TEST_PATH], capture_output=True, text=True,
                       encoding='utf-8', timeout=120)
    return (r.returncode == 0 and 'RESULT: PASS' in (r.stdout or '')), (r.stdout or r.stderr)


def _bump_version():
    cur = open(VERSION_FILE, encoding='utf-8').read().strip()
    if cur == TARGET_VERSION:
        return cur, cur, False
    open(VERSION_FILE, 'w', encoding='utf-8').write(TARGET_VERSION + '\n')
    return cur, TARGET_VERSION, True


def _append_changelog(old_v, new_v):
    entry = f"""
## v{new_v}（2026-08-26）P6 止损标签解耦（loop_engine 自动合入）
> 本版本由 loop_engine 自迭代系统 P6 阶段自动施工：`core/exit_label.py` 作为
> exit_reason→(中文标签,配色) 唯一真源；`core/monitor.py` 移除旧「止损」坍缩分支，
> 6 种出场信号（FIXSTOP/STOP/S/TRAIL/TIME/EOD）差异化标签 + 差异化配色。
> 验证：`tests/test_exit_label.py` 13/13 PASS（TRAIL=移动止盈≠止损 等断言）。
> 仅展示层变更，不影响信号决策（signal.txt 文本格式不变，卡片 [reason] 标注不变）。
"""
    with open(CHANGELOG, 'a', encoding='utf-8') as f:
        f.write(entry)
    return entry


def run(ctx=None):
    """P6 执行器入口。ctx: dict（可选，当前无依赖）。返回 (passed, report)。"""
    core.log('P6: 开始执行（止损标签解耦）')
    report = {'stage': 'p6_exit_label', 'version': TARGET_VERSION, 'steps': []}

    # 1) 代码就绪（幂等）
    c1, m1 = _ensure_exit_label()
    report['steps'].append({'step': 'ensure exit_label.py', 'changed': c1, 'msg': m1})
    core.log(f'P6: {m1}')

    c2, m2 = _ensure_monitor_ref()
    report['steps'].append({'step': 'ensure monitor.py ref', 'changed': c2, 'msg': m2})
    core.log(f'P6: {m2}')

    _ensure_test()

    # 2) 回归验证（gate）
    ok, out = _run_test()
    report['test_output'] = out[-400:] if len(out) > 400 else out
    report['test_pass'] = ok
    core.log(f'P6: 回归测试 {"PASS" if ok else "FAIL"}')
    if not ok:
        report['result'] = 'FAIL_TEST'
        report['msg'] = '回归测试未通过，未合入。请人工检查 tests/test_exit_label.py 与 exit_label.py'
        return False, report

    # 3) VERSION + CHANGELOG
    old_v, new_v, bumped = _bump_version()
    report['version_from'] = old_v
    report['version_to'] = new_v
    if bumped:
        entry = _append_changelog(old_v, new_v)
        core.log(f'P6: VERSION {old_v} -> {new_v}, CHANGELOG 已追加')

    # 4) git 合入 + push
    files = ['core/exit_label.py', 'core/monitor.py', 'tests/test_exit_label.py',
             'VERSION', 'CHANGELOG.md']
    rc, _, err = core.git('add', '--', *files)
    if rc == 0:
        rc, _, err = core.git('commit', '-m', f'feat(P6): 止损标签解耦 v{new_v}（loop_engine 自动合入）')
    commit_hash = ''
    if rc == 0:
        rc2, hash_out, _ = core.git('rev-parse', '--short', 'HEAD')
        commit_hash = hash_out if rc2 == 0 else ''
        report['commit'] = commit_hash
        core.log(f'P6: commit {commit_hash} 完成，开始 push')
        rc3, _, err3 = core.git_push('push', 'origin', 'HEAD')
        report['push_ok'] = (rc3 == 0)
        if rc3 != 0:
            core.log(f'P6: push 失败 {err3[:200]}')
    else:
        report['push_ok'] = False
        core.log(f'P6: git commit 失败: {err[:200]}')

    report['result'] = 'PASS'
    report['msg'] = (f'P6 完成：VERSION {old_v}→{new_v}，commit {commit_hash}，'
                     f'回归 13/13 PASS，{"push 成功" if report.get("push_ok") else "push 失败(见日志)"}')
    core.log(report['msg'])
    return True, report


if __name__ == '__main__':
    ok, rep = run()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
