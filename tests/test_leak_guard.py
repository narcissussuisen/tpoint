# -*- coding: utf-8 -*-
"""test_leak_guard.py — 前视偏差(lookahead)防护栅栏回归测试  [2026-08-17 新增]

## 为什么有这个测试
普林斯顿×蚂蚁 AQuA 论文(及 QuantML 复现)指出: 分钟级策略最隐蔽的失败来自前视偏差
——某个特征在归一化/聚合时无意用了盘后或未来数据, 回测 IC 漂亮但实盘归零。tpoint 用
core/leak_guard.py 的"未来扰动测试"把这条纪律变成可复算、可回归的硬检验。

本测试把两道防线和"栅栏本身有效"三件事锁死:
  1. v9 特征栈(compute_indicators)无前视 —— 历史特征值在灌入未来噪声后不变;
  2. miji 特征栈(compute_miji_indicators)无前视 —— 含多周期 MACD 方向过滤;
  3. 红测: 故意构造一个用"全样本均值"做分母的泄漏特征, 断言栅栏能抓出它。
     若栅栏对明显泄漏也放行, 说明检验本身失效(比漏报更危险) → 必须红。

任何一侧未来新增含未来数据的特征, 这里都会红; 修掉泄漏后才会绿。

## 跑法
  venv/Scripts/python.exe tests/test_leak_guard.py
全部通过 exit 0; 任一失败 exit 1 并打印明细。不触碰真实配置 / 不发飞书。
"""
import os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

from leak_guard import (check_indicators_no_lookahead, check_miji_no_lookahead,
                        perturbation_test, leaky_feature, _demo_ohlcv)


def _run():
    fails = []

    # 1) v9 特征栈无前视
    try:
        r = check_indicators_no_lookahead()
        assert r['ok'], f"v9 特征栈检出前视: {r['worst']}"
        print(f"[1] v9 特征栈 无前视  ✅  (n_checks={r['n_checks']}, "
              f"worst_diff={r['worst']['max_abs_diff']:.2e})")
    except Exception as e:
        print(f"[1] v9 特征栈 无前视  ❌  {e}")
        fails.append("v9")

    # 2) miji 特征栈无前视(含多周期 MACD)
    try:
        r = check_miji_no_lookahead()
        assert r['ok'], f"miji 特征栈检出前视: {r['worst']}"
        print(f"[2] miji 特征栈 无前视  ✅  (n_checks={r['n_checks']}, "
              f"worst_diff={r['worst']['max_abs_diff']:.2e})")
    except Exception as e:
        print(f"[2] miji 特征栈 无前视  ❌  {e}")
        fails.append("miji")

    # 3) 红测: 栅栏必须能抓出明显前视泄漏
    leak = perturbation_test(leaky_feature, _demo_ohlcv())
    if (not leak['ok']) and leak['worst']['feature'] == 'dev_from_fullmean':
        print(f"[3] 红测: 栅栏抓出故意泄漏  ✅  (feature={leak['worst']['feature']}, "
              f"diff={leak['worst']['max_abs_diff']:.2e})")
    else:
        print(f"[3] 红测: 栅栏未能抓出故意泄漏  ❌  {leak}")
        fails.append("red-test")

    # 4) [2026-08-18 Phase 2] 因子注册表无前视：新因子进 registry 自动受守护
    try:
        from factor_registry import factor_feat
        from leak_guard import assert_no_lookahead
        r = assert_no_lookahead(factor_feat(), _demo_ohlcv())
        assert r['ok'], f"因子注册表检出前视: {r['worst']}"
        print(f"[4] 因子注册表 无前视  ✅  (n_checks={r['n_checks']}, "
              f"features={len(r.get('features', [])) or 'n/a'})")
    except Exception as e:
        print(f"[4] 因子注册表 无前视  ❌  {e}")
        fails.append("factor-registry")

    return fails


if __name__ == '__main__':
    print("=" * 64)
    print("tpoint 前视偏差防护栅栏 — 回归测试")
    print("=" * 64)
    fails = _run()
    print("=" * 64)
    if fails:
        print(f"❌ 失败: {fails}")
        sys.exit(1)
    print("✅ ALL PASS — v9/miji 特征栈均无前视, 栅栏自身有效")
    sys.exit(0)
