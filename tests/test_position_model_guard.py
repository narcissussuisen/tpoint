# -*- coding: utf-8 -*-
"""tests/test_position_model_guard.py — T1.5 仓位模型静态守卫（2026-09-27）

防复发断言（hardening plan v2.1 §T1.5 任务 3）：
  1. 白名单之外的 .py 文件**禁止 import / 调用 simulate_bidirectional**
     （裸卖空 + 成本翻转 bug 的旧模型；正确口径 = core/simulate_position_sm.py）。
  2. 白名单之外的文件**禁止出现 simulate_day(...) + simulate_bidirectional(...)
     相加模式**（同一组信号双重计费）。
  3. core/simulate_position_sm.py 必须存在且导出 simulate_position_sm。

白名单 = 显式冻结的 legacy 文件（定义点 / 已被 simulate_position_sm 取代但保留存档 /
research 研究线（永不进生产））。新代码引用仓位模拟必须经 simulate_position_sm
或显式 has_base 门控。

运行：python tests/test_position_model_guard.py   （无第三方依赖，stdlib only）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# —— 白名单：允许引用 simulate_bidirectional 的冻结 legacy 文件（相对 ROOT）——
# 任何不在此列表的文件出现引用 = FAIL。新增条目必须附理由注释。
WHITELIST = {
    'core/simulate_bidirectional.py',      # 定义点本身
    'core/simulate_base_position.py',      # legacy：已被 simulate_position_sm 取代，存档保留
    'core/simulate_position_sm.py',        # 新模型 docstring 中的历史说明（仅注释）
    'core/composite_scorer_v4full.py',     # 仅 docstring 提及（兼容说明），无调用
    'core/general_signal.py',              # 仅 docstring 提及，无调用
    'tests/test_position_sm.py',           # 测试 docstring 中的 bug 描述
    'tests/test_position_model_guard.py',  # 本守卫自身（检测串）
    'scripts/evaluate_base_position.py',   # 仅 docstring 提及（对账缺陷说明）
    # —— 冻结 legacy 脚本（deprecated，禁止新引用；处置见重建方案 §七）——
    'scripts/stop_exit_ab.py',             # legacy 研究脚本
    'scripts/topbot_diag.py',              # legacy 诊断脚本
    'scripts/topbot_replay.py',            # legacy 复盘绘图
    'scripts/v4_param_search.py',          # deprecated（v4 参数搜索旧口径）
    'scripts/backtest_v3_exit.py',         # deprecated（exit_compare 旧口径对照）
    'scripts/evaluate_det_bidirectional.py',  # deprecated（DET v1，统一走 v2+随机对照）
    'scripts/_p3_revtest.py',              # legacy 临时脚本
    'scripts/p3_verify_sideaware.py',      # legacy 研究脚本
    'scripts/research/',                   # research 线整体豁免（永不进生产）
}

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail else ""))


def _whitelisted(rel):
    rel = rel.replace(os.sep, '/')
    for w in WHITELIST:
        if rel == w or rel.startswith(w):
            return True
    return False


def _iter_py():
    for base in ('core', 'scripts', 'tests'):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for fn in filenames:
                if fn.endswith('.py'):
                    yield os.path.relpath(os.path.join(dirpath, fn), ROOT)


def _code_lines(path):
    """剥离注释与 docstring 后的近似代码行（import/call 检测用）。
    简化处理：去掉 # 行尾注释与三引号块。对守卫目的足够。"""
    with open(path, encoding='utf-8', errors='replace') as f:
        src = f.read()
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    out = []
    for line in src.splitlines():
        line = line.split('#', 1)[0]
        if line.strip():
            out.append(line)
    return out


def main():
    offenders_import = []
    offenders_add = []

    for rel in _iter_py():
        rel_n = rel.replace(os.sep, '/')
        if _whitelisted(rel_n):
            continue
        path = os.path.join(ROOT, rel)
        lines = _code_lines(path)
        has_bidir = any('simulate_bidirectional' in ln for ln in lines)
        if has_bidir:
            offenders_import.append(rel_n)
        has_day_call = any(re.search(r'\bsimulate_day\s*\(', ln) for ln in lines)
        has_bidir_call = any(re.search(r'\bsimulate_bidirectional\s*\(', ln) for ln in lines)
        if has_day_call and has_bidir_call:
            offenders_add.append(rel_n)

    check('白名单外零 simulate_bidirectional 引用', not offenders_import,
          '; '.join(offenders_import) if offenders_import else 'clean')
    check('白名单外零 simulate_day+bidirectional 相加模式', not offenders_add,
          '; '.join(offenders_add) if offenders_add else 'clean')

    sm_path = os.path.join(ROOT, 'core', 'simulate_position_sm.py')
    ok_sm = os.path.exists(sm_path)
    if ok_sm:
        with open(sm_path, encoding='utf-8') as f:
            ok_sm = 'def simulate_position_sm(' in f.read()
    check('core/simulate_position_sm.py 存在且导出 simulate_position_sm', ok_sm)

    print(f"\nRESULT: {'PASS' if not FAIL else 'FAIL'} "
          f"({len(PASS)} passed, {len(FAIL)} failed)")
    if FAIL:
        print('FAILED: ' + '; '.join(FAIL))
        print('修复指引：新代码引用仓位模拟必须经 core/simulate_position_sm.py '
              '或显式 has_base>0 门控；确需保留的 legacy 文件加入 WHITELIST 并附理由。')
    return 0 if not FAIL else 1


if __name__ == '__main__':
    sys.exit(main())
