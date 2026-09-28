# -*- coding: utf-8 -*-
r"""spec_freeze.py — RSI 循环的 spec 冻结面与黑名单硬校验（P2.1，2026-09-28）

RSI（递归自我改进）循环的 AI 驱动边界（docs/rsi_loop_agenda.md §边界）：
  参数层 = daily_agent.apply（三重闸门）
  代码层 = rsi_agent.apply-code（四道闸 + spec_hash 不变 + 黑名单外 + 60 天队列）
  spec 层 = 永不自动（本模块的冻结面）

本模块提供三件事：
  spec_hash()        对 spec 面文件算 canonical hash——代码层改动不得改变它
                     （变了 = 判据/口径语义变化 = 锁人审，对齐 EvoAlpha Curator spec_hash 规则）。
  FROZEN_SURFACE     永不被 RSI 触碰的黑名单（apply-code 硬校验，patch 任一文件命中即拒绝）。
  validate_patch()   patch.json 结构校验（必填字段 + 文件清单 + 黑名单 + spec_hash 比对）。

用法（rsi_agent 调用 / 单独核查）：
  python scripts/ai_loop/spec_freeze.py hash                     # 打印当前 spec_hash
  python scripts/ai_loop/spec_freeze.py check <patch.json>       # 校验一个 patch
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RSI_DIR = os.path.join(ROOT, 'data', 'rsi')

# --------------------------------------------------------------------------- #
# spec 冻结面：这些文件的「语义」构成 tpoint 的验收口径与判据体系。
# RSI 代码层改动不得改变它们（hash 变化 = 锁人审提案卡，无自动通道）。
# 注意：修复类变更（如 samebar 方案 A）本身经人审裁决授权后，
# 由人工/授权执行者重算并落盘新基准 hash——RSI 循环只是执行者与守卫。
# --------------------------------------------------------------------------- #
SPEC_SURFACE = [
    'core/general_signal.py',            # GT-1.0 判据语义（GeneralConfig 默认位）
    'core/simulate_position_sm.py',      # 成交口径（exec_delay_bars 语义）
    'docs/player_benchmark.md',          # T0/T1/T2 验收阈值
    'docs/knowledge_alignment_map.md',   # K1-K7 判据真源 + 判读纪律
    'docs/rsi_loop_agenda.md',           # RSI 循环操作程序（边界自描述）
]

# 永不自动触碰（FROZEN_SURFACE 黑名单）：patch.files 任一命中即整单拒绝。
# data/monitor_config.json 的唯一变更通道 = effect_ledger.apply_config_change
# （daily_agent.apply / 人审）；data/watchlist.json = 人审专用。
FROZEN_SURFACE = [
    'core/monitor.py',                   # 实盘推送链（生产命脉）
    'data/watchlist.json',               # 持仓真相源（人审专用）
    'data/monitor_config.json',          # 参数真相源（effect_ledger 唯一通道）
    'docs/player_benchmark.md',          # 验收阈值（spec 层）
    'docs/knowledge_alignment_map.md',   # 判据真源（spec 层）
    'VERSION',
    'METHODOLOGY_VERSION',
    'config.json',                       # monitor 模块常量（根 config）
    'config/monitor_config.json',        # alert_engine 规则
    'docs/rsi_loop_agenda.md',           # RSI 边界自描述（spec 层）
    'scripts/ai_loop/spec_freeze.py',    # 本模块（守卫不自改）
    'scripts/ai_loop/gates.py',          # 闸门定义（守卫不自改）
]

PATCH_REQUIRED_KEYS = ('proposal_id', 'hypothesis', 'knowledge_clause', 'files',
                       'validation_plan', 'rollback', 'spec_hash_expected')


def _canonical(path):
    """读文件并做 canonical 化（统一换行）后 hash。"""
    with open(path, 'rb') as f:
        raw = f.read()
    text = raw.decode('utf-8', errors='replace').replace('\r\n', '\n')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def spec_hash():
    """spec 面 canonical hash（文件顺序固定；任一文件缺失 = fail-loud）。"""
    parts = []
    for rel in SPEC_SURFACE:
        fp = os.path.join(ROOT, rel)
        if not os.path.exists(fp):
            raise FileNotFoundError(f'spec 面文件缺失: {rel}')
        parts.append(f'{rel}:{_canonical(fp)}')
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def save_baseline(note=''):
    """把当前 spec_hash 落盘为 RSI 基准（仅授权修复后/初次部署时调用）。"""
    os.makedirs(RSI_DIR, exist_ok=True)
    fp = os.path.join(RSI_DIR, 'spec_baseline.json')
    doc = {'spec_hash': spec_hash(), 'updated_at': __import__('datetime').datetime.now()
           .strftime('%Y-%m-%d %H:%M:%S'), 'note': note,
           'surface': list(SPEC_SURFACE)}
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return fp


def load_baseline():
    fp = os.path.join(RSI_DIR, 'spec_baseline.json')
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _norm_rel(p):
    """patch 里的文件路径归一（正斜杠、去 ./）。"""
    return os.path.normpath(str(p).replace('\\', '/')).replace('\\', '/')


def validate_patch(patch_fp):
    """校验 patch.json：结构 / 黑名单 / spec_hash。返回 (ok, reasons, patch)。
    ok=False 时 reasons 逐条给出拒绝原因（rc=2 拒绝 / rc=3 结构坏）。"""
    try:
        with open(patch_fp, encoding='utf-8') as f:
            patch = json.load(f)
    except Exception as e:
        return False, [f'patch JSON 无法解析: {e}'], None

    reasons = []
    missing = [k for k in PATCH_REQUIRED_KEYS if k not in patch]
    if missing:
        reasons.append(f'缺必填字段: {missing}')
        return False, reasons, patch

    files = patch['files']
    if not isinstance(files, list) or not files:
        reasons.append('files 必须是非空数组')
        return False, reasons, patch

    frozen = set(_norm_rel(p) for p in FROZEN_SURFACE)
    for frel in files:
        rel = _norm_rel(frel if isinstance(frel, str) else frel.get('path', ''))
        if not rel:
            reasons.append(f'files 项缺 path: {frel!r}')
            continue
        if rel in frozen:
            reasons.append(f'⛔ 黑名单命中（永不自动触碰）: {rel}')
        # 仓库外路径防线（绝对路径/上跳）
        if os.path.isabs(rel) or rel.startswith('..'):
            reasons.append(f'⛔ 仓库外路径: {rel}')
        # 目标不存在 = 合法新建文件（patch 可创建新脚本），不拒；仅信息提示。

    # spec_hash 比对（patch 声明的期望 hash vs 当前实际 hash vs 落盘基准）
    try:
        cur = spec_hash()
    except FileNotFoundError as e:
        reasons.append(f'⛔ {e}')
        cur = None
    expected = patch.get('spec_hash_expected')
    if cur is not None and expected is not None and expected != cur:
        reasons.append(f'⛔ spec_hash 漂移（patch 期望 {expected[:12]}… vs 当前 {cur[:12]}…）'
                       '——判据/口径语义已变化，锁人审提案卡（无自动通道）')

    return (not reasons), reasons, patch


def main():
    ap = argparse.ArgumentParser(description='RSI spec 冻结面守卫')
    ap.add_argument('cmd', choices=('hash', 'check', 'baseline'))
    ap.add_argument('patch', nargs='?', help='check: patch.json 路径')
    ap.add_argument('--note', default='', help='baseline: 落盘备注')
    a = ap.parse_args()
    if a.cmd == 'hash':
        print(spec_hash())
    elif a.cmd == 'baseline':
        print(save_baseline(note=a.note or 'RSI 初始部署基准'))
        print(spec_hash())
    elif a.cmd == 'check':
        if not a.patch:
            ap.error('check 需要 patch.json 路径')
        ok, reasons, patch = validate_patch(a.patch)
        for r in reasons:
            print(r)
        print('PATCH_' + ('OK' if ok else 'REJECTED'))
        sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
