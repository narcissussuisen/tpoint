"""core/watchlist_engine.py — tpoint watchlist 模块（通用算法驱动 + v4 灰度）

=================================================================================
职责（2026-08-20 完善方案落地）：
---------------------------------------------------------------------------------
本模块是 tpoint 的 **watchlist 模块**，统一完成：
  1. 从 data/watchlist.json 读取全部监控标的（单一真相源，symbol-agnostic）。
  2. 对**每一个标的**用**通用算法**（core/general_signal.GeneralConfig）生成信号，
     即「通用算法驱动的 watchlist」——一套配置驱动全标的，无逐标的特例。
  3. 按配置将 **v4 版本纳入灰度测试**：当 v4_gray_enable=true，对每标的并行跑
     composite_scorer_v4full.detect_signals_v4full（影子候选），落盘到
     data/v4_shadow/v4_shadow_<date>_<sym>.jsonl，并生成 v4 与生产(通用算法)的
     对比报告 output/v4_gray_compare_<date>.json，供 A/B 决策与 promote 门控使用。
  4. 生产信号落盘 output/general_signals_<date>.json（通用算法驱动的最终信号）。

设计要点：
  - 与 monitor.detect_for 共用同一套数据构建（daily_signal_review.build_data）与
    出场模拟（exit_manager.simulate_day），保证「实时 / 回测 / 灰度」三口径一致。
  - 全部 flag 来自 data/monitor_config.json 的 _global（use_general_engine /
    v4_gray_enable / v4_promote / bidirectional_enable / vol_ratio_b_max），
    热重载、默认关，符合 plan 纪律。
  - 本模块为**批量/影子**引擎，不改动 monitor 实时决策路径；monitor 经 flag 调用
    general_signal 的 check_*_trigger 实现实时同源（见 monitor.detect_for 改造）。
=================================================================================
"""
import os
import sys
import json
import datetime
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(BASE_DIR, 'core')
SCRIPTS_DIR = os.path.join(BASE_DIR, 'scripts')
for p in (CORE_DIR, SCRIPTS_DIR, BASE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from daily_signal_review import fetch_1m, get_pc, build_data, MootdxDataSource  # noqa: E402
from exit_manager import make_config, cost_for_symbol, simulate_day, aggregate_metrics  # noqa: E402
from general_signal import detect_signals_general, GeneralConfig, GENERAL_DEFAULT  # noqa: E402
from composite_scorer_v4full import detect_signals_v4full, V4FULL_DEFAULT  # noqa: E402

WATCHLIST_FILE = os.path.join(BASE_DIR, 'data', 'watchlist.json')
MONITOR_CONFIG_FILE = os.path.join(BASE_DIR, 'data', 'monitor_config.json')
OUT_DIR = os.path.join(BASE_DIR, 'output')
SHADOW_DIR = os.path.join(BASE_DIR, 'data', 'v4_shadow')
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(SHADOW_DIR, exist_ok=True)

EXIT_CFG = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True)


# ========== 配置读取 ==========

def load_flags() -> Dict[str, Any]:
    """读取 monitor_config.json _global 的引擎 flag（全部默认关，fail-open）。"""
    defaults = {
        'use_general_engine': True,
        'v4_gray_enable': False,
        'v4_promote': False,
        'bidirectional_enable': False,
        'vol_ratio_b_max': None,
    }
    try:
        if os.path.exists(MONITOR_CONFIG_FILE):
            with open(MONITOR_CONFIG_FILE, encoding='utf-8') as f:
                raw = json.load(f)
            g = (raw.get('_global') or {}) if isinstance(raw, dict) else {}
            for k in defaults:
                if k in g:
                    defaults[k] = g[k]
    except Exception:
        pass
    return defaults


def build_general_config(flags: Dict[str, Any]) -> GeneralConfig:
    """由 flag 构造通用算法配置（symbol-agnostic；可选对齐生产量能门控）。"""
    cfg = GeneralConfig()
    vrb = flags.get('vol_ratio_b_max')
    if vrb is not None:
        cfg.vol_ratio_b_max = float(vrb)
    return cfg


# ========== 单标的处理 ==========

def process_symbol(sym: str, name: str, date: str, ds,
                   general_cfg: GeneralConfig, v4_cfg, v4_gray: bool) -> Dict[str, Any]:
    """对单标的跑通用算法（生产）+ v4 灰度（影子），返回结构化结果。"""
    res: Dict[str, Any] = {'sym': sym, 'name': name, 'date': date,
                           'ok': False, 'reason': ''}
    try:
        df = fetch_1m(ds, sym, date)
        if df is None or len(df) < 5:
            res['reason'] = '无1m数据'; return res
        pc = get_pc(ds, sym, date)
        if pc is None or pc <= 0:
            res['reason'] = '无昨收'; return res
        data = build_data(df, pc)
        n = data['n']

        # —— 生产：通用算法 ——
        g_sigs = detect_signals_general(data, pc, general_cfg)
        prices = {'o': data['o'], 'h': data['h'], 'lo': data['lo'], 'c': data['c'],
                  'atr': data['atr'], 'trend': data['trend'], 'n': n,
                  'pc': pc, 'sym': sym, 'date': date}
        g_trips = simulate_day(g_sigs, prices, EXIT_CFG, cost=cost_for_symbol(sym))
        g_metrics = aggregate_metrics(g_trips) if g_trips else {}

        res['general'] = {
            'n_bars': n,
            'n_signals': len(g_sigs),
            'n_b': sum(1 for s in g_sigs if s['type'] == 'B'),
            'n_s': sum(1 for s in g_sigs if s['type'] == 'S'),
            'avg_score': round(float(np_mean([abs(s['score']) for s in g_sigs])), 4) if g_sigs else 0.0,
            'trips': len(g_trips),
            'wr': round(float(g_metrics.get('win_rate', 0.0)), 4),
            'total_ret': round(float(g_metrics.get('total_ret', 0.0)), 4),
            'signals': g_sigs,
        }

        # —— 灰度：v4 影子候选 ——
        if v4_gray:
            v_sigs = detect_signals_v4full(data, pc, v4_cfg)
            v_trips = simulate_day(v_sigs, prices, EXIT_CFG, cost=cost_for_symbol(sym))
            v_metrics = aggregate_metrics(v_trips) if v_trips else {}
            res['v4_gray'] = {
                'n_signals': len(v_sigs),
                'n_b': sum(1 for s in v_sigs if s['type'] == 'B'),
                'n_s': sum(1 for s in v_sigs if s['type'] == 'S'),
                'avg_score': round(float(np_mean([abs(s['score']) for s in v_sigs])), 4) if v_sigs else 0.0,
                'trips': len(v_trips),
                'wr': round(float(v_metrics.get('win_rate', 0.0)), 4),
                'total_ret': round(float(v_metrics.get('total_ret', 0.0)), 4),
            }
            # 影子落盘（去重后可追加）
            sp = os.path.join(SHADOW_DIR, f"v4_shadow_{date}_{sym}.jsonl")
            with open(sp, 'w', encoding='utf-8') as f:
                for s in v_sigs:
                    f.write(json.dumps({'sym': sym, 'date': date, **s}, ensure_ascii=False) + "\n")
            res['v4_shadow_file'] = sp

        res['ok'] = True
        return res
    except Exception as e:
        res['reason'] = f'异常: {e}'
        return res


def np_mean(xs):
    if not xs:
        return 0.0
    import numpy as np
    return float(np.mean(xs))


# ========== 批量运行 ==========

def run_watchlist(date: Optional[str] = None, ds=None) -> Dict[str, Any]:
    """对 watchlist 全部标的跑通用算法 + v4 灰度，落盘并生成对比报告。"""
    if date is None:
        date = datetime.date.today().strftime('%Y-%m-%d')
    flags = load_flags()
    general_cfg = build_general_config(flags)
    v4_gray = bool(flags.get('v4_gray_enable'))
    v4_promote = bool(flags.get('v4_promote'))

    wl = {}
    try:
        with open(WATCHLIST_FILE, encoding='utf-8') as f:
            wl = json.load(f) or {}
    except Exception as e:
        return {'date': date, 'error': f'watchlist 读取失败: {e}'}

    own_ds = False
    if ds is None:
        ds = MootdxDataSource(); own_ds = True

    per_sym: List[Dict[str, Any]] = []
    try:
        for sym, name in wl.items():
            r = process_symbol(sym, name, date, ds, general_cfg, V4FULL_DEFAULT, v4_gray)
            per_sym.append(r)
    finally:
        if own_ds and hasattr(ds, 'close'):
            try:
                ds.close()
            except Exception:
                pass

    # 生产信号落盘（通用算法驱动的最终信号）
    prod = {r['sym']: r.get('general', {}) for r in per_sym if r.get('ok')}
    gen_path = os.path.join(OUT_DIR, f"general_signals_{date}.json")
    with open(gen_path, 'w', encoding='utf-8') as f:
        json.dump({'date': date, 'engine': 'general',
                   'use_general_engine': flags['use_general_engine'],
                   'v4_gray_enable': v4_gray, 'v4_promote': v4_promote,
                   'symbols': prod}, f, ensure_ascii=False, indent=2)

    # 对比报告
    cmp = _build_comparison(date, per_sym, flags)
    cmp_path = os.path.join(OUT_DIR, f"v4_gray_compare_{date}.json")
    with open(cmp_path, 'w', encoding='utf-8') as f:
        json.dump(cmp, f, ensure_ascii=False, indent=2)

    cmp['general_signals_file'] = gen_path
    cmp['compare_file'] = cmp_path
    return cmp


def _build_comparison(date, per_sym, flags) -> Dict[str, Any]:
    rows = []
    for r in per_sym:
        if not r.get('ok'):
            rows.append({'sym': r['sym'], 'name': r.get('name'), 'ok': False, 'reason': r.get('reason')})
            continue
        g = r.get('general', {})
        v = r.get('v4_gray')
        row = {
            'sym': r['sym'], 'name': r.get('name'), 'ok': True,
            'general': {'n_bars': g.get('n_bars'), 'n_signals': g.get('n_signals'),
                        'n_b': g.get('n_b'), 'n_s': g.get('n_s'), 'trips': g.get('trips'),
                        'avg_score': g.get('avg_score'), 'wr': g.get('wr'), 'total_ret': g.get('total_ret')},
        }
        if v is not None:
            row['v4_gray'] = {'n_signals': v.get('n_signals'), 'n_b': v.get('n_b'), 'n_s': v.get('n_s'),
                              'trips': v.get('trips'),
                              'avg_score': v.get('avg_score'), 'wr': v.get('wr'), 'total_ret': v.get('total_ret')}
            # A/B 判定
            delta_wr = round(float((v.get('wr', 0.0) or 0) - (g.get('wr', 0.0) or 0)), 4)
            delta_ret = round(float((v.get('total_ret', 0.0) or 0) - (g.get('total_ret', 0.0) or 0)), 4)
            row['v4_minus_general'] = {'wr': delta_wr, 'total_ret': delta_ret}
            row['v4_better'] = bool(delta_ret > 0)
        rows.append(row)
    return {
        'date': date,
        'engine': 'general',
        'flags': {k: flags[k] for k in ('use_general_engine', 'v4_gray_enable', 'v4_promote', 'bidirectional_enable')},
        'rows': rows,
        'v4_promote_recommend': _recommend_promote(rows),
    }


def _recommend_promote(rows) -> Dict[str, Any]:
    """简单 A/B 建议：v4 在全部可比标的上 total_ret 均不劣于通用算法且多数更优 → 建议 promote。"""
    comp = [r for r in rows if r.get('ok') and 'v4_gray' in r]
    if not comp:
        return {'promote': False, 'reason': '无 v4 灰度数据'}
    better = sum(1 for r in comp if r.get('v4_better'))
    all_ok = all(r['v4_minus_general']['total_ret'] >= -0.5 for r in comp)  # 不显著更差
    promote = (better >= len(comp) * 0.6) and all_ok
    return {'promote': promote, 'v4_better_count': better, 'compared': len(comp),
            'reason': f'v4 更优 {better}/{len(comp)} 标的，且不显著更差'}


# ========== CLI ==========

def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
    print(f"== tpoint watchlist 引擎 [{date}] ==")
    cmp = run_watchlist(date)
    if 'error' in cmp:
        print("❌", cmp['error']); return
    print(f"通用算法驱动标的数: {len(cmp['rows'])}")
    for r in cmp['rows']:
        if not r.get('ok'):
            print(f"  - {r['sym']}: 跳过({r.get('reason')})"); continue
        g = r['general']
        line = f"  - {r['sym']} {r.get('name','')}: 通用 B{g['n_b']}/S{g['n_s']} 配对{g['trips']} WR{g['wr']} 净{g['total_ret']}%"
        if 'v4_gray' in r:
            v = r['v4_gray']
            d = r.get('v4_minus_general', {})
            line += f" | v4灰 B{v['n_b']}/S{v['n_s']} WR{v['wr']} 净{v['total_ret']}% (Δ净{d.get('total_ret')})"
        print(line)
    rec = cmp.get('v4_promote_recommend', {})
    print(f"v4 promote 建议: {rec}")
    print(f"产出: {cmp.get('general_signals_file')} | {cmp.get('compare_file')}")


if __name__ == '__main__':
    main()
