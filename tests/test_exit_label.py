# -*- coding: utf-8 -*-
"""tests/test_exit_label.py — P6 止损标签解耦回归测试（纯脚本模式，无 pytest 依赖）

运行：venv/Scripts/python.exe tests/test_exit_label.py
覆盖：
  1. EXIT_LABEL_MAP 含全部 6 个 exit_reason
  2. 标签非空且互不相同（差异化，不再坍缩成单一「止损」）
  3. TRAIL 语义正确（移动止盈，不是止损）—— 本次 P6 核心 bug 修复验证
  4. monitor.py 已引用 EXIT_LABEL_MAP（不再硬编码坍缩分支）
  5. label_for 未知 reason 走保守兜底
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
from exit_label import EXIT_LABEL_MAP, EXIT_LABEL_ORDER, label_for  # noqa: E402

REQUIRED = {'FIXSTOP', 'STOP', 'S', 'TRAIL', 'TIME', 'EOD'}
FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f'  PASS  {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL  {name}  {detail}')


def main():
    print('=== P6 test_exit_label.py ===')

    # 1) 6 个 exit_reason 全部有映射
    missing = REQUIRED - set(EXIT_LABEL_MAP.keys())
    extra = set(EXIT_LABEL_MAP.keys()) - REQUIRED
    check('6 reasons covered', not missing and not extra,
          f'missing={missing} extra={extra}')

    # 2) 标签互不相同（差异化）
    labels = [v[0] for v in EXIT_LABEL_MAP.values()]
    check('labels unique', len(set(labels)) == len(labels), f'dup={[l for l in labels if labels.count(l) > 1]}')
    check('labels non-empty', all(len(l) >= 2 for l in labels))

    # 2b) P12 推送分级：每个 reason 有合法 level（action/remind/positive/info）
    valid_levels = {'action', 'remind', 'positive', 'info'}
    lvls = set(v[2] for v in EXIT_LABEL_MAP.values())
    check('levels valid', lvls <= valid_levels, f'bad={lvls - valid_levels}')
    check('STOP is action-level', EXIT_LABEL_MAP['STOP'][2] == 'action')
    check('TRAIL is positive-level', EXIT_LABEL_MAP['TRAIL'][2] == 'positive')

    # 3) 核心 bug 修复：TRAIL 语义 = 移动止盈（不是「止损」）；STOP 与 TRAIL 标签不同
    check('TRAIL != 止损', EXIT_LABEL_MAP['TRAIL'][0] != '止损',
          f"got {EXIT_LABEL_MAP['TRAIL'][0]}")
    check('STOP != TRAIL', EXIT_LABEL_MAP['STOP'][0] != EXIT_LABEL_MAP['TRAIL'][0],
          f"STOP={EXIT_LABEL_MAP['STOP'][0]} TRAIL={EXIT_LABEL_MAP['TRAIL'][0]}")
    check('FIXSTOP label', EXIT_LABEL_MAP['FIXSTOP'][0] == '固定止损')
    check('EOD label', EXIT_LABEL_MAP['EOD'][0] == '收盘强平')

    # 4) 配色齐全且合法
    valid_colors = {'red', 'orange', 'blue', 'green', 'grey', 'purple'}
    colors = set(v[1] for v in EXIT_LABEL_MAP.values())
    check('colors valid', colors <= valid_colors, f'bad={colors - valid_colors}')

    # 5) EXIT_LABEL_ORDER 与 MAP 键一致
    check('order covers all', set(EXIT_LABEL_ORDER) == set(EXIT_LABEL_MAP.keys()))

    # 6) monitor.py 已引用 EXIT_LABEL_MAP，且旧的坍缩硬编码分支已移除
    monitor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core', 'monitor.py')
    src = open(monitor_path, encoding='utf-8').read()
    check('monitor imports EXIT_LABEL_MAP', 'EXIT_LABEL_MAP' in src)
    check('monitor uses label_for', 'label_for(' in src)
    check('old collapsed branch removed', "('STOP', 'TRAIL', 'TIME')" not in src,
          '旧坍缩分支应已移除')

    # 7) label_for 未知 reason 兜底（三元组）
    lb, _col, _lvl = label_for('__unknown__')
    check('label_for fallback', lb == '卖出')

    print()
    if FAILURES:
        print(f'RESULT: FAIL ({len(FAILURES)} failed) -> {FAILURES}')
        sys.exit(1)
    print('RESULT: PASS (all exit_label tests)')


if __name__ == '__main__':
    main()
