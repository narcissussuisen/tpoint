# -*- coding: utf-8 -*-
r"""tests/test_undefined_names.py — 未定义全局名静态回归（2026-09-29）

背景
----
`scripts/daily_closed_loop.py` 的「组合回测」块曾调用 `FO.day_signals(sym, wl[sym], days, eff_atr)`，
而 `eff_atr` 在整个仓库**从未定义**（正确值应为 `FO.CUR_ATR`）。该 NameError 被紧邻的
`except Exception` 捕获，降级成日报正文里一行「组合回测失败：name 'eff_atr' is not defined」——
**每日出现在推送里、却连续多日无人当异常处理**，且该环节（组合回测）事实上从未跑成。

这类缺陷的共同特征是：
  ① 名字写错（拼写/作用域），静态即可发现；
  ② 生产脚本里被宽 except 吞掉 ⇒ 无异常逃逸、无测试覆盖；
  ③ 只在特定分支（此处为「网格提出组合改动」）才触发 ⇒ 手工跑一遍也未必遇到。

⇒ 用 `symtable` 做一次**定义域完备性**检查：模块内任何函数引用的「全局名」必须能在
   模块作用域中找到定义（或为内建 / loader 注入的 dunder）。误报率实测为 0
   （2026-09-29 对 scripts/、core/、根目录全部 *.py 扫描：健康状态下 0 命中；
   修复 eff_atr 前恰命中 1 处）。

局限（有意为之，避免噪声）
  · 不检查属性（`FO.eff_atr` 这类 AttributeError 不在覆盖范围）；
  · 不检查 `import *` 引入的名字（本仓库无此用法；若将来引入需在 ALLOW_NAMES 补白名单）；
  · 只做「名字是否存在」，不做类型/调用签名检查。

运行：venv/Scripts/python.exe tests/test_undefined_names.py
"""
import builtins
import os
import symtable
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# loader 语义注入的模块级 dunder：模块符号表里不会出现，但运行期一定存在
LOADER_DUNDERS = {'__file__', '__name__', '__doc__', '__package__', '__spec__',
                  '__loader__', '__builtins__', '__annotations__'}
BUILTINS = set(dir(builtins)) | LOADER_DUNDERS

# 扫描范围：生产脚本 + 核心库 + 仓库根脚本（不扫 tests/ 与 venv/）
SCAN_DIRS = ('scripts', 'core')
SCAN_ROOT = True

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


def scan_source(src, filename):
    """返回 [(作用域名, 可疑名), ...]。语法错误单独以 ('SYNTAX', msg) 返回。"""
    try:
        top = symtable.symtable(src, filename, 'exec')
    except SyntaxError as e:
        return [('SYNTAX', str(e))]

    module_names = {s.get_name() for s in top.get_symbols()}
    hits = []

    def walk(tbl):
        for sym in tbl.get_symbols():
            n = sym.get_name()
            if sym.is_global() and n not in BUILTINS and n not in module_names:
                hits.append((tbl.get_name(), n))
        for child in tbl.get_children():
            walk(child)

    walk(top)
    return hits


def scan_file(path):
    try:
        with open(path, encoding='utf-8') as f:
            return scan_source(f.read(), path)
    except Exception as e:                                  # noqa: BLE001
        return [('READ_ERROR', repr(e))]


def iter_py_files():
    seen = []
    for base in SCAN_DIRS:
        for dp, dirs, files in os.walk(os.path.join(ROOT, base)):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for f in sorted(files):
                if f.endswith('.py'):
                    seen.append(os.path.join(dp, f))
    if SCAN_ROOT:
        for f in sorted(os.listdir(ROOT)):
            if f.endswith('.py'):
                seen.append(os.path.join(ROOT, f))
    return seen


def main():
    # ---- 0. 自检：检查器必须能抓到「合成 eff_atr 型缺陷」----
    print('\n=== 0. 检查器自检（阳性对照） ===')
    synthetic = (
        'def f(xs, n):\n'
        '    return day_signals(sym, xs, n, eff_atr)\n'
    )
    hits = scan_source(synthetic, '<synthetic>')
    check('S1 能抓到未定义的 eff_atr', ('f', 'eff_atr') in hits, str(hits))

    synthetic_ok = (
        'CUR_ATR = 0.25\n'
        'sym_or_none = 1\n'
        'def day_signals(*a):\n'
        '    return None\n'
        'def g(xs, n):\n'
        '    return day_signals(sym_or_none, xs, n, CUR_ATR)\n'
    )
    hits_ok = scan_source(synthetic_ok, '<synthetic_ok>')
    check('S2 已定义名不误报', hits_ok == [], str(hits_ok))

    # ---- 1. 全仓扫描 ----
    files = iter_py_files()
    print(f'\n=== 1. 扫描 {len(files)} 个 .py（scripts/ + core/ + 根目录） ===')
    bad = {}
    for p in files:
        h = scan_file(p)
        if h:
            bad[os.path.relpath(p, ROOT)] = h
    for rel, h in sorted(bad.items()):
        print(f'  ❌ {rel}: ' + '; '.join(f'{fn}()→{n}' for fn, n in h[:5]))
    check(f'R1 {len(files)} 个文件无未定义全局名', not bad,
          f'{len(bad)} 个文件命中: {sorted(bad)[:5]}')

    # ---- 2. 回归锚点：daily_closed_loop 组合回测块 ----
    dcl = os.path.join(ROOT, 'scripts', 'daily_closed_loop.py')
    if os.path.exists(dcl):
        src = open(dcl, encoding='utf-8').read()
        check('R2 daily_closed_loop 不再引用未定义的 eff_atr',
              'eff_atr' not in src.replace('原为 eff_atr', ''))
        check('R3 daily_closed_loop 组合回测取 FO.CUR_ATR',
              'FO.day_signals(sym, wl[sym], days, FO.CUR_ATR)' in src)
    else:
        check('R2 daily_closed_loop 存在', False, dcl)

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
