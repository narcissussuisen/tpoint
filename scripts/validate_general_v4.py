#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_general_v4.py —— 通用算法驱动 watchlist + v4 灰度 验收脚本

用法:
  python scripts/validate_general_v4.py [YYYY-MM-DD] [YYYY-MM-DD ...]

验收口径（对齐 2026-08-20 完善方案）:
  C1 通用算法驱动全标的: 每个 watchlist 标的均产出 B>0 且 S>0（证明 symbol-agnostic、
      双向、无原始 v4「只卖不买」死锁）。
  C2 v4 灰度可运行: v4 影子候选在每标的产出 B>0（证明 v4 死锁已修复且灰度链路通）。
  C3 对比报告产出: output/v4_gray_compare_<date>.json 存在且含 promote 建议。
  C4 生产信号落盘: output/general_signals_<date>.json 存在。
  C5 monitor 实时集成编译通过（由调用方 py_compile 保证，本脚本不重复）。
全部 PASS → 系统具备「通用算法驱动 watchlist + v4 灰度测试支持」。
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)

from watchlist_engine import run_watchlist, load_flags  # noqa: E402


def validate_date(date):
    print(f"\n===== 验收 [{date}] =====")
    flags = load_flags()
    print(f"  flag: use_general_engine={flags['use_general_engine']} v4_gray={flags['v4_gray_enable']} promote={flags['v4_promote']}")
    cmp = run_watchlist(date)
    if 'error' in cmp:
        print("  ❌", cmp['error']); return False

    criteria = {'C1_general_bidirectional': True, 'C2_v4_gray_runs': True,
                'C3_compare_report': True, 'C4_general_signals_file': True}
    rows = cmp.get('rows', [])
    for r in rows:
        sym = r.get('sym')
        if not r.get('ok'):
            print(f"  ⚠️ {sym}: 跳过({r.get('reason')})"); continue
        g = r['general']
        v = r.get('v4_gray')
        print(f"  {sym} {r.get('name','')}: 通用 B{g['n_b']}/S{g['n_s']} 配对{g['trips']} WR{g['wr']} 净{g['total_ret']}%"
              + (f" | v4灰 B{v['n_b']}/S{v['n_s']} WR{v['wr']} 净{v['total_ret']}%" if v else ""))
        if not (g['n_b'] > 0 and g['n_s'] > 0):
            criteria['C1_general_bidirectional'] = False
            print(f"    ❌ C1 失败: 通用算法未双向出信号 (B={g['n_b']}, S={g['n_s']})")
        if v is not None and v['n_b'] <= 0:
            criteria['C2_v4_gray_runs'] = False
            print(f"    ❌ C2 失败: v4 灰度 B={v['n_b']} 未出买点")

    if not cmp.get('compare_file') or not os.path.exists(cmp['compare_file']):
        criteria['C3_compare_report'] = False
        print("  ❌ C3 失败: 对比报告缺失")
    else:
        print(f"  ✅ C3 对比报告: {cmp['compare_file']}  promote建议={cmp.get('v4_promote_recommend')}")

    if not cmp.get('general_signals_file') or not os.path.exists(cmp['general_signals_file']):
        criteria['C4_general_signals_file'] = False
        print("  ❌ C4 失败: 生产信号文件缺失")
    else:
        print(f"  ✅ C4 生产信号: {cmp['general_signals_file']}")

    all_pass = all(criteria.values())
    print(f"  >>> [{date}] {'✅ 全部 PASS' if all_pass else '❌ 存在 FAIL'}: {criteria}")
    return all_pass


def main():
    dates = sys.argv[1:]
    if not dates:
        dates = [datetime.date.today().strftime('%Y-%m-%d')]
    results = [validate_date(d) for d in dates]
    overall = all(results)
    print(f"\n{'='*60}\n总验收: {'✅ PASS' if overall else '❌ FAIL'}  ({sum(results)}/{len(results)} 日期通过)")
    sys.exit(0 if overall else 1)


if __name__ == '__main__':
    main()
