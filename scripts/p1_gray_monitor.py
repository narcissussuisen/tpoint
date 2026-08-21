#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 灰度观测脚本 — 反T + regime 门控实盘灰度日报

读取当日的 prod_vs_bt_reconcile 与 live_roundtrip_review 产物，
汇总灰度观测指标，判断灰度状态是否健康，输出 JSON 供每日复盘与 agent 评审。

用法：
  python scripts/p1_gray_monitor.py --date 2026-08-21
"""
import os, sys, json, argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)

def gray_config():
    cfg_path = os.path.join(ROOT, 'data', 'monitor_config.json')
    cfg = load_json(cfg_path) or {}
    glob = cfg.get('_global', {})
    ga = glob.get('general_algorithm', {})
    return {
        'bidirectional_enable': bool(glob.get('bidirectional_enable')),
        'regime_gate': bool(ga.get('regime_gate')),
        'regime_lookback': int(ga.get('regime_lookback', 0)),
        'regime_downtrend_thresh': float(ga.get('regime_downtrend_thresh', 0.0)),
        'settle_split_enable': bool(glob.get('settle_split_enable')),
        'signal_gap': int(ga.get('signal_gap', 0)),
    }

def count_anti_t(roundtrip_path):
    """统计 roundtrip jsonl 中反T（S→B回补/EOD）的 trip 数与净收益。"""
    if not os.path.exists(roundtrip_path):
        return {'anti_t_trips': 0, 'anti_t_valid': 0, 'anti_t_net_ret_pct': None}
    trips, valid, net_sum = 0, 0, 0.0
    with open(roundtrip_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('source') != 'live':
                continue
            if r.get('dir') != '反T':
                continue
            trips += 1
            if r.get('valid'):
                valid += 1
            net_sum += float(r.get('net_ret_pct') or 0.0)
    return {
        'anti_t_trips': trips,
        'anti_t_valid': valid,
        'anti_t_net_ret_pct': round(net_sum, 4) if trips else None,
    }

def main():
    ap = argparse.ArgumentParser(description='P1 反T+regime 灰度观测')
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    args = ap.parse_args()
    date = args.date

    rec = load_json(os.path.join(ROOT, 'output', f'reconcile_{date}.json'))
    live = load_json(os.path.join(ROOT, 'output', f'live_review_{date}.json'))
    rt_path = os.path.join(ROOT, 'data', 'roundtrip', f'{date}.jsonl')

    cfg = gray_config()

    # 从 reconcile 提取各标的信息
    sym_stats = {}
    if rec:
        for sym, srec in rec.get('symbols', {}).items():
            lc = srec.get('live_counts', {})
            sym_stats[sym] = {
                'live_B': int(lc.get('B', 0)),
                'live_S': int(lc.get('S', 0)),
                'live_total': int(lc.get('total', 0)),
                'recalc_total': int(srec.get('recalc_n_signals', 0)),
                'wr_prod_exec': srec.get('wr_prod_exec'),
                'wr_recalc': srec.get('wr_recalc'),
                'g1_pp': srec.get('g1_sym'),
            }

    pool = rec.get('pool', {}) if rec else {}
    live_summary = live.get('summary', {}) if live else {}

    # regime 抑制粗略估计：复算 B 数 - 实盘 B 数（含 regime + 其他门控的共同作用）
    recalc_b_total = sum(s.get('recalc_total', 0) for s in sym_stats.values())  # 这里用 recalc_total 近似，更准确需分 B/S
    live_b_total = sum(s.get('live_B', 0) for s in sym_stats.values())
    # 更准确的 regime 抑制率需要从 recalc 明细算，这里先占位
    regime_suppress_estimate = None
    if recalc_b_total > 0:
        regime_suppress_estimate = round((recalc_b_total - live_b_total) / recalc_b_total * 100, 1)

    anti_t = count_anti_t(rt_path)

    result = {
        'date': date,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'gray_config': cfg,
        'config_ok': cfg['bidirectional_enable'] and cfg['regime_gate'] and not cfg['settle_split_enable'],
        'pool': {
            'wr_prod_exec': pool.get('wr_prod_exec'),
            'wr_recalc': pool.get('wr_recalc'),
            'g1_pp': pool.get('g1_pp'),
            'n_live_trips': pool.get('n_live_trips'),
            'n_recalc_trips': pool.get('n_recalc_trips'),
        },
        'live_review': {
            'pushes': live_summary.get('n_pushes'),
            'paired': live_summary.get('n_trips'),
            'valid': live_summary.get('n_valid'),
            'valid_rate_pct': live_summary.get('valid_rate_pct'),
            'net_ret_pct': live_summary.get('net_sum_pct'),
            'avg_quality_score': live_summary.get('avg_quality_score'),
            'regime_tag': live_summary.get('regime'),
        },
        'anti_t': anti_t,
        'symbols': sym_stats,
        'regime_suppress_estimate_pct': regime_suppress_estimate,
        'p1_status': 'pending_data' if not rec else 'observing',
    }

    out_path = os.path.join(ROOT, 'output', f'p1_gray_status_{date}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f'[P1 gray {date}] config_ok={result["config_ok"]}')
    print(f'  gray: bidirectional={cfg["bidirectional_enable"]} regime_gate={cfg["regime_gate"]} settle_split={cfg["settle_split_enable"]}')
    print(f'  pool: wr_live={pool.get("wr_prod_exec")} wr_recalc={pool.get("wr_recalc")} g1={pool.get("g1_pp")}pp')
    print(f'  live_review: pushes={result["live_review"]["pushes"]} paired={result["live_review"]["paired"]} valid={result["live_review"]["valid"]}({result["live_review"]["valid_rate_pct"]}%) net={result["live_review"]["net_ret_pct"]}% regime={result["live_review"]["regime_tag"]}')
    print(f'  antiT: trips={anti_t["anti_t_trips"]} valid={anti_t["anti_t_valid"]} net={anti_t["anti_t_net_ret_pct"]}%')
    print(f'  -> {os.path.relpath(out_path, ROOT)}')

if __name__ == '__main__':
    main()
