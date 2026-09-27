#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""daily_iterate.py — tpoint 每日自迭代（2026-08-04 晚用户指令：自迭代频率 每周→每交易日）

每日复盘报告产出后自动执行：
1) 跑 factor_optimizer（F盘全历史网格寻优，回测检验）
2) 达标参数按护栏自动写 monitor_config.json（热重载，次日开盘生效）：
   - 仅自动应用「GT 引擎真实消费的 per-symbol 参数」：buy_threshold / sell_threshold /
     signal_gap / min_hist_diff / vol_ratio_b_max（写入 per_symbol[sym]['general_algorithm']，
     经 effect_ledger 账本优先写入；[2026-09-27] atr_min_pct/mpr 已退役——GT 引擎下死参数）
   - trail（全局+用户锁定 0.4/0.6）与 tune_pool_40 未复核项 → 仅出建议，不自动改
   - 门槛：净胜率 ≥+1pp 且 n≥30；薄样本标的（<80d）≥+2pp；随机对照 z<1.0 → 仅建议
3) git 版本记录：有变更 → VERSION 小版本 bump（9.3.x）+ CHANGELOG + commit + tag；
   核心算法/大改动走周五评审大版本（minor）
4) 迭代摘要推 a35d7f52（动态标题）

CLI：python scripts/daily_iterate.py --date 2026-08-04
"""
import os, sys, json, argparse, subprocess, datetime, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# [2026-09-25] 交易日历单一真源 + 非交易日守卫（休市日无信号，迭代无意义且会污染状态）
sys.path.insert(0, os.path.join(ROOT, 'core'))
from trading_calendar import is_trading_day as _is_trading_day  # noqa: E402
PY = sys.executable
HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/a35d7f52-9ed2-47df-a929-f11aaf89025d'
# [2026-09-27 任务1.2] GT 引擎真实消费的 per-symbol 参数白名单（写入
# per_symbol[sym]['general_algorithm'] 块，由 monitor._general_cfg_for 消费）。
# 旧白名单 {'atr_min_pct'} 已退役：该参数在 GT 引擎下零消费（死参数，
# general_signal.check_general_b_trigger 入参直接忽略）。
HOT_PARAMS = {'buy_threshold', 'sell_threshold', 'signal_gap',
              'min_hist_diff', 'vol_ratio_b_max'}
LOCKED_PARAMS = {'trail'}             # 用户锁定/需两段式 → 仅建议

CFG = os.path.join(ROOT, 'data', 'monitor_config.json')
VERSION_FILE = os.path.join(ROOT, 'VERSION')
CHANGELOG = os.path.join(ROOT, 'CHANGELOG.md')


def push(text):
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}},
                         ensure_ascii=False).encode('utf-8')
    req = __import__('urllib.request').request.Request(
        HOOK, data=payload, headers={'Content-Type': 'application/json'})
    with __import__('urllib.request').request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', 'replace')


def git(*args):
    r = subprocess.run(['git', '-C', ROOT] + list(args), capture_output=True, text=True, encoding='utf-8')
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def bump_version():
    cur = open(VERSION_FILE, encoding='utf-8').read().strip()
    m = re.match(r'(\d+)\.(\d+)\.(\d+)', cur)
    new = f'{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}'
    open(VERSION_FILE, 'w', encoding='utf-8').write(new + '\n')
    return cur, new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    a = ap.parse_args()
    date = a.date

    # [2026-09-25] 非交易日不自迭代（休市日无信号，迭代只会污染状态）
    if not _is_trading_day(date):
        print(f'[daily_iterate] {date} 非交易日，跳过')
        return

    # 1) 寻优
    opt_out = os.path.join(ROOT, 'output', f'factor_opt_{date}.json')
    r = subprocess.run([PY, os.path.join(ROOT, 'scripts', 'factor_optimizer.py'), '--out', opt_out],
                       capture_output=True, text=True, encoding='utf-8')
    if r.returncode != 0:
        push(f'⚠️ tpoint 每日自迭代 {date}：factor_optimizer 运行失败，请检查日志')
        sys.exit(1)
    rep = json.load(open(opt_out, encoding='utf-8'))

    # 2) 护栏自动应用（[任务1.2] 一律经 effect_ledger 账本优先写入：
    #    先备份+记 hash_before 再写 config，AI 自动回滚依赖该账本）
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    import effect_ledger
    applied, pending = [], []
    for rec in rep.get('recommendations', []):
        sym, param, val = rec['sym'], rec['param'], rec['value']
        if param in LOCKED_PARAMS or param not in HOT_PARAMS:
            pending.append(rec)
            continue
        # [任务1.2] 随机对照一票否决（fail-closed）：未附随机对照 z 或 z<1.0 → 仅建议不热更
        if rec.get('random_z') is None or rec['random_z'] < 1.0:
            pending.append(rec)
            continue
        v = float(val) if isinstance(val, (int, float)) or str(val).replace('.', '', 1).lstrip('-').isdigit() else val
        e = effect_ledger.apply_config_change(
            sym, f'general_algorithm.{param}', v,
            source=f'daily_iterate:{date}', note=f"寻优增益 +{rec['delta_pp']}pp")
        applied.append({'sym': sym, 'param': param, 'old': e['old'], 'new': v, 'delta_pp': rec['delta_pp']})
    if applied:
        cfg = json.load(open(CFG, encoding='utf-8'))  # 供后续版本记录读取（账本已写入）

    # 3) git 版本记录（仅在有配置变更时 bump 小版本）
    ver_line = '无配置变更，不 bump 版本'
    if applied:
        old_v, new_v = bump_version()
        entry = (f'\n## v{new_v}（{date}）每日自迭代小版本\n'
                 + ''.join(f"- {x['sym']} {x['param']}: {x['old']} → {x['new']}（寻优增益 +{x['delta_pp']}pp，全历史回测验证）\n"
                           for x in applied)
                 + f'- 寻优报告：output/factor_opt_{date}.json\n')
        open(CHANGELOG, 'a', encoding='utf-8').write(entry)
        git('add', 'VERSION', 'CHANGELOG.md', 'data/monitor_config.json',
            f'output/factor_opt_{date}.json')
        rc, _, err = git('commit', '-m', f'chore(v{new_v}): 每日自迭代 {date} 参数优化（{len(applied)}项热更）')
        if rc == 0:
            git('tag', f'v{new_v}')
            ver_line = f'v{old_v} → v{new_v}（已 commit+tag）'
        else:
            ver_line = f'v{old_v} → v{new_v}（commit 失败: {err[:80]}）'

    # 4) 摘要推送（动态标题）
    lines = [f'🔁 tpoint 每日自迭代 {date}｜热更{len(applied)}项 待评审{len(pending)}项｜{ver_line}', '']
    if applied:
        lines.append('■ 已自动热更（次日开盘生效，可回滚=改回 monitor_config.json）：')
        for x in applied:
            lines.append(f"· {x['sym']} {x['param']}: {x['old']} → {x['new']}（+{x['delta_pp']}pp）")
    if pending:
        lines.append('■ 待评审（锁定参数/需两段式复核，未自动改）：')
        for p in pending:
            lines.append(f"· {p['sym']} {p.get('name', '')}：{p['param']}→{p['value']}（+{p['delta_pp']}pp）"
                         '→ 周五任务B评审')
    if not applied and not pending:
        lines.append('今日寻优无达标候选（阈值内无更优参数），维持当前配置。')
    lines.append('')
    lines.append(f"口径：F盘全历史+生产同源信号+simulate_day，成本万一+印花+滑点2bps；明细 output/factor_opt_{date}.json")
    push('\n'.join(lines))
    print('\n'.join(lines[:6]))


if __name__ == '__main__':
    main()
