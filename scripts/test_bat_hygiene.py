# -*- coding: utf-8 -*-
"""scripts/test_bat_hygiene.py — .bat 三铁律回归（2026-09-10）

背景：`run_daily_review.bat` 的注释里早就写了「本文件所有 REM 注释禁止出现尖括号」，
但**规则没有测试守着，就一定会再犯**。2026-09-10 实测物证：仓库根目录出现了 0 字节文件
`push_feishu_html`，mtime = 2026-09-10 15:30（正好是 `tpoint_daily_review` 的调度时刻）——
成因是 REM 注释里的 `->` 被 cmd 在**执行 REM 之前**当成了输出重定向：
    REM ... -> push_feishu_html 推飞书云链接。
⇒ cmd 先做重定向，于是创建了一个名为 `push_feishu_html` 的空文件，注释的后半段被吞掉。

三条铁律（本测试逐文件强制）：
  ① **只用 CRLF**：孤立 LF 会让 cmd 的行解析错位（换行被吞 → 后半行变独立命令）。
  ② **REM 行禁含 `<` / `>`**：即便在 REM 里，cmd 也会先做重定向 → 静默造垃圾文件 / 报语法错。
     需要表达箭头时用 `→`（U+2192，非 cmd 元字符）。
  ③ **引用的脚本必须存在**：`%ROOT%\\scripts\\X.py|bat` 形式的引用逐个验证，
     防止脚本改名/删除后 bat 静默失败（计划任务只显示 LastResult 非 0，根因难查）。
已知未决项（2026-09-29，未落为自动规则）：
  cmd 按**读盘时**的活动代码页解析 bat 字节。沙盒实测（`%TEMP%/pcbat/minetest.bat`）：同样 6 行中文
  REM，**不带** `chcp 65001` 时其中 2 行吞掉 CRLF、后半段被当成命令执行（stderr 报
  not recognized as a command），带 chcp 则全部正常。既有 6 个 bat 或含 `chcp 65001`、或全 ASCII，
  线上未观察到影响 ⇒ 未升级为铁律。原因：以「行级 chcp 位置」为判据会在本仓库恒红
  （`run_daily_review.bat` 首个中文 REM 在 L2，chcp 在 L21，但生产运行正常），
  而真实的吞行条件依赖具体字节序列，尚未摸清 ⇒ 留作后续机制研究，勿据此改代码。

运行：venv/Scripts/python.exe scripts/test_bat_hygiene.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT_DIR = os.path.join(ROOT, 'scripts')

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


def main():
    bats = sorted(f for f in os.listdir(BAT_DIR) if f.lower().endswith('.bat'))
    print(f'\n=== 待检 .bat 文件 {len(bats)} 个 ===')
    for b in bats:
        print(f'  · {b}')

    for b in bats:
        path = os.path.join(BAT_DIR, b)
        raw = io.open(path, 'rb').read()
        print(f'\n--- {b} ---')

        # ① CRLF
        crlf = raw.count(b'\r\n')
        lone_lf = raw.count(b'\n') - crlf
        check(f'{b} 无孤立 LF（CRLF={crlf}）', lone_lf == 0, f'lone_lf={lone_lf}')

        text = raw.decode('utf-8', 'ignore')
        lines = text.split('\r\n')

        # ② REM 行禁尖括号（只查注释行；引擎行里的 >> 是刻意重定向）
        bad_rem = [(i, ln) for i, ln in enumerate(lines, 1)
                   if ln.lstrip().startswith('REM') and ('<' in ln or '>' in ln)]
        check(f'{b} REM 行无尖括号（防 cmd 误判重定向）', not bad_rem,
              '; '.join(f'L{i}:{ln.strip()[:60]}' for i, ln in bad_rem[:3]))

        # ③ 引用的脚本存在
        refs = set()
        for m in re.finditer(r'%ROOT%[\\/]scripts[\\/]([A-Za-z0-9_\-.]+\.(?:py|bat))', text):
            refs.add(m.group(1))
        missing = sorted(r for r in refs
                         if not os.path.exists(os.path.join(BAT_DIR, r)))
        check(f'{b} 引用的 {len(refs)} 个脚本均存在', not missing, f'缺失={missing}')

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
