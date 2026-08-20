"""tests/test_evolution.py — 因子演化引擎红测（2026-08-18 Phase 3）

锁三件事：
  1. _op_ok 比较算子（纯函数）。
  2. apply_gate 只过滤指定侧信号，不误杀。
  3. evaluate_gate 对「杀光 B」的极端门控 → DEMOTE（n=0 < 10，机制确定性）；
     以及「单标的深跌拟合」反例 → DEMOTE（池级 OOS 不晋升，防逐标的过拟合）。
"""
import sys, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from evolution import _op_ok, apply_gate, evaluate_gate


def _sig_days():
    # 合成一天：2 个 B（idx 3, 8）+ 1 个 S（idx 5），data 含 20 根 bar
    n = 20
    rng = np.random.default_rng(0)
    c = 10 + np.cumsum(rng.normal(0, 0.01, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(c, o) + 0.005
    lo = np.minimum(c, o) - 0.005
    v = np.full(n, 1000.0)
    data = {'o': o, 'h': h, 'lo': lo, 'c': c, 'v': v, 'n': n}
    sigs = [{'type': 'B', 'idx': 3, 'price': 10.0},
            {'type': 'S', 'idx': 5, 'price': 10.1},
            {'type': 'B', 'idx': 8, 'price': 10.2}]
    return [('2026-01-01', data, sigs)]


def test_op_ok():
    assert _op_ok(1.0, '<', 2.0) is True
    assert _op_ok(2.0, '<', 2.0) is False
    assert _op_ok(2.0, '<=', 2.0) is True
    assert _op_ok(3.0, '>', 2.0) is True
    assert _op_ok(1.0, '>=', 2.0) is False


def test_apply_gate_only_filters_side():
    sd = _sig_days()
    # rsi < 999 → 全部 B 都满足（不杀 B）；S 不受影响
    gate = {'factor': 'rsi', 'side': 'B', 'op': '<', 'thr': 999.0}
    out = apply_gate(sd, gate)
    kept = out[0][2]
    assert sum(1 for s in kept if s['type'] == 'B') == 2, "B 应全保留"
    assert sum(1 for s in kept if s['type'] == 'S') == 1, "S 不受 B 侧门控影响"


def test_kill_all_b_demotes():
    # 杀光 B（rsi < -999 永假）→ n=0 → DEMOTE（机制确定性）
    gate = {'name': 'kill_all_b', 'factor': 'rsi', 'side': 'B', 'op': '<', 'thr': -999.0}
    r = evaluate_gate(gate, syms=['513310.SH'])
    assert r['verdict'] == 'DEMOTE', f"杀光 B 应 DEMOTE，实际 {r['verdict']} n={r['OOS']['n_gate']}"
    assert r['OOS']['n_gate'] < 10


def test_overfit_deep_dip_demotes():
    # 单标的深跌拟合反例：池级 OOS 必须 DEMOTE（防逐标的过拟合）
    from evolution import OVERFIT_CANDIDATES
    r = evaluate_gate(OVERFIT_CANDIDATES[0])
    assert r['verdict'] == 'DEMOTE', \
        f"单标的深跌拟合应在池级被淘汰，实际 {r['verdict']} Δret={r['d_ret_oos_pp']}"


if __name__ == '__main__':
    print("=" * 64)
    print("tpoint 因子演化引擎 — 红测")
    print("=" * 64)
    fails = []
    for fn in [test_op_ok, test_apply_gate_only_filters_side, test_kill_all_b_demotes,
               test_overfit_deep_dip_demotes]:
        try:
            fn()
            print(f"[OK] {fn.__name__}")
        except Exception as e:
            print(f"[FAIL] {fn.__name__}: {e}")
            fails.append(fn.__name__)
    print("=" * 64)
    if fails:
        print(f"❌ 失败: {fails}")
        sys.exit(1)
    print("✅ ALL PASS — 演化引擎机制正确、单标的拟合被淘汰")
    sys.exit(0)
