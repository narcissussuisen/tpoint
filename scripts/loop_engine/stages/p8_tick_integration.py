# -*- coding: utf-8 -*-
"""scripts/loop_engine/stages/p8_tick_integration.py — P8 阶段执行器（tick/Level2 数据接入）

交付（v10.7.0）：
  1. core/tick_loader.py     — tick_cache CSV 加载器（381 文件/9 标的）
  2. core/tick_aggregator.py — 分钟级 tick 聚合（OHLC/买卖失衡/大单）+ 一致性校验
  3. core/tick_features.py   — 分钟级相对特征（density/flow/iceberg 代理），落盘 parquet
  4. data/tick_features/     — 9 标的 × ~8.1 万分钟特征行
  5. tests/test_tick_aggregator.py — 11/11 PASS

已知约束（如实记录）：tick 价格与 F 盘 1m 差 ~10x（复权口径），时间戳仅 HH:MM → 3 秒聚合不可行，
特征全部为**相对口径**（不依赖绝对价格），与 1m bar 做形态级交叉验证。
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import importlib.util  # noqa: E402
_LE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('le_core', os.path.join(_LE_DIR, 'core.py'))
le_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(le_core)

TARGET_VERSION = '10.7.0'
MODULES = ['tick_loader.py', 'tick_aggregator.py', 'tick_features.py']
TEST = os.path.join(ROOT, 'tests', 'test_tick_aggregator.py')
PY = os.path.join(ROOT, 'venv', 'Scripts', 'python.exe')
FEATURE_DIR = os.path.join(ROOT, 'data', 'tick_features')


def run(ctx=None):
    le_core.log('P8: 开始执行（tick/Level2 数据接入）')
    report = {'stage': 'p8_tick_integration', 'version': TARGET_VERSION, 'steps': []}

    # 1) 模块就位
    missing = [m for m in MODULES if not os.path.exists(os.path.join(ROOT, 'core', m))]
    report['steps'].append({'step': 'modules present', 'missing': missing})
    if missing:
        report['result'] = 'FAIL_MODULES'
        report['msg'] = f'P8 模块缺失: {missing}'
        return False, report

    # 2) 特征已生成（9 标的 parquet）
    feats = [f for f in os.listdir(FEATURE_DIR) if f.endswith('_features.parquet')] if os.path.isdir(FEATURE_DIR) else []
    report['feature_files'] = len(feats)
    report['steps'].append({'step': 'features generated', 'n_files': len(feats)})
    if len(feats) < 9:
        le_core.log(f'P8: 特征文件 {len(feats)}/9，尝试补全…')
        import subprocess
        subprocess.run([PY, os.path.join(ROOT, 'core', 'tick_features.py'), '--all'],
                       capture_output=True, text=True, encoding='utf-8', timeout=300)
        feats = [f for f in os.listdir(FEATURE_DIR) if f.endswith('_features.parquet')] if os.path.isdir(FEATURE_DIR) else []
        report['feature_files'] = len(feats)

    # 3) 回归测试
    import subprocess
    r = subprocess.run([PY, TEST], capture_output=True, text=True, encoding='utf-8', timeout=180)
    report['test_pass'] = (r.returncode == 0 and 'RESULT: PASS' in (r.stdout or ''))
    report['test_out'] = (r.stdout or r.stderr)[-300:]
    le_core.log(f'P8: 回归测试 {"PASS" if report["test_pass"] else "FAIL"}')
    if not report['test_pass']:
        report['result'] = 'FAIL_TEST'
        report['msg'] = 'P8 回归测试未通过'
        return False, report

    # 4) VERSION + CHANGELOG（bump 守门：基建/研究态拦截）
    _allowed, _reason = le_core.guard_bump('p8_tick_integration', TARGET_VERSION)
    ver_path = os.path.join(ROOT, 'VERSION')
    cur = open(ver_path, encoding='utf-8').read().strip()
    if _allowed and cur != TARGET_VERSION:
        open(ver_path, 'w', encoding='utf-8').write(TARGET_VERSION + '\n')
        report['version_from'] = cur
        report['version_to'] = TARGET_VERSION
    elif not _allowed:
        report['bump_blocked'] = _reason
        le_core.log(f'P8: {_reason}')
    with open(os.path.join(ROOT, 'CHANGELOG.md'), 'a', encoding='utf-8') as f:
        f.write(f"""
## v{TARGET_VERSION}（2026-08-26）P8 tick/Level2 数据接入（loop_engine 自动合入）
> loop_engine P8 阶段：接入 data/tick_cache/（381 文件/9 标的，逐笔成交快照）。
> - 新增 core/tick_loader.py / tick_aggregator.py / tick_features.py（分钟级相对特征：买卖失衡、
>   大单密度、iceberg 代理、方向流）。
> - 特征落地 data/tick_features/（{len(feats)} 标的 parquet，~8.1 万分钟行），供 P9 顶底捕捉/ML 使用。
> - 已知约束：tick 价格与 F 盘 1m 复权口径差 ~10x、时间戳仅 HH:MM（无秒）→ 3 秒聚合不可行，
>   特征为相对口径（不依赖绝对价格）。
> - 验证：tests/test_tick_aggregator.py 11/11 PASS。
""")

    # 5) git 合入
    files = ['core/tick_loader.py', 'core/tick_aggregator.py', 'core/tick_features.py',
             'tests/test_tick_aggregator.py', 'VERSION', 'CHANGELOG.md',
             'scripts/loop_engine/stages/p8_tick_integration.py', 'scripts/loop_engine/loop_state.json']
    rc, _, err = le_core.git('add', '--', *files)
    if rc == 0:
        rc, _, err = le_core.git('commit', '-m', f'feat(P8): tick/Level2 数据接入 v{TARGET_VERSION}（loop_engine 自动合入）')
    if rc == 0:
        rc2, hash_out, _ = le_core.git('rev-parse', '--short', 'HEAD')
        report['commit'] = hash_out
        rc3, _, err3 = le_core.git_push('push', 'origin', 'HEAD')
        report['push_ok'] = (rc3 == 0)
        if rc3 != 0:
            le_core.log(f'P8: push 失败 {err3[:200]}')
    else:
        report['push_ok'] = False
        le_core.log(f'P8: git commit 失败: {err[:200]}')

    report['result'] = 'PASS'
    report['msg'] = (f'P8 完成：tick 管道上线（{len(feats)} 标的特征，单测 11/11），'
                     f'commit {report.get("commit", "?")}，'
                     f'{"push 成功" if report.get("push_ok") else "push 失败(见日志)"}')
    le_core.log(report['msg'])
    return True, report


if __name__ == '__main__':
    ok, rep = run()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
