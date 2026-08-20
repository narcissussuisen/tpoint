"""tests/test_p0_fixes.py — Phase 1 P0 修复红测（2026-08-18）

1. _bar_tradability：锁涨停/锁跌停/停牌/正常 四类判定（涨跌停/停牌成交可行性）。
2. trim_frontier：live 执行模型同根前视——trim_frontier=True 时最后一条"进行中"bar
   不参与判定（bar 标记少一根、且无 idx==n-1 的信号），False 时全量评估。
"""
import sys, os
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

import monitor as M
from miji_alpha import compute_miji_indicators


def _min_data(n=60, pc=10.0):
    rng = np.random.default_rng(1)
    c = 10 + np.cumsum(rng.normal(0, 0.005, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(c, o) + 0.003
    lo = np.minimum(c, o) - 0.003
    v = np.full(n, 1000.0)
    d = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    d['df'] = None  # detect_for 读 trade_time，None 时降级
    return d


def test_bar_tradability():
    # 主板 10%
    sym = '600000.SH'; pc = 10.0
    # lu = 11.00, ld = 9.00
    data = {'lo': np.array([10.98]), 'h': np.array([10.5]), 'v': np.array([1000.0])}
    assert M._bar_tradability(sym, data, 0, pc) == (True, False, False), "锁涨停应不可买"

    data = {'lo': np.array([9.5]), 'h': np.array([9.02]), 'v': np.array([1000.0])}
    assert M._bar_tradability(sym, data, 0, pc) == (False, True, False), "锁跌停应不可卖"

    data = {'lo': np.array([9.5]), 'h': np.array([9.5]), 'v': np.array([0.0])}
    assert M._bar_tradability(sym, data, 0, pc) == (False, False, True), "一字无量=停牌"

    data = {'lo': np.array([10.0]), 'h': np.array([10.2]), 'v': np.array([1000.0])}
    assert M._bar_tradability(sym, data, 0, pc) == (False, False, False), "正常 bar 不应被过滤"

    # 无 pc → 全 False（不误伤）
    data = {'lo': np.array([11.5]), 'h': np.array([11.5]), 'v': None}
    assert M._bar_tradability(sym, data, 0, 0.0) == (False, False, False)

    # 创业板 20%
    sym2 = '300308.SZ'  # lu = 12.00
    data = {'lo': np.array([11.99]), 'h': np.array([11.0]), 'v': np.array([500.0])}
    assert M._bar_tradability(sym2, data, 0, pc) == (True, False, False)


def test_trim_frontier_marker_count():
    data = _min_data()
    sym = '161129.SZ'
    M.STATE[sym] = {'PC': 10.0}
    n = data['n']
    today = M.datetime.now(M.CST).strftime('%Y%m%d')
    prefix = f'bar_{sym}_{today}_'

    orig_b, orig_s = M.check_b_trigger, M.check_s_trigger
    M.check_b_trigger = lambda *a, **k: (True, 'test')
    M.check_s_trigger = lambda *a, **k: (False, '')
    try:
        st_full = {}
        M.detect_for(sym, 't', data, st_full, trim_frontier=False)
        cnt_full = sum(1 for k in st_full if k.startswith(prefix))

        st_trim = {}
        M.detect_for(sym, 't', data, st_trim, trim_frontier=True)
        cnt_trim = sum(1 for k in st_trim if k.startswith(prefix))

        assert cnt_full == cnt_trim + 1, \
            f"trim_frontier 应少评估最后一根 bar: full={cnt_full} trim={cnt_trim}"
        assert not any(k.endswith(f'_{n - 1}') for k in st_trim), \
            "trim_frontier=True 时最后一根 bar 不应被标记"
        assert any(k.endswith(f'_{n - 2}') for k in st_trim), \
            "trim_frontier=True 时倒数第二根 bar 应仍被评估"
    finally:
        M.check_b_trigger, M.check_s_trigger = orig_b, orig_s


def test_limit_thr():
    from exit_manager import limit_thr
    assert limit_thr('600000.SH') == 0.10   # 主板
    assert limit_thr('300308.SZ') == 0.20   # 创业板
    assert limit_thr('688111.SH') == 0.20   # 科创板
    assert limit_thr('920000.BJ') == 0.30   # 北交所


def test_simulate_day_exit_lock():
    """出场侧成交可行性：锁跌停 bar 上 S 出场被跳过，持有至 EOD 强平。"""
    from exit_manager import simulate_day, make_config
    n = 10; pc = 10.0; sym = '600000.SH'   # 主板 10% → ld=9.0
    c = np.full(n, 10.0); c[5] = 9.0; c[9] = 9.2
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(c, o) + 0.01; h[5] = 9.0   # bar5 锁跌停(h<=ld+0.02)
    lo = np.minimum(c, o) - 0.01; lo[5] = 8.9
    atr = np.full(n, 0.05)
    prices = {'o': o, 'h': h, 'lo': lo, 'c': c, 'atr': atr, 'n': n,
              'pc': pc, 'sym': sym, 'date': '2026-01-01'}
    sigs = [{'type': 'B', 'idx': 2, 'price': 10.0, 'reason': '回踩下轨'},
            {'type': 'S', 'idx': 5, 'price': 9.0, 'reason': '反弹遇阻'}]
    cfg = make_config(use_stop=False, use_time=False, use_trailing=False, s_signal_exit=True)
    trips = simulate_day(sigs, prices, cfg)
    assert len(trips) == 1, f"应只有一笔 trip（锁跌停跳过 S 出场），实际 {len(trips)}"
    assert trips[0]['exit_reason'] == 'EOD', f"应在 EOD 强平，实际 {trips[0]['exit_reason']}"
    assert trips[0]['exit_idx'] == n - 1


if __name__ == '__main__':
    test_bar_tradability(); print('✅ test_bar_tradability')
    test_trim_frontier_marker_count(); print('✅ test_trim_frontier_marker_count')
    test_limit_thr(); print('✅ test_limit_thr')
    test_simulate_day_exit_lock(); print('✅ test_simulate_day_exit_lock')
    print('ALL P0 TESTS PASS')
