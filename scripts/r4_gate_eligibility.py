# -*- coding: utf-8 -*-
"""
r4_gate_eligibility.py —— 可做性滚动门控（R4 第一步，先选战场再上武器）

核心：用过去 WINDOW_DAYS 天的【滚动 Precision + 滚动 gross edge】双指标打分，
      得分过低的标的/时段标记为「不可做」（禁止开仓/低仓）。
防过拟合：① 滚动窗口（非全历史静态阈值，适应标的近期结构变化）；② 双指标组合；③ 动态判定。

输出：
  1. 逐标滚动得分序列（滚动 Precision / gross edge / eligible 判定）。
  2. 门控有效性：滚动门控后回测净收益 vs 全池基线（-1647%），验证止血幅度。

指标定义：
  Precision  = 窗口内信号点离真极值 ≤ delta_eff 的命中率（DET v2.0，i+1 起 W_PREC=30）。
  net_edge   = 窗口内 simulate_day(A基线) 每笔净收益（扣成本+出场损耗，非毛收益）。
              注：P2 冒烟证明 gross edge 会误判个股（高波动个股毛收益高但净收益负），故用净收益。
"""
import sys, csv, json, os, glob, argparse, datetime, subprocess
import numpy as np
import pandas as pd

ROOT = r'C:/Users/YZP/WorkBuddy/Claw/tpoint'
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from general_signal import detect_signals_general, GENERAL_DEFAULT
from daily_signal_review import build_data
from exit_manager import simulate_day, make_config, cost_for_symbol
from evaluate_signal_validity_v2 import is_fund, _atr_n, W_PREC, C2, DELTA_ABS

DATA_DIR = r'F:/keyfactor_data/1m_clean'
OUT = os.path.join(ROOT, 'output')

HEARTBEAT = os.path.join(OUT, 'r4_gate_heartbeat.txt')
DONE_FLAG = os.path.join(OUT, 'r4_gate_done.flag')
NOTIFY = r'C:/Users/YZP/.workbuddy/notify.py'

WINDOW_DAYS = 15      # 滚动窗口（交易日）。个股数据仅 21-22 天，30 天窗口无法滚动；取 15 天（约 2/3 数据）
PREC_FLOOR = 0.55     # 滚动 Precision 门槛（可调）
EDGE_FLOOR = 0.0      # 滚动净收益门槛（每笔净收益须 >0，即回测不亏钱）


def feishu(text):
    try:
        with open(HEARTBEAT, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + str(text) + '\n')
    except Exception:
        pass
    try:
        subprocess.run([sys.executable, NOTIFY, str(text)], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_days(path):
    rows = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rows.setdefault(row['trade_date'], []).append(row)
    days = {}
    for d, rs in rows.items():
        rs.sort(key=lambda x: x['trade_time'])
        o = np.array([float(x['open']) for x in rs])
        h = np.array([float(x['high']) for x in rs])
        lo = np.array([float(x['low']) for x in rs])
        c = np.array([float(x['close']) for x in rs])
        v = np.array([float(x['volume']) for x in rs])
        days[d] = (o, h, lo, c, v)
    return days


def sym_daily_metrics(sym):
    """逐日生成信号，返回按日聚合的 {date: [prec_hit, net_ret, n_trips]} 序列。
    net_ret = simulate_day(A基线) 当日净收益（扣成本），作为门控的「净收益」指标（非 gross edge）。"""
    path = f'{DATA_DIR}/{sym}_1m.csv'
    if not os.path.exists(path):
        return None
    days_all = load_days(path)
    dates = sorted(days_all.keys())
    cfg = make_config(use_stop=False, use_time=False, use_trailing=True,
                      trail_activate_pct=0.4, trail_pct=0.6, s_signal_exit=True,
                      use_fixed_stop=True, fixed_stop_pct=1.5)   # 正T 生产 EXIT_CFG（非反T默认 atr1.5）
    cost = cost_for_symbol(sym)
    prev_close = None
    daily = []   # [(date, n_sig, prec_hits, net_ret, n_trips)]
    for d in dates:
        o, h_, lo, c, v = days_all[d]
        if len(c) < 20:
            continue
        pc = prev_close if prev_close is not None else c[0]
        df = pd.DataFrame({'open': o, 'high': h_, 'low': lo, 'close': c, 'volume': v,
                           'trade_time': [d + ' 09:31:00'] * len(c)})
        data = build_data(df, pc)
        if data is None:
            continue
        sigs = detect_signals_general(data, pc, GENERAL_DEFAULT)
        n = len(c)
        # 净收益（A 基线回测，扣成本 + 出场损耗）
        prices = {'o': o, 'h': h_, 'lo': lo, 'c': c, 'atr': data['atr'], 'trend': data['trend'],
                  'n': n, 'date': d, 'pc': pc, 'sym': sym}
        trips = simulate_day(sigs, prices, cfg, cost)
        net_ret = sum(float(t['ret_pct']) for t in trips)
        n_trips = len(trips)
        hits = 0
        for s in sigs:
            i = s['idx']; p = float(s['price'])
            atr = _atr_n(h_, lo, c, i, 20)
            delta_eff = max(DELTA_ABS, C2 * atr / p) if p > 0 else DELTA_ABS
            # Precision 命中（i+1 起 W_PREC 局部窗口，消除开天眼）
            end = min(i + 1 + W_PREC, n)
            if (i + 1 >= end) or ((end - (i + 1)) < W_PREC):
                continue
            if s['type'] == 'B':
                base = float(lo[i + 1:end].min())
                dev = (p - base) / base if base > 0 else 1.0
                if dev <= delta_eff:
                    hits += 1
            else:
                base = float(h_[i + 1:end].max())
                dev = (base - p) / base if base > 0 else 1.0
                if dev <= delta_eff:
                    hits += 1
        if n_trips > 0 or len(sigs) > 0:
            daily.append((d, len(sigs), hits, net_ret, n_trips))
        prev_close = c[-1]
    return daily


def roll_gate(daily, prec_floor=PREC_FLOOR):
    """按 WINDOW_DAYS 滚动窗口聚合 prec/net_edge，返回 [(window_end, n, prec, net_edge, eligible)]。"""
    out = []
    for idx in range(len(daily)):
        if idx < WINDOW_DAYS - 1:
            continue
        win = daily[idx - WINDOW_DAYS + 1: idx + 1]
        n_sig = sum(x[1] for x in win)
        hits = sum(x[2] for x in win)
        net_sum = sum(x[3] for x in win)
        n_trips = sum(x[4] for x in win)
        prec = hits / n_sig if n_sig else None
        net_edge = (net_sum / n_trips) if n_trips else None   # 每笔净收益（%）
        eligible = (prec is not None and net_edge is not None and prec >= prec_floor and net_edge > EDGE_FLOOR)
        out.append(dict(window_end=daily[idx][0], n_sig=n_sig, prec=(round(prec, 4) if prec is not None else None),
                        net_edge=(round(net_edge, 5) if net_edge is not None else None), eligible=eligible))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-syms', type=int, default=0, help='0=全部标的（含基金+个股）')
    ap.add_argument('--only-stock', action='store_true', help='仅个股池')
    ap.add_argument('--prec-floor', type=float, default=PREC_FLOOR)
    ap.add_argument('--out-suffix', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    prec_floor = a.prec_floor

    files = sorted(glob.glob(f'{DATA_DIR}/*_1m.csv'))
    syms = [os.path.basename(f).replace('_1m.csv', '') for f in files]
    if a.only_stock:
        syms = [s for s in syms if not is_fund(s)]
    if a.max_syms:
        syms = syms[:a.max_syms]

    feishu(f'🚀 R4 可做性滚动门控启动：{len(syms)} 标的，窗口{WINDOW_DAYS}天，'
           f'门槛 Prec>={prec_floor} & 净收益>0')

    results = {}
    for idx, sym in enumerate(syms):
        daily = sym_daily_metrics(sym)
        if not daily:
            continue
        gates = roll_gate(daily, prec_floor)
        n_win = len(gates)
        n_elig = sum(1 for g in gates if g['eligible'])
        # 滚动 Prec/net_edge 的中位（全窗口）
        precs = [g['prec'] for g in gates if g['prec'] is not None]
        edges = [g['net_edge'] for g in gates if g['net_edge'] is not None]
        results[sym] = dict(
            is_fund=is_fund(sym), n_days=len(daily), n_windows=n_win,
            elig_frac=(round(n_elig / n_win, 3) if n_win else None),
            median_prec=(round(float(np.median(precs)), 4) if precs else None),
            median_net_edge=(round(float(np.median(edges)), 5) if edges else None),
        )
        if (idx + 1) % 40 == 0:
            feishu(f'⏳ R4 门控进度 {idx+1}/{len(syms)} 标的')

    # 汇总：基金 vs 个股的可做性
    funds = {s: r for s, r in results.items() if r['is_fund']}
    stocks = {s: r for s, r in results.items() if not r['is_fund']}
    def _grp(grp):
        if not grp:
            return dict(n=0, elig_frac_mean=None, prec_median=None, net_edge_median=None)
        fracs = [r['elig_frac'] for r in grp.values() if r['elig_frac'] is not None]
        precs = [r['median_prec'] for r in grp.values() if r['median_prec'] is not None]
        edges = [r['median_net_edge'] for r in grp.values() if r['median_net_edge'] is not None]
        return dict(n=len(grp),
                    elig_frac_mean=(round(float(np.mean(fracs)), 3) if fracs else None),
                    prec_median=(round(float(np.median(precs)), 4) if precs else None),
                    net_edge_median=(round(float(np.median(edges)), 5) if edges else None))
    g_fund = _grp(funds); g_stock = _grp(stocks)

    out = dict(
        meta=dict(date=a.out_suffix, window_days=WINDOW_DAYS, prec_floor=prec_floor,
                  edge_floor=EDGE_FLOOR, n_syms=len(results)),
        fund=g_fund, stock=g_stock,
        symbols=results,
    )
    fn = f'r4_gate_{a.out_suffix}.json'
    fp = os.path.join(OUT, fn)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'\n=== R4 可做性滚动门控（{len(results)} 标的，窗口{WINDOW_DAYS}天）===')
    print(f'  基金({g_fund["n"]}只): 可做占比={g_fund["elig_frac_mean"]} 滚动Prec中位={g_fund["prec_median"]} 净收益中位={g_fund["net_edge_median"]}')
    print(f'  个股({g_stock["n"]}只): 可做占比={g_stock["elig_frac_mean"]} 滚动Prec中位={g_stock["prec_median"]} 净收益中位={g_stock["net_edge_median"]}')
    print(f'JSON -> {fp}')

    feishu(f'✅ R4 可做性滚动门控完成（{len(results)} 标的）：\n'
           f'基金({g_fund["n"]}只) 可做占比={g_fund["elig_frac_mean"]} Prec中位={g_fund["prec_median"]} 净收益中位={g_fund["net_edge_median"]}；\n'
           f'个股({g_stock["n"]}只) 可做占比={g_stock["elig_frac_mean"]} Prec中位={g_stock["prec_median"]} 净收益中位={g_stock["net_edge_median"]}\n产物 output/{fn}')

    try:
        with open(DONE_FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' done\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        feishu(f'💥 R4 可做性滚动门控异常: {e}')
        raise
