# -*- coding: utf-8 -*-
r"""gates.py — RSI 循环四道闸运行器（P2.1，2026-09-28）

四道闸（docs/rsi_loop_agenda.md；任一 FAIL ⇒ 候选退回 diagnose，负面结果留痕）：
  闸一 重放基准窗   : 固定 seed/池/窗/判据版本的池级基准（生产出场配置 + GT 默认信号，
                      exec_delay=1 口径），候选 vs 基线不退步——
                      net_wr ≥ 基线−1.0pp 且 mean_net 不恶化。
  闸二 验证器自检   : random_control_validator --self-test rc=0（验证器分辨率为零 =
                      一切结论失效）；且候选分子分母保留率 ≥90%（n_signals/n_trips vs 基线，
                      防「过滤到只剩 3 笔全胜」式虚胖）。
  闸三 测试基线     : tests/ 全量脚本对 step-0 快照新增失败 = 0。
  闸四 随机对照不退步: 基准窗上候选 z ≥1.0 且 p<0.05 且 n≥30，且 z 不低于当前生产配置。

机械设计原则（与 daily_agent 一致）：本脚本只做确定性机械；判断归 AI 会话（议程）。
重计算全部在本模块内跑（AI 会话只调命令 + 读 JSON），长跑带 longtask 三件套。

用法：
  python scripts/ai_loop/gates.py bench-window --init        # 初始化/刷新基准窗定义
  python scripts/ai_loop/gates.py bench --tag baseline       # 跑基准（落 data/rsi/bench_<tag>.json）
  python scripts/ai_loop/gates.py selftest                   # 闸二：验证器自检
  python scripts/ai_loop/gates.py tests --snapshot           # 闸三：快照当前测试基线
  python scripts/ai_loop/gates.py tests                      # 闸三：对快照比对（新增失败=0）
  python scripts/ai_loop/gates.py compare <cand.json> <base.json>   # 闸一+二+四汇总判定
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

RSI_DIR = os.path.join(ROOT, 'data', 'rsi')
PY = os.path.join(ROOT, 'venv', 'Scripts', 'python.exe')

# 基准窗定义（闸一/闸四共用；初始化后冻结，改动=人审）
WINDOW_FP = os.path.join(RSI_DIR, 'benchmark_windows.json')

# 闸三测试清单（新增测试文件自动纳入：tests/test_*.py 且可独立执行）
TESTS_DIR = os.path.join(ROOT, 'tests')

# 容差（docs/rsi_loop_agenda.md 闸一定义）
TOL_NET_WR_PP = 1.0        # net_wr 相对基线最大允许降幅
MIN_KEEP_RATE = 0.90       # 分子分母保留率
GATE_Z_MIN = 1.0
GATE_P_MAX = 0.05
GATE_N_MIN = 30


# --------------------------------------------------------------------------- #
# 基准窗
# --------------------------------------------------------------------------- #
def init_window(force=False):
    if os.path.exists(WINDOW_FP) and not force:
        with open(WINDOW_FP, encoding='utf-8') as f:
            return json.load(f)
    pool_fp = os.path.join(ROOT, 'data', 'tune_pool_40.json')
    with open(pool_fp, encoding='utf-8') as f:
        pool = json.load(f)
    syms = [e['symbol'] for e in (pool.get('pool') if isinstance(pool, dict) else pool)]
    win = {
        'frozen_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbols': syms, 'n_symbols': len(syms),
        'days': 60, 'm': 50, 'seed': 42, 'exec_delay': 1,
        'exit_cfg': 'production semantic (trail 0.4/0.6, no stop/time, s_signal_exit)',
        'judge': {'z_min': GATE_Z_MIN, 'p_max': GATE_P_MAX, 'n_min': GATE_N_MIN,
                  'tol_net_wr_pp': TOL_NET_WR_PP, 'min_keep_rate': MIN_KEEP_RATE},
        'note': 'RSI 基准窗（P2.1 部署）。改动本文件 = 人审（基准即标尺）。',
    }
    os.makedirs(RSI_DIR, exist_ok=True)
    with open(WINDOW_FP, 'w', encoding='utf-8') as f:
        json.dump(win, f, ensure_ascii=False, indent=2)
    return win


def load_window():
    if not os.path.exists(WINDOW_FP):
        raise FileNotFoundError('基准窗未初始化：先跑 gates.py bench-window --init')
    with open(WINDOW_FP, encoding='utf-8') as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 闸一/闸四：池级基准跑（复用 exit_anchor_grid 的 A0 臂语义，单臂 m 套伪样本）
# --------------------------------------------------------------------------- #
def run_bench(tag):
    """跑池级基准：全部标的 × 基准窗 → pooled 统计 + 随机对照 z。落 data/rsi/bench_<tag>.json。"""
    win = load_window()
    sys.path.insert(0, os.path.join(ROOT, 'scripts', 'research'))
    import exit_anchor_grid as grid_mod
    # 单臂跑：临时收窄 ARMS 到基线 A0（复用 eval_symbol 的 paired 伪样本与统计）
    orig_arms = dict(grid_mod.ARMS)
    try:
        grid_mod.ARMS = {k: v for k, v in orig_arms.items() if k == grid_mod.BASELINE}
        results = {}
        pooled_real, pooled_rand = [], [[] for _ in range(win['m'])]
        for sym in win['symbols']:
            r = grid_mod.eval_symbol(sym, win['days'], win['m'], win['seed'],
                                     verbose=False, exec_delay=win['exec_delay'])
            if 'error' in r:
                results[sym] = r['error']
                continue
            v = r['arms'][grid_mod.BASELINE]
            pooled_real.extend(v['real_rets'])
            for b in range(win['m']):
                pooled_rand[b].extend(v['rand_rets'][b])
        st = grid_mod.trip_stats(pooled_real)
        rw = [grid_mod.trip_stats(x)['net_wr'] if x else None for x in pooled_rand]
        rm = [grid_mod.trip_stats(x)['mean_net'] if x else None for x in pooled_rand]
        p_wr, z_wr = grid_mod._pz(st['net_wr'], [x for x in rw if x is not None])
        p_mn, z_mn = grid_mod._pz(st['mean_net'], [x for x in rm if x is not None])
        doc = {
            'tag': tag, 'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'window': win, 'n_symbols_ok': len(win['symbols']) - len(results),
            'errors': results,
            'pooled': st, 'random_control': {'p_netwr': p_wr, 'z_netwr': z_wr,
                                             'p_meannet': p_mn, 'z_meannet': z_mn},
        }
    finally:
        grid_mod.ARMS = orig_arms
    os.makedirs(RSI_DIR, exist_ok=True)
    fp = os.path.join(RSI_DIR, f'bench_{tag}.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f'bench[{tag}] n_trips={st["n_trips"]} net_wr={st["net_wr"] * 100:.2f}% '
          f'mean_net={st["mean_net"]:+.3f}pp z_wr={z_wr} -> {fp}')
    return fp


# --------------------------------------------------------------------------- #
# 闸二：验证器自检
# --------------------------------------------------------------------------- #
def run_selftest():
    """random_control_validator --exec-delay 1 --self-test rc=0 即过（随机信号 != PASS）。"""
    cmd = [PY, os.path.join(ROOT, 'scripts', 'random_control_validator.py'),
           '--sym', '300010.SZ', '--days', '20', '--m', '60', '--m-rt', '30',
           '--exec-delay', '1', '--self-test']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    out = (r.stdout or '') + (r.stderr or '')
    ok = (r.returncode == 0 and 'verdict = FAIL' in out)
    print(f'selftest rc={r.returncode} ok={ok}')
    return ok


# --------------------------------------------------------------------------- #
# 闸三：测试基线
# --------------------------------------------------------------------------- #
def _test_scripts():
    return sorted(f for f in os.listdir(TESTS_DIR)
                  if f.startswith('test_') and f.endswith('.py'))


def run_tests(snapshot=False):
    snap_fp = os.path.join(RSI_DIR, 'test_baseline.json')
    results = {}
    for tf in _test_scripts():
        r = subprocess.run([PY, os.path.join(TESTS_DIR, tf)],
                           capture_output=True, text=True, timeout=1800)
        results[tf] = {'rc': r.returncode}
    if snapshot:
        os.makedirs(RSI_DIR, exist_ok=True)
        with open(snap_fp, 'w', encoding='utf-8') as f:
            json.dump({'taken_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'results': results}, f, ensure_ascii=False, indent=2)
        print(f'snapshot: {sum(1 for v in results.values() if v["rc"] == 0)}/{len(results)} rc=0 -> {snap_fp}')
        return True, results
    if not os.path.exists(snap_fp):
        raise FileNotFoundError('无测试基线快照：先跑 gates.py tests --snapshot')
    with open(snap_fp, encoding='utf-8') as f:
        snap = json.load(f)['results']
    new_fail = [tf for tf, v in results.items()
                if v['rc'] != 0 and snap.get(tf, {}).get('rc', 1) == 0]
    fixed = [tf for tf, v in results.items() if v['rc'] == 0 and snap.get(tf, {}).get('rc', 0) != 0]
    print(f'tests: {"OK" if not new_fail else "NEW_FAIL"} '
          f'(新增失败={new_fail or "无"}; 修复={fixed or "无"})')
    return (not new_fail), results


# --------------------------------------------------------------------------- #
# 汇总判定（闸一/二/四）
# --------------------------------------------------------------------------- #
def compare(cand_fp, base_fp):
    with open(cand_fp, encoding='utf-8') as f:
        cand = json.load(f)
    with open(base_fp, encoding='utf-8') as f:
        base = json.load(f)
    reasons, oks = [], {}

    # 闸一：重放基准窗不退步
    cs, bs = cand['pooled'], base['pooled']
    if cs['net_wr'] is None or bs['net_wr'] is None:
        reasons.append('闸一：net_wr 缺失'); oks['gate1'] = False
    else:
        d_wr = (cs['net_wr'] - bs['net_wr']) * 100.0
        mn_ok = (cs['mean_net'] is not None and bs['mean_net'] is not None
                 and cs['mean_net'] >= bs['mean_net'])
        oks['gate1'] = (d_wr >= -TOL_NET_WR_PP) and mn_ok
        if not oks['gate1']:
            reasons.append(f'闸一 FAIL：Δnet_wr={d_wr:+.2f}pp（容差 −{TOL_NET_WR_PP}pp）'
                           f' mean_net {bs["mean_net"]:+.3f}→{cs["mean_net"]:+.3f}pp')

    # 闸二（分子分母保留率）：n_trips / n_signals 类指标
    keep = None
    for key in ('n_trips',):
        if cs.get(key) and bs.get(key):
            keep = cs[key] / bs[key]
    if keep is None:
        oks['gate2'] = False
        reasons.append('闸二：样本量缺失，无法算保留率')
    else:
        oks['gate2'] = keep >= MIN_KEEP_RATE
        if not oks['gate2']:
            reasons.append(f'闸二 FAIL：保留率 {keep:.2%} < {MIN_KEEP_RATE:.0%}')

    # 闸四：随机对照不退步
    cr, br = cand['random_control'], base['random_control']
    z_ok = (cr.get('z_netwr') is not None and cr['z_netwr'] >= GATE_Z_MIN
            and (cr.get('p_netwr') is None or cr['p_netwr'] < GATE_P_MAX))
    z_not_lower = (cr.get('z_netwr') is not None and br.get('z_netwr') is not None
                   and cr['z_netwr'] >= br['z_netwr'])
    n_ok = cs.get('n_trips', 0) >= GATE_N_MIN
    oks['gate4'] = z_ok and z_not_lower and n_ok
    if not oks['gate4']:
        reasons.append(f'闸四 FAIL：z_netwr {br.get("z_netwr")}→{cr.get("z_netwr")} '
                       f'(需≥{GATE_Z_MIN} 且不降) n_trips={cs.get("n_trips")}')

    verdict = 'PASS' if all(oks.values()) else 'FAIL'
    print(f'compare: {verdict} ' + ' '.join(f'{k}={"OK" if v else "FAIL"}' for k, v in oks.items()))
    for r in reasons:
        print('  ' + r)
    return verdict == 'PASS', reasons


def main():
    ap = argparse.ArgumentParser(description='RSI 四道闸运行器')
    ap.add_argument('cmd', choices=('bench-window', 'bench', 'selftest', 'tests', 'compare'))
    ap.add_argument('--init', action='store_true', help='bench-window: 初始化/刷新基准窗')
    ap.add_argument('--force', action='store_true', help='bench-window: 覆盖既有定义')
    ap.add_argument('--tag', default='', help='bench: 产物标签')
    ap.add_argument('--snapshot', action='store_true', help='tests: 落快照而非比对')
    ap.add_argument('cand', nargs='?', help='compare: 候选 bench json')
    ap.add_argument('base', nargs='?', help='compare: 基线 bench json')
    a = ap.parse_args()

    if a.cmd == 'bench-window':
        win = init_window(force=a.force or a.init)
        print(json.dumps({k: win[k] for k in ('n_symbols', 'days', 'm', 'seed', 'exec_delay')},
                         ensure_ascii=False))
    elif a.cmd == 'bench':
        if not a.tag:
            ap.error('bench 需要 --tag')
        run_bench(a.tag)
    elif a.cmd == 'selftest':
        sys.exit(0 if run_selftest() else 2)
    elif a.cmd == 'tests':
        ok, _ = run_tests(snapshot=a.snapshot)
        sys.exit(0 if ok else 2)
    elif a.cmd == 'compare':
        if not (a.cand and a.base):
            ap.error('compare 需要 <cand.json> <base.json>')
        ok, _ = compare(a.cand, a.base)
        sys.exit(0 if ok else 2)


if __name__ == '__main__':
    main()
