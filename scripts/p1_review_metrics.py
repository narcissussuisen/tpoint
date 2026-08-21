#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 多日稳定性评审聚合器 — loop engineering 的 P1 gate 量化输入

读取 output/p1_gray_status_*.json（由 p1_gray_monitor.py 每日生成），
聚合为「反T + regime 灰度实盘信号质量稳定性」指标，输出给量化专家 agent
作为评审 gate 的量化证据。

验收口径（信号质量稳定，无阻断项）：
  1) 观测交易日数 >= MIN_DAYS（默认 5）
  2) 配置全期稳定：config_ok 每日均 True（bidirectional/regime 开启，settle_split 关闭）
  3) 有效配对率稳定：日均 valid_rate_pct >= VR_FLOOR 且 日度 std <= VR_STD_MAX
  4) 净收益无系统性灾难：无「valid>0 但当日 net < NET_BLOCK_PCT」的阻断日
  5) 反T 信号持续产出（观察项，非阻断）

用法：
  python scripts/p1_review_metrics.py
  python scripts/p1_review_metrics.py --min-days 5
"""
import os, sys, json, glob, argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'output')

# 稳定性阈值（可在调用时覆盖）
DEFAULT_MIN_DAYS = 5        # 至少观测交易日数
DEFAULT_VR_FLOOR = 25.0     # 日均有效配对率下限 (%)
DEFAULT_VR_STD_MAX = 20.0   # 日度有效配对率标准差上限 (pp)
DEFAULT_NET_BLOCK_PCT = -5.0  # 单日 net 阻断阈值：valid>0 但 net 低于此值视为阻断


def load_all():
    files = sorted(glob.glob(os.path.join(OUT, 'p1_gray_status_*.json')))
    recs = []
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                recs.append(json.load(f))
        except Exception as e:
            print(f'[warn] 跳过无法解析: {fp} ({e})')
    return recs


def main():
    ap = argparse.ArgumentParser(description='P1 多日稳定性评审聚合')
    ap.add_argument('--min-days', type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument('--vr-floor', type=float, default=DEFAULT_VR_FLOOR)
    ap.add_argument('--vr-std-max', type=float, default=DEFAULT_VR_STD_MAX)
    ap.add_argument('--net-block', type=float, default=DEFAULT_NET_BLOCK_PCT)
    args = ap.parse_args()

    recs = load_all()
    if not recs:
        print('[P1 review] 暂无灰度状态文件，请先运行 p1_gray_monitor.py 采集数据。')
        out = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'n_days': 0,
            'review_ready': False,
            'blocking': ['no_data'],
            'gate_input': {},
            'daily': [],
        }
        with open(os.path.join(OUT, 'p1_review_metrics.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return

    # 只统计有实盘数据的观测日（排除 pending_data）
    days = [r for r in recs if r.get('p1_status') != 'pending_data']
    days.sort(key=lambda r: r.get('date', ''))

    daily = []
    for r in days:
        lr = r.get('live_review', {}) or {}
        at = r.get('anti_t', {}) or {}
        daily.append({
            'date': r.get('date'),
            'config_ok': r.get('config_ok'),
            'pushes': lr.get('pushes'),
            'paired': lr.get('paired'),
            'valid': lr.get('valid'),
            'valid_rate_pct': lr.get('valid_rate_pct'),
            'net_ret_pct': lr.get('net_ret_pct'),
            'regime': lr.get('regime_tag'),
            'anti_t_trips': at.get('anti_t_trips'),
            'anti_t_net': at.get('anti_t_net_ret_pct'),
        })

    n_days = len(days)
    config_ok_all = all(bool(r.get('config_ok')) for r in days) if days else False

    vr_list = [d['valid_rate_pct'] for d in daily if d['valid_rate_pct'] is not None]
    net_list = [d['net_ret_pct'] for d in daily if d['net_ret_pct'] is not None]
    regime_dist = {}
    for d in daily:
        regime_dist[d['regime']] = regime_dist.get(d['regime'], 0) + 1

    total_pushes = sum(d['pushes'] or 0 for d in daily)
    total_paired = sum(d['paired'] or 0 for d in daily)
    total_valid = sum(d['valid'] or 0 for d in daily)
    total_anti_t = sum(d['anti_t_trips'] or 0 for d in daily)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    def std(xs):
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

    vr_mean = mean(vr_list)
    vr_std = std(vr_list)
    net_mean = mean(net_list)
    net_std = std(net_list)

    # 阻断项检查
    blocking = []
    if n_days < args.min_days:
        blocking.append(f'insufficient_days(n={n_days}<{args.min_days})')
    if not config_ok_all:
        blocking.append('config_unstable(bidirectional/regime/settle_split 有日异常)')
    if vr_mean is not None and vr_mean < args.vr_floor:
        blocking.append(f'valid_rate_low(mean={vr_mean:.1f}%<{args.vr_floor}%)')
    if vr_std is not None and vr_std > args.vr_std_max:
        blocking.append(f'valid_rate_unstable(std={vr_std:.1f}pp>{args.vr_std_max}pp)')
    # 净收益灾难阻断：valid>0 但当日 net 低于阈值
    for d in daily:
        if d['valid'] and d['valid'] > 0 and d['net_ret_pct'] is not None and d['net_ret_pct'] < args.net_block:
            blocking.append(f'net_catastrophe({d["date"]}:valid={d["valid"]},net={d["net_ret_pct"]}%)')
            break

    review_ready = len(blocking) == 0

    gate_input = {
        'n_days': n_days,
        'config_ok_all': config_ok_all,
        'total_pushes': total_pushes,
        'total_paired': total_paired,
        'total_valid': total_valid,
        'overall_valid_rate_pct': round(total_valid / total_paired * 100, 2) if total_paired else None,
        'valid_rate_mean_pct': round(vr_mean, 2) if vr_mean is not None else None,
        'valid_rate_std_pp': round(vr_std, 2) if vr_std is not None else None,
        'net_mean_pct': round(net_mean, 4) if net_mean is not None else None,
        'net_std_pp': round(net_std, 4) if net_std is not None else None,
        'regime_distribution': regime_dist,
        'total_anti_t_trips': total_anti_t,
        'blocking': blocking,
        'review_ready': review_ready,
    }

    out = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'thresholds': {
            'min_days': args.min_days, 'vr_floor': args.vr_floor,
            'vr_std_max': args.vr_std_max, 'net_block_pct': args.net_block,
        },
        'n_days': n_days,
        'review_ready': review_ready,
        'blocking': blocking,
        'gate_input': gate_input,
        'daily': daily,
    }

    out_path = os.path.join(OUT, 'p1_review_metrics.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'[P1 review] 观测交易日={n_days}  review_ready={review_ready}')
    print(f'  config_ok_all={config_ok_all}')
    print(f'  total: pushes={total_pushes} paired={total_paired} valid={total_valid} overall_vr={gate_input["overall_valid_rate_pct"]}%')
    print(f'  valid_rate: mean={gate_input["valid_rate_mean_pct"]}% std={gate_input["valid_rate_std_pp"]}pp')
    print(f'  net: mean={gate_input["net_mean_pct"]}% std={gate_input["net_std_pp"]}pp')
    print(f'  regime_dist={regime_dist}  antiT_trips={total_anti_t}')
    if blocking:
        print(f'  BLOCKING: {blocking}')
    else:
        print(f'  -> 无阻断项，可提交量化专家 agent 评审 (gate PASS)')
    print(f'  -> {os.path.relpath(out_path, ROOT)}')


if __name__ == '__main__':
    main()
