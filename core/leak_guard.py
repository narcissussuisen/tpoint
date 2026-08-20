# -*- coding: utf-8 -*-
"""
leak_guard.py — tpoint 前视偏差(lookahead bias)防护栅栏  [2026-08-17 引入, 源自普林斯顿×蚂蚁 AQuA 论文研判]

## 为什么需要它
分钟级做T信号对前视偏差极度敏感。AQuA 论文(及 QuantML 复现)指出: 哪怕某个特征在
归一化/聚合时无意用了盘后或未来数据(如"成交量参与率"用当日累计量做分母, 盘中上午的
每根 Bar 都用到了下午收盘后的成交量), 回测 IC 会非常漂亮, 但实盘瞬间归零。用"语言模型
审代码"只是软约束 —— 两者共享同一套认知盲区; 只有基于"时序算子仅读截至当前 Bar /
截面算子仅读当前截面"的物理约束 + 可证伪的自动化检验, 才能消灭泄漏表达。

本模块提供两道防线, 与 tpoint 现有 self-check 体系统一:
  1. 未来扰动测试(perturbation_test): 往"未来"bar 灌随机噪声, 重算历史时点的全部
     特征, 若历史特征值在容差内不变 → 证明无泄漏。这是可复算、可回归的硬性检验。
  2. 结构性纪律(time_locked 契约): 任何新特征算子必须只依赖 j<=i 的数据; 通过
     assert_no_lookahead 包装即默认接受此契约, 违反则测试直接红(防止日后回归)。

## 用法
  from leak_guard import check_indicators_no_lookahead, perturbation_test, assert_no_lookahead
  check_indicators_no_lookahead()            # 证伪 v9/miji 特征栈无前视
  perturbation_test(my_feat, ohlcv)          # 对任意特征函数做泄漏检验
  assert_no_lookahead(my_feat, ohlcv)        # 失败抛 AssertionError(适合 pytest/selftest)

参考清单(新增特征时自检):
  ✅ 滚动窗口只用 v[i-lookback:i]            ❌ 用当日/全样本均值、标准差、VWAP 做分母
  ✅ 累计量/价只 cumsum 到 i                 ❌ 用 v[i+1:] 或 bar 之后的收盘/最高最低
  ✅ 标签用 i 之后才可知的量(如 i+1 收益)     ❌ 标签或特征用同日未到时刻的聚合值
"""
import numpy as np


# ========== 核心: 未来扰动测试 ==========

def perturbation_test(feat_fn, ohlcv, ks_frac=(0.3, 0.5, 0.7, 0.85),
                      noise_scale=0.02, seeds=(0, 1, 2), tol=1e-3):
    """未来扰动测试 — 证伪特征函数是否存在前视偏差。

    feat_fn(o, h, lo, c, v) -> Mapping[str, np.ndarray]
        输入为完整序列(长度 n), 输出每个特征的逐 bar 数组(长度 n)。
    ohlcv: (o, h, lo, c, v) 元组, 各为长度 n 的数组。

    原理(AQuA 复现的"未来扰动测试"): 在若干个切分点 k 处, 仅对 k 之后的"未来"bar
    注入高斯噪声, 重算全量特征, 比对 k 及之前的历史特征值。若存在泄漏, 未来数据会
    通过该特征的"盘后/全样本"聚合回流到历史, 历史值将发生 > tol 的偏移。

    返回 dict:
      ok          : 所有 (k, feature, seed) 组合的 max_abs_diff <= tol
      tol         : 判定容差
      n_checks    : 检查组合数
      worst       : {'k','k_frac','seed','feature','max_abs_diff'} 最差组合
      per_feature : {feature: max_abs_diff 全组合最大值}
      detail      : 各组合明细(list)
    """
    o, h, lo, c, v = ohlcv
    o = np.asarray(o, float); h = np.asarray(h, float)
    lo = np.asarray(lo, float); c = np.asarray(c, float); v = np.asarray(v, float)
    n = len(c)
    base = feat_fn(o, h, lo, c, v)
    feat_names = list(base.keys())

    detail = []
    per_feat_max = {fn: 0.0 for fn in feat_names}
    global_worst = None
    for kf in ks_frac:
        k = int(n * kf)
        if k < 2 or k >= n - 1:
            continue
        for sd in seeds:
            rng = np.random.default_rng(sd)
            scale = noise_scale * (float(np.abs(c[k + 1:]).mean()) + 1e-9)

            def _corrupt(arr):
                arr = arr.astype(float).copy()
                arr[k + 1:] += rng.normal(0, scale, arr[k + 1:].shape[0])
                return arr

            oc, hc, loc, cc, vc = (_corrupt(o), _corrupt(h), _corrupt(lo),
                                   _corrupt(c), _corrupt(v))
            corr = feat_fn(oc, hc, loc, cc, vc)
            combo_worst = 0.0
            combo_feat = None
            for fn in feat_names:
                b = np.asarray(base[fn], float)
                q = np.asarray(corr[fn], float)
                diff = float(np.max(np.abs(b[:k + 1] - q[:k + 1])))
                per_feat_max[fn] = max(per_feat_max[fn], diff)
                if diff > combo_worst:
                    combo_worst, combo_feat = diff, fn
            detail.append({'k_frac': round(kf, 2), 'k': k, 'seed': sd,
                           'max_abs_diff': round(combo_worst, 6),
                           'worst_feature': combo_feat})
            if global_worst is None or combo_worst > global_worst['max_abs_diff']:
                global_worst = {'k': k, 'k_frac': kf, 'seed': sd,
                                'feature': combo_feat, 'max_abs_diff': combo_worst}

    n_checks = len(detail)
    ok = (global_worst is None) or (global_worst['max_abs_diff'] <= tol)
    return {'ok': ok, 'tol': tol, 'n_checks': n_checks,
            'worst': global_worst,
            'per_feature': {k: round(v, 6) for k, v in per_feat_max.items()},
            'detail': detail}


def assert_no_lookahead(feat_fn, ohlcv, **kw):
    """断言特征函数无前视偏差; 失败抛 AssertionError(带可读诊断)。

    适合接入 pytest / selftest: 一旦有人新增了含未来数据的特征, 这里会立刻红。
    """
    rep = perturbation_test(feat_fn, ohlcv, **kw)
    if not rep['ok']:
        w = rep['worst']
        raise AssertionError(
            f"LOOKAHEAD DETECTED: 特征 '{w['feature']}' 在 k={w['k']}({w['k_frac']}) "
            f"seed={w['seed']} 下历史特征值偏移 {w['max_abs_diff']:.6f} > tol={rep['tol']}。"
            f"该特征疑似使用了未来数据(盘后/全样本聚合), 违反 time_locked 契约。")
    return rep


# ========== 默认特征栈: v9/miji 无前视证伪 ==========

def _demo_ohlcv(n=300, seed=7, pc=10.0):
    """确定性合成行情(日级随机游走), 用于无偏跑未来扰动测试。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = pc + 0.002 * t + np.sin(2 * np.pi * t / 30) * 0.15
    c = base + rng.normal(0, 0.03, n)
    o = np.empty(n); o[0] = c[0]; o[1:] = c[:-1]
    h = np.maximum(c, o) + rng.uniform(0.005, 0.02, n)
    lo = np.minimum(c, o) - rng.uniform(0.005, 0.02, n)
    v = rng.uniform(800, 1400, n) + 1500 * np.abs(np.diff(np.concatenate([[c[0]], c])))
    return o, h, lo, c, v


def _indicators_feat(o, h, lo, c, v):
    """包装 core.indicators.compute_indicators 为 feat_fn 口径(逐 bar 数组)。"""
    from indicators import compute_indicators
    pc = float(c[0])
    d = compute_indicators(o, h, lo, c, v, pc, has_vol=True)
    return {k: d[k] for k in
            ['c', 'vwap', 'atr', 'trend', 'vol_ratio', 'rsi', 'temp',
             'ema_f', 'ema_s', 'adx']}


def _miji_feat(o, h, lo, c, v):
    """包装 core.miji_alpha.compute_miji_indicators 为 feat_fn 口径(逐 bar 数组)。"""
    from miji_alpha import compute_miji_indicators
    pc = float(c[0])
    d = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    return {k: d[k] for k in
            ['c', 'vwap', 'atr', 'dif', 'dea', 'hist', 'trend', 'trend_strong',
             'rsi', 'vol_ratio', 'temp', 'macd60_dif', 'macd15_dif']}


def check_indicators_no_lookahead(**kw):
    """证伪 v9 默认特征栈无前视偏差。供 selftest / 测试套件调用。

    返回 perturbation_test 的报告 dict(含 ok 字段)。当前特征全部为因果算子
    (cumsum 截至 i / 滚动窗口 v[i-lookback:i] / 仅依赖 j<=i), 故预期 ok=True。
    """
    o, h, lo, c, v = _demo_ohlcv()
    return assert_no_lookahead(_indicators_feat, (o, h, lo, c, v), **kw)


def check_miji_no_lookahead(**kw):
    """证伪 miji(做T秘籍)特征栈无前视偏差。与 v9 共用同一套栅栏。

    两条信号引擎(inds/miji)统一受防, 任一侧日后新增含未来数据的特征都会被抓出。
    """
    o, h, lo, c, v = _demo_ohlcv()
    return assert_no_lookahead(_miji_feat, (o, h, lo, c, v), **kw)


# ========== 故意泄漏的参考特征(用于红测, 证明栅栏非摆设) ==========

def leaky_feature(o, h, lo, c, v):
    """❌ 反例: 用"全样本均值"做归一化分母 → 未来 bar 改变均值 → 前视泄漏。

    对应 AQuA 论文里的"成交量参与率"用当日累计量分母的坑。
    """
    mean = float(np.mean(c))
    return {'dev_from_fullmean': (c - mean) / mean if mean != 0 else c * 0.0}


if __name__ == '__main__':
    print("== tpoint 前视偏差防护栅栏自检 ==")
    for name, fn in (('v9 特征栈', check_indicators_no_lookahead),
                     ('miji 特征栈', check_miji_no_lookahead)):
        try:
            rep = fn()
            print(f"[{name}] ok={rep['ok']}  n_checks={rep['n_checks']}  "
                  f"worst_diff={rep['worst']['max_abs_diff'] if rep['worst'] else 0:.6f}")
            assert rep['ok']
            print(f"  ✅ {name} 无前视偏差")
        except AssertionError as e:
            print(f"  ❌ {e}")
            raise

    leak = perturbation_test(leaky_feature, _demo_ohlcv())
    print(f"[故意泄漏反例] ok={leak['ok']}  worst={leak['worst']}")
    assert not leak['ok'], "栅栏应当抓出泄漏反例, 否则测试本身无效"
    print("  ✅ 栅栏能正确抓出前视泄漏(红测通过)")
    print("\nALL PASS")
