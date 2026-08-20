#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""factor_optimizer.py — tpoint 关键因子参数网格寻优引擎（M3 · 2026-08-04 晚首跑）

回应「自迭代不能只发报告」：对关键因子参数做离线网格寻优，用数据决定改不改、改多少。
闭环（用户已批准 radiant-cascade-babbage）：
  寻优（本脚本，周五任务B集成周跑）→ 两段式验证 → 达标自动灰度写 monitor_config.json（M4）

v1 范围（watchlist 5 只验证段；tune_pool_40 调参段待 40 只池清单就位后补）：
- 出场侧网格：trail_activate_pct {0.3,0.4,0.5} × trail_pct {0.5,0.6,0.8}（信号固定生产配置）
- 信号侧网格：atr_min_pct {0.15,0.25,0.35}（重放 detect_for 重生成信号；出场固定 0.4/0.6）
- 数据：F盘 tickflow 1m 全量历史（300058/600570/688111 ~124-145d；161129/513310 ~66d 薄样本加水印）
- 目标：池级净胜率优先，盈亏比/总收益参考；成本=生产口径（万一+印花+滑点2bps/边）
- 硬约束：候选池级净胜率不得劣于当前配置（全集口径不降）；推荐门槛 ≥+1pp 且 n_trips≥30；
  薄样本标的（<80d）门槛 ≥+2pp

CLI：python scripts/factor_optimizer.py [--syms 300058.SZ,688111.SH] [--out output/factor_opt_<today>.json]
"""
import os, sys, json, argparse, datetime, copy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import monitor as M
import daily_signal_review as R
from exit_manager import make_config, cost_for_symbol, simulate_day, aggregate_metrics
from backtest_screener import load_1m_csv, group_by_day, day_prev_close
from prod_vs_bt_reconcile import recalc_rows_to_sigs

F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')

TRAIL_ACT = [0.3, 0.4, 0.5]
TRAIL_PCT = [0.5, 0.6, 0.8]
ATR_GRID = [0.15, 0.25, 0.35]
CUR_TRAIL = (0.4, 0.6)  # ⚠️ DEPRECATED（2026-08-11）：仅作 EXIT_CFG 默认值的记录与报告展示。
                        # 各标的 trail 自 08-05 起 per-symbol 分化，**基线一律用 prod_trail(sym)**。
                        # 任何新代码引用本常量当基线即为 P0 缺陷（已致 factor_opt/oos_validate 双处失真）。
CUR_ATR = 0.25
THIN_DAYS = 80          # 薄样本水印线
MIN_TRIPS = 30          # 统计可靠性下限
GATE_PP = 1.0           # 推荐门槛（厚样本）
GATE_PP_THIN = 2.0      # 推荐门槛（薄样本）


def sym_days(sym):
    """F盘全历史 → [(date, data, df_day)]（data=build_data 口径）。"""
    full = load_1m_csv(os.path.join(F_DATA, f'{sym}_1m.csv'))
    out = []
    for d, g in group_by_day(full):
        pc = day_prev_close(full, d)
        if pc is None or len(g) < 30:
            continue
        g = g.reset_index(drop=True)
        out.append((d, R.build_data(g, pc), g))
    return out


def prod_trail(sym):
    """该标的**真实生产** trail（monitor_config 优先，缺失回退 EXIT_CFG 默认）。

    [2026-08-11 P0] 原代码用模块级硬编码 CUR_TRAIL=(0.4,0.6) 当 baseline，但 08-05
    v10.0.1 起各标的已 per-symbol 分化（161129=0.5/0.6、513310=0.3/0.5、
    688111=0.4/0.6、300308=0.5/0.6）。auto_tune 用 `cell.ret - baseline.ret` 判定改善，
    baseline 若不是生产真值，等于一直在跟一个**从未上线的配置**比改善 → 改善量虚假。
    """
    return (M.exit_param(sym, 'trail_activate_pct'), M.exit_param(sym, 'trail_pct'))


def day_signals(sym, name, days, atr_min_pct):
    """全部交易日的生产同源复算信号（atr_min_pct 显式覆盖，不再靠改全局配置）。

    [2026-08-11 P0] 原实现靠 `if sym in M.PER_SYMBOL_CFG: ...['atr_min_pct']=v` 覆盖，
    对不在 monitor_config 的标的**静默失效**（atr 网格退化为同一档跑三遍）；且当时
    replay_symbol 根本不读闸门 → 覆盖全程无效。现改为显式入参透传。
    """
    res = []
    for d, data, df in days:
        try:
            rows, _ = R.replay_symbol(sym, name, data,
                                      data.get('pc') or df['close'].iloc[0],
                                      gates='prod', atr_min_pct=atr_min_pct)
            tt = df['trade_time'].values if 'trade_time' in df.columns else None
            sigs = recalc_rows_to_sigs(rows, tt, data['n'])
            data['sym'] = sym  # [2026-08-12 口径修复] 透传 sym 使 eval_config 走正确 per-symbol 成本
                          # （否则 cost=None→DEFAULT_COST 含印花税，LOF/ETF 被误征印花，绝对 ret 偏低）。
            res.append((d, data, sigs))
        except Exception:
            continue
    return res


def day_signals_trail(sym, name, days, atr_min_pct, trail_act, trail_pct):
    """按 (trail_act, trail_pct) 写回 PER_SYMBOL_CFG 后重放信号 —— 消除配置状态泄漏。

    [2026-08-11 P0 配置状态泄漏] core/monitor.py:1188 的 TRAIL 出场信号读
    `exit_param(sym,'trail_*')`，且 TRAIL 触发后 `pos=None` 会**重塑后续整条信号流**。
    所以「信号侧用生产 trail、出场侧用网格 trail」是混合口径，既不可复现也不等于
    该配置真实上线后的表现。原代码只 `sig_base = day_signals(...)` 重放一次，就把同一
    份信号喂给 9 个 trail 网格单元 —— 泄漏。
    实测（513310.SH，74 交易日）：单元级 total_ret 偏差最大 **2.20pp**（= auto_tune
    决策阈值 RET_MIN_IMPROVE 0.2pp 的 11 倍），且最优单元由 0.3/0.6 **翻转**为 0.5/0.8。
    修复成本：同参口径 6s/标的（8.2×），可接受。
    """
    cfg = M.PER_SYMBOL_CFG.setdefault(sym, {})
    saved = {k: cfg.get(k, 'ABSENT') for k in ('trail_activate_pct', 'trail_pct')}
    cfg['trail_activate_pct'] = trail_act
    cfg['trail_pct'] = trail_pct
    try:
        return day_signals(sym, name, days, atr_min_pct)
    finally:
        for k, v in saved.items():
            if v == 'ABSENT':
                cfg.pop(k, None)
            else:
                cfg[k] = v


def eval_config(sig_days, trail_act, trail_pct):
    """对 (trail_act, trail_pct) 跑 simulate_day 聚合全部 trip。"""
    mcfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                       trail_activate_pct=trail_act, trail_pct=trail_pct, s_signal_exit=True)
    trips = []
    for d, data, sigs in sig_days:
        prices = {'o': data['o'], 'h': data['h'], 'lo': data['lo'], 'c': data['c'],
                  'atr': data['atr'], 'trend': data.get('trend'), 'n': data['n'],
                  'date': d,
                  # [2026-08-18 P0 出场侧成交可行性] 透传 pc+sym 供 simulate_day 算锁跌停
                  'pc': data.get('pc'), 'sym': data.get('sym')}
        trips.extend(simulate_day(sigs, prices, mcfg, cost=cost_for_symbol(data.get('sym', '')) if data.get('sym') else None))
    return trips


def metrics_of(trips):
    m = aggregate_metrics(trips)
    out = {'n': m['total'], 'win_rate': m['win_rate'], 'pl_ratio': m['pl_ratio'],
           'total_ret': m.get('total_ret_pct', m.get('total_ret', 0))}
    # [2026-08-17 AQuA 第三点] 透出逐年稳健性, 供 OOS 检验/选股报告统一展示
    if m.get('yearly') is not None:
        out['yearly'] = m['yearly']
        out['yearly_consistent'] = m['yearly_consistent']
        out['worst_year'] = m['worst_year']
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', default=None)
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    wl = json.load(open(WATCHLIST, encoding='utf-8'))
    syms = a.syms.split(',') if a.syms else list(wl.keys())

    # simulate_day 需要 sym 决定成本：把 sym 塞进 data
    report = {'date': datetime.date.today().strftime('%Y-%m-%d'),
              'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'grids': {'trail_activate_pct': TRAIL_ACT, 'trail_pct': TRAIL_PCT, 'atr_min_pct': ATR_GRID},
              'current': {'trail': CUR_TRAIL, 'atr_min_pct': CUR_ATR},
              'symbols': {}, 'recommendations': []}

    pool_cur_trips, pool_best = [], None
    for sym in syms:
        name = wl[sym]
        try:
            days = sym_days(sym)
        except Exception as e:
            report['symbols'][sym] = {'error': str(e)}
            continue
        n_days = len(days)
        thin = n_days < THIN_DAYS
        for d, data, g in days:
            data['sym'] = sym
        # [2026-08-11 P0 修复] 每个网格单元都「先写回 per-symbol 配置再重放」，
        # 保证信号侧与出场侧同参（消除配置状态泄漏）；baseline 取该标的**真实生产 trail**。
        cur_trail = tuple(prod_trail(sym))
        base_label = f'current(atr{CUR_ATR}+trail{cur_trail[0]}/{cur_trail[1]})'
        cells = {}
        base_sigs = day_signals_trail(sym, name, days, CUR_ATR, *cur_trail)
        base_trips = eval_config(base_sigs, *cur_trail)
        cells[base_label] = metrics_of(base_trips)
        base_wr = cells[base_label]['win_rate']
        # trail 网格（逐单元同参重放）
        trail_res = {}
        for ta in TRAIL_ACT:
            for tp in TRAIL_PCT:
                if (ta, tp) == cur_trail:
                    trail_res[f'{ta}/{tp}'] = metrics_of(base_trips)
                    continue
                sig_c = day_signals_trail(sym, name, days, CUR_ATR, ta, tp)
                trail_res[f'{ta}/{tp}'] = metrics_of(eval_config(sig_c, ta, tp))
        # atr 网格（出场固定为该标的生产 trail）
        atr_res = {}
        for av in ATR_GRID:
            if av == CUR_ATR:
                atr_res[str(av)] = metrics_of(base_trips)
                continue
            sig_v = day_signals_trail(sym, name, days, av, *cur_trail)
            atr_res[str(av)] = metrics_of(eval_config(sig_v, *cur_trail))

        gate = GATE_PP_THIN if thin else GATE_PP
        cands = []
        for k, m in trail_res.items():
            if k == f'{cur_trail[0]}/{cur_trail[1]}':
                continue
            if m['n'] >= MIN_TRIPS and m['win_rate'] >= base_wr + gate:
                cands.append(('trail', k, m))
        for k, m in atr_res.items():
            if k == str(CUR_ATR):
                continue
            if m['n'] >= MIN_TRIPS and m['win_rate'] >= base_wr + gate:
                cands.append(('atr_min_pct', k, m))
        cands.sort(key=lambda x: -x[2]['win_rate'])
        rec = None
        if cands:
            kind, val, m = cands[0]
            rec = {'sym': sym, 'name': name, 'param': kind, 'value': val,
                   'win_rate': m['win_rate'], 'delta_pp': round(m['win_rate'] - base_wr, 1),
                   'n_trips': m['n'], 'baseline_wr': base_wr, 'thin': thin,
                   'status': '待两段式tune_pool_40验证后自动灰度(M4)'}
            report['recommendations'].append(rec)
        report['symbols'][sym] = {
            'name': name, 'n_days': n_days, 'thin_sample': thin,
            # baseline 现为该标的真实生产 trail 下的表现（下游 auto_tune 据此算 d_ret）
            'baseline': cells[base_label],
            'baseline_label': base_label,
            'prod_trail': list(cur_trail),
            'same_param_replay': True,      # 网格已逐单元同参重放（无配置状态泄漏）
            'trail_grid': trail_res, 'atr_grid': atr_res,
            'recommendation': rec,
        }
        print(f"[{sym}] days={n_days}{'(薄样本)' if thin else ''} baseWR={base_wr}% "
              f"rec={rec['param'] + '=' + rec['value'] + ' +' + str(rec['delta_pp']) + 'pp' if rec else '无达标候选'}")

    out = a.out or os.path.join(ROOT, 'output', f"factor_opt_{report['date']}.json")
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f'[ok] {out}')
    print(f'推荐 {len(report["recommendations"])} 项（≥+{GATE_PP}pp 且 n≥{MIN_TRIPS}，薄样本≥+{GATE_PP_THIN}pp）')


if __name__ == '__main__':
    main()
