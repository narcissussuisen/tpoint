# -*- coding: utf-8 -*-
"""scripts/loop_engine/loop_engine.py — tpoint loop engineering 自迭代主循环

设计（loop engineering / 全自动闭环，替代人工逐条提示）：
  1. 状态机：loop_state.json 记录 5 个阶段（P6→P10），每阶段 pending/in_progress/done/failed
  2. 触发：`python loop_engine.py --run-once`（由 schtasks 每交易日 15:05 调用）
     `--stage p6_exit_label` 可指定；缺省自动推进到下一个 pending
  3. 执行：调用 stages/<stage>.py 的 run(ctx) → (passed, report)
  4. 验证：stage 内部自带 gate（回归测试 / OOS 回测达标判定）
  5. 合入：stage 内部自动 git commit + push（SSH key 代推）
  6. 推送：每轮结果推送飞书自迭代群 a35d7f52 + 全局状态群 b4eba7a9 一行摘要
  7. 迭代：passed → 状态 done + 自动推进下一阶段；failed → retry+1 并告警

CLI：
  python loop_engine.py --run-once            # 跑当前阶段一轮（推荐，每日触发）
  python loop_engine.py --stage p6_exit_label # 强制指定阶段（重试用）
  python loop_engine.py --status              # 只读状态
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

STAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stages')


def _load_stage(name):
    mod = __import__(f'stages.{name}', fromlist=['run'])
    return mod.run


def _run_stage(name, ctx):
    core.log(f'LOOP: 开始阶段 {name}')
    try:
        run_fn = _load_stage(name)
        passed, report = run_fn(ctx or {})
        return passed, report
    except Exception as e:
        core.log(f'LOOP: 阶段 {name} 异常: {e!r}')
        return False, {'stage': name, 'result': 'EXCEPTION', 'error': repr(e)}


def run_once(force_stage=None):
    st = core.load_state()
    stage = force_stage or core.current_stage(st)
    if stage is None:
        core.log('LOOP: 所有阶段已完成（P6-P10 全闭环）')
        core.push_safe('✅ loop_engine：P6-P10 全部阶段已完成，进入监控态。')
        return 0

    s = st['stages'].setdefault(stage, {'status': 'pending', 'retry': 0, 'last_run': None, 'report': None})
    s['status'] = 'in_progress'
    s['retry'] = s.get('retry', 0) + 1
    core.save_state(st)

    # 执行前推送：阶段启动
    core.push_safe(f'🔄 loop_engine 启动阶段 {stage}（第 {s["retry"]} 轮）…', core.HOOK_GLOBAL)

    passed, report = _run_stage(stage, {'stage': stage})

    # 更新状态
    if passed:
        s['status'] = 'done'
        st['history'].append({'stage': stage, 'ts': core.time.strftime('%Y-%m-%d %H:%M:%S'),
                              'result': 'PASS', 'report': report})
        core.save_state(st)
        # 自动推进
        nxt = core.current_stage(st)
        core.push_safe(f'✅ P6 阶段 {stage} PASS\n{report.get("msg", "")}')
        core.push_safe(f'✅ loop_engine {stage} 完成 → 下一阶段: {nxt or "全部完成"}', core.HOOK_GLOBAL)
        core.log(f'LOOP: {stage} PASS -> next={nxt}')
    else:
        s['status'] = 'failed'
        st['history'].append({'stage': stage, 'ts': core.time.strftime('%Y-%m-%d %H:%M:%S'),
                              'result': 'FAIL', 'report': report})
        core.save_state(st)
        core.push_safe(f'❌ loop_engine 阶段 {stage} FAIL（第 {s["retry"]} 轮）\n{report.get("msg", "")}')
        core.push_safe(f'❌ 阶段 {stage} 失败需处理，详见 logs/loop_engine.log', core.HOOK_GLOBAL)
        core.log(f'LOOP: {stage} FAIL')
    return 0 if passed else 1


def show_status():
    st = core.load_state()
    print(json.dumps(st, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description='tpoint loop engineering 自迭代主循环')
    ap.add_argument('--run-once', action='store_true', help='跑当前阶段一轮（每日触发入口）')
    ap.add_argument('--stage', default=None, help='强制指定阶段（如 p6_exit_label）')
    ap.add_argument('--status', action='store_true', help='只读状态')
    a = ap.parse_args()

    if a.status:
        show_status()
        return 0

    if not core.acquire_lock(timeout=60):
        core.log('LOOP: 另一实例运行中，本轮跳过')
        return 1
    try:
        return run_once(force_stage=a.stage)
    finally:
        core.release_lock()


if __name__ == '__main__':
    sys.exit(main())
