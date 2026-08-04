#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""live_roundtrip_review.py — 实盘推送 round-trip 有效性分析器（2026-08-04 报告实盘化重构 M1）

报告哲学（用户拍板）：复盘只看「真实推送给我的交易信号」——push_audit.jsonl（飞书ACK确认）
是唯一权威信号源；复算信号不进报告。有效判定 = 一B一S（或反T S→回补B）完整做完一个T，
**净盈亏 > 0**（扣双边成本：佣金万1+印花(仅股票)+滑点2bps/边），替代旧 0.15% 阈值。

产出 output/live_review_<date>.json：
  trips[]      逐笔 round-trip（方向/进出场/毛差/成本/净盈亏/有效/滑点/亏损归因）
  summary      当日汇总（推送数/配对数/有效率/净盈亏合计/毛差合计）
  baseline     近5交易日「实盘推送 round-trip」基线（push_audit 07-23 起，稀疏标样本不足）
  volatility   当日行情捕获分析（波动段法）：有效波动段/捕获率/未捕获归因/因子改进建议

CLI：python scripts/live_roundtrip_review.py --date 2026-08-04
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.environ['MACD_GATE_MODE'] = 'floor'

import daily_signal_review as R                       # fetch_1m/get_pc/build_data/load_push_audit/prev_trading_days
from datasource import MootdxDataSource
from exit_manager import cost_for_symbol
from backtest_screener import load_1m_csv, group_by_day, day_prev_close

F_DATA = r'F:\keyfactor_data\1m'
AUDIT = os.path.join(ROOT, 'data', 'push_audit.jsonl')
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')
OUT = os.path.join(ROOT, 'output')


# --------------------------------------------------------------------------- #
# 数据获取：mootdx（当日/近日）→ F盘 tickflow 兜底（历史日）
# --------------------------------------------------------------------------- #
def fetch_day(ds, sym, date):
    """返回 (closes:list, times:list[str], pc:float, src)；失败 (None,None,None,reason)。"""
    try:
        pc = R.get_pc(ds, sym, date)
        df = R.fetch_1m(ds, sym, date)
        if df is not None and len(df) >= 5 and pc:
            tt = [str(t) for t in (df['trade_time'].values if 'trade_time' in df.columns else df.index)]
            return [float(x) for x in df['close'].values], tt, float(pc), 'mootdx'
    except Exception:
        pass
    csv_path = os.path.join(F_DATA, f'{sym}_1m.csv')
    if not os.path.exists(csv_path):
        return None, None, None, 'no_data'
    try:
        full = load_1m_csv(csv_path)
        pc = day_prev_close(full, date)
        sub = None
        for d, g in group_by_day(full):
            if d == date:
                sub = g
                break
        if sub is None or len(sub) < 5 or not pc:
            return None, None, None, 'fdisk_no_day'
        tt = [str(t) for t in (sub['trade_time'].values if 'trade_time' in sub.columns else sub.index)]
        return [float(x) for x in sub['close'].values], tt, float(pc), 'fdisk'
    except Exception as e:
        return None, None, None, f'fdisk_err({e})'


def time_to_idx(times, hhmmss):
    """推送时间 → bar idx：HH:MM 精确匹配优先，否则取 <= 推送时间的最后一根。"""
    if not hhmmss:
        return -1
    hhmm = hhmmss[:5]
    cand = -1
    for k, t in enumerate(times):
        ts = str(t)
        if len(ts) >= 16 and ts[11:16] == hhmm:
            return k
        if len(ts) >= 19 and ts[11:19] <= hhmmss:
            cand = k
    return cand


# --------------------------------------------------------------------------- #
# round-trip 配对（实盘语义：单仓位；B→S/X/EOD 正T；S→B回补/EOD 反T）
# --------------------------------------------------------------------------- #
def pair_trips(sym, pushes, closes, times, use_push_price=True):
    """pushes: [{ts,type,price}]（已按时间排序、仅当日 ok 记录）。
    价格口径（2026-08-04 晚修正）：当日同源行情（mootdx）→ **用推送价**（用户真实可成交价；
    实证 688111 反T 推277.28/补275.00=+0.82% 盈利，bar close 口径会错判为 -0.15% 亏损）；
    历史日 F盘兜底（复权差可达2.6%）→ 用信号 bar close。EOD 强平恒用收盘 bar close。
    另一口径价格记入 *_bar_close 与 *_slip_pct 供参考。"""
    n = len(closes)
    trips, orphans = [], []
    pos = None
    for p in pushes:
        idx = time_to_idx(times, p['ts'][11:19] if len(p['ts']) >= 19 else None)
        if idx < 0 or idx >= n:
            orphans.append({'ts': p['ts'], 'type': p['type'], 'note': '无法映射到行情bar'})
            continue
        typ = p['type']
        px = (p.get('price') if (use_push_price and p.get('price')) else None) or closes[idx]
        if pos is None:
            if typ in ('B', 'S'):
                pos = {'dir': '正T' if typ == 'B' else '反T', 'entry_idx': idx,
                       'entry_price': px, 'entry_bar': closes[idx],
                       'entry_ts': p['ts'], 'entry_push': p.get('price')}
            else:
                orphans.append({'ts': p['ts'], 'type': 'X', 'note': '无持仓的出场信号（忽略）'})
            continue
        if pos['dir'] == '正T' and typ in ('S', 'X'):
            trips.append(_close_trip(sym, pos, idx, px, closes[idx], p, typ))
            pos = None
        elif pos['dir'] == '反T' and typ == 'B':
            trips.append(_close_trip(sym, pos, idx, px, closes[idx], p, 'B回补'))
            pos = None
        # 同向重复/杂散信号：单仓位模型忽略（〇节已标注重复）
    if pos is not None:
        trips.append(_close_trip(sym, pos, n - 1, closes[-1], closes[-1],
                                 {'ts': str(times[-1]) + ' EOD', 'price': None}, 'EOD'))
    return trips, orphans


def _close_trip(sym, pos, exit_idx, exit_price, exit_bar, exit_push, reason):
    e, x = pos['entry_price'], exit_price
    gross = ((x - e) / e * 100) if pos['dir'] == '正T' else ((e - x) / e * 100)
    buy_c, sell_c = cost_for_symbol(sym)
    cost = buy_c + sell_c
    net = gross - cost
    def slip(push_px, bar_px):
        return round((bar_px - push_px) / push_px * 100, 3) if push_px else None
    return {
        'sym': sym, 'dir': pos['dir'],
        'entry_time': pos['entry_ts'][11:16] if len(pos['entry_ts']) >= 16 else pos['entry_ts'],
        'exit_time': exit_push['ts'][11:16] if len(exit_push['ts']) >= 16 else exit_push['ts'],
        'entry_price': round(e, 3), 'exit_price': round(x, 3),
        'entry_bar_close': round(pos['entry_bar'], 3), 'exit_bar_close': round(exit_bar, 3),
        'entry_push_price': pos.get('entry_push'), 'exit_push_price': exit_push.get('price'),
        'entry_slip_pct': slip(pos.get('entry_push'), pos['entry_bar']),
        'exit_slip_pct': slip(exit_push.get('price'), exit_bar),
        'hold_bars': exit_idx - pos['entry_idx'], 'exit_reason': reason,
        'gross_ret_pct': round(gross, 3), 'cost_pct': round(cost, 3),
        'net_ret_pct': round(net, 3), 'valid': net > 0,
    }


def attribute_loss(trip, max_fav_pct):
    """亏损 T 单根因分类。"""
    g, net = trip['gross_ret_pct'], trip['net_ret_pct']
    tags = []
    if g > 0 and net <= 0:
        tags.append('毛差不足以覆盖双边成本（成本线附近抖动单）')
    if g <= 0:
        if trip['exit_reason'] == 'EOD':
            tags.append('进场后方向未兑现，持有至收盘强平（趋势/时机误判）')
        elif trip['exit_reason'] in ('S', 'X'):
            tags.append('进场后反向运行，被对手信号/出场信号止损（方向误判）')
        else:
            tags.append('进场后反向运行（方向误判）')
    if max_fav_pct is not None and max_fav_pct > trip['cost_pct'] and net <= 0:
        tags.append(f'盘中曾有 +{max_fav_pct:.2f}% 有利波动（>成本线）但未止盈——出场规则失效（TRAIL 激活/回撤参数待寻优）')
    slips = [s for s in (trip.get('entry_slip_pct'), trip.get('exit_slip_pct')) if s is not None and abs(s) > 0.15]
    if slips:
        tags.append('推送价与信号bar收盘价偏离>0.15%（推送延迟/行情源差异）')
    return tags if tags else ['未归类']


# --------------------------------------------------------------------------- #
# 波动段分析（zigzag）：有效波动 = 相邻高低点间幅度 ≥ 双边成本线的段
# --------------------------------------------------------------------------- #
def zigzag_segments(closes, times, thr_pct):
    n = len(closes)
    segs = []
    start_i, ex_i, direction = 0, 0, None
    for i in range(1, n):
        c = closes[i]
        if direction is None:
            if c > closes[ex_i]:
                direction, ex_i = 'up', i
            elif c < closes[ex_i]:
                direction, ex_i = 'down', i
        elif direction == 'up':
            if c >= closes[ex_i]:
                ex_i = i
            elif (closes[ex_i] - c) / closes[ex_i] * 100 >= thr_pct:
                amp = (closes[ex_i] - closes[start_i]) / closes[start_i] * 100
                segs.append({'s': start_i, 'e': ex_i, 'dir': 'up', 'amp': round(amp, 3)})
                start_i, ex_i, direction = ex_i, i, 'down'
        else:
            if c <= closes[ex_i]:
                ex_i = i
            elif (c - closes[ex_i]) / closes[ex_i] * 100 >= thr_pct:
                amp = (closes[start_i] - closes[ex_i]) / closes[start_i] * 100
                segs.append({'s': start_i, 'e': ex_i, 'dir': 'down', 'amp': round(amp, 3)})
                start_i, ex_i, direction = ex_i, i, 'up'
    if direction:
        amp = ((closes[ex_i] - closes[start_i]) / closes[start_i] * 100) if direction == 'up' \
            else ((closes[start_i] - closes[ex_i]) / closes[start_i] * 100)
        if amp > 0:
            segs.append({'s': start_i, 'e': ex_i, 'dir': direction, 'amp': round(amp, 3)})
    for s in segs:
        s['t0'] = str(times[s['s']])[11:16]
        s['t1'] = str(times[s['e']])[11:16]
    return [s for s in segs if s['amp'] >= thr_pct]


def analyze_capture(sym, closes, times, pushes, trips):
    """波动段捕获分析：有效段逐段归因（已捕获/有信号未配对/方向相反/无信号触发）。"""
    thr = sum(cost_for_symbol(sym))
    segs = zigzag_segments(closes, times, thr)
    sig_marks = []
    for p in pushes:
        idx = time_to_idx(times, p['ts'][11:19] if len(p['ts']) >= 19 else None)
        if idx >= 0:
            sig_marks.append((idx, p['type']))
    for s in segs:
        in_seg = [(i, t) for i, t in sig_marks if s['s'] <= i <= s['e']]
        want = 'B' if s['dir'] == 'up' else 'S'
        hit_trip = any(t['dir'] == ('正T' if s['dir'] == 'up' else '反T')
                       and t['entry_time'] >= s['t0'] and t['entry_time'] <= s['t1'] for t in trips)
        if hit_trip:
            s['attr'] = '已捕获'
        elif any(t == want for _, t in in_seg):
            s['attr'] = '有信号但未完成配对'
        elif in_seg:
            s['attr'] = '信号方向相反'
        else:
            s['attr'] = '无信号触发'
    total_amp = round(sum(s['amp'] for s in segs), 3)
    captured = round(sum(max(t['gross_ret_pct'], 0) for t in trips), 3)
    rate = round(min(captured / total_amp * 100, 100), 1) if total_amp > 0 else None
    # 显著段（≥3×成本线）：微波动段分母过大时的可读子集（ETF 成本线 0.06% 会切出大量碎段）
    sig_segs = [s for s in segs if s['amp'] >= 3 * thr]
    sig_amp = round(sum(s['amp'] for s in sig_segs), 3)
    sig_captured = round(sum(max(t['gross_ret_pct'], 0) for t in trips), 3)  # 同源毛差，展示口径=显著段
    sig_rate = round(min(sig_captured / sig_amp * 100, 100), 1) if sig_amp > 0 else None
    none_amp = sum(s['amp'] for s in segs if s['attr'] == '无信号触发')
    opp_amp = sum(s['amp'] for s in segs if s['attr'] == '信号方向相反')
    unpaired_amp = sum(s['amp'] for s in segs if s['attr'] == '有信号但未完成配对')
    # 因子改进建议（归因 → 参数映射）
    if total_amp == 0:
        sug = '全天无超成本线波动段，算法无发挥空间，无需调整。'
    elif none_amp / total_amp > 0.5:
        sug = ('未捕获段过半数无信号触发 → 触发灵敏度不足：候选因子 atr_min_pct↓(0.25→0.15) / '
               'TP_MHD_THRESHOLD↓(0.15→0.10) 进 factor_optimizer 网格寻优（M3）。')
    elif opp_amp > unpaired_amp and opp_amp / total_amp > 0.3:
        sug = ('未捕获段以方向相反为主 → 方向过滤待修：候选 mpr_periods/mpr_enable 寻优（R2）；'
               'engine_signals.jsonl 落盘（M2）后可区分「被抑制」与「未触发」。')
    elif rate is not None and rate >= 60:
        sug = '捕获率良好 → 重点转向出场侧：trail_activate_pct/trail_pct 网格寻优（M3 首轮已含）。'
    else:
        sug = ('捕获率中等，混合成因 → 待 engine_signals.jsonl（M2）落盘后精确区分抑制/未触发；'
               'trail 与 atr 参数进 M3 网格。')
    return {
        'thr_pct': round(thr, 3), 'n_valid_segs': len(segs), 'total_amp_pct': total_amp,
        'captured_pct': captured, 'capture_rate_pct': rate, 'segments': segs,
        'sig_n_segs': len(sig_segs), 'sig_amp_pct': sig_amp, 'sig_capture_rate_pct': sig_rate,
        'attr_amp': {'无信号触发': round(none_amp, 3), '信号方向相反': round(opp_amp, 3),
                     '有信号未配对': round(unpaired_amp, 3)},
        'suggestion': sug,
    }


# --------------------------------------------------------------------------- #
# 优化空间清单（2026-08-04 晚用户要求：正文只给结论三要素，明细留后台 JSON）
# --------------------------------------------------------------------------- #
def build_opportunities(vol, per_sym):
    """跨标的聚合波动段归因 → [{problem, cause, direction}]，按严重度排序，≤5 条。"""
    opps = []
    ok_vol = {s: v for s, v in vol.items() if 'total_amp_pct' in v}
    if not ok_vol:
        return opps
    pool_amp = sum(v['total_amp_pct'] for v in ok_vol.values())
    pool_sig = sum(v.get('sig_amp_pct', 0) for v in ok_vol.values())
    pool_cap = sum(v['captured_pct'] for v in ok_vol.values())
    none_amp = sum(v['attr_amp']['无信号触发'] for v in ok_vol.values())
    opp_amp = sum(v['attr_amp']['信号方向相反'] for v in ok_vol.values())
    unpaired_amp = sum(v['attr_amp']['有信号未配对'] for v in ok_vol.values())

    # 1) 零信号标的（有显著波动但全程无推送）——最高优先级
    zero_syms = [(s, v) for s, v in ok_vol.items()
                 if per_sym.get(s, {}).get('n_push', 0) == 0 and v['total_amp_pct'] > 3]
    if zero_syms:
        names = '、'.join(v['name'] for _, v in zero_syms)
        amp = sum(v['total_amp_pct'] for _, v in zero_syms)
        sig = sum(v.get('sig_amp_pct', 0) for _, v in zero_syms)
        opps.append({
            'severity': 100,
            'problem': f'{names} 全天零信号推送，但当日存在有效波动合计 {amp:.1f}%（显著段 {sig:.1f}%）——波动在走、算法在看戏',
            'cause': '均线引力(dev)+MHD 背离+ATR 门控的组合触发条件对该类标的当日波动形态全部不满足；'
                     '因子库缺少 RSI/KDJ 类经典高低点反转因子做兜底',
            'direction': '①factor_optimizer 对零信号标的加跑更宽网格（atr/mhd/mpr 联合）定位卡死闸门；'
                         '②RSI/KDJ 超买超卖反转因子立项评估（离线两段式验证后以小版本上线）',
        })
    # 2) 无信号触发占比
    if pool_amp > 0 and none_amp / pool_amp > 0.5:
        opps.append({
            'severity': 80,
            'problem': f'全标的有效波动 {pool_amp:.1f}% 中 {none_amp:.1f}%（{none_amp / pool_amp * 100:.0f}%）时段无任何信号触发',
            'cause': '触发灵敏度不足：atr_min_pct=0.25 与 MHD 点数阈值 0.15 对低波动标的偏严；'
                     '早盘首扫抑制窗口吞掉开盘波动段',
            'direction': 'atr_min_pct/TP_MHD_THRESHOLD 进每日网格寻优；首扫抑制 startup_suppress_min 参数化灰度（R1）',
        })
    # 3) 方向相反
    if pool_amp > 0 and opp_amp / pool_amp > 0.3:
        opps.append({
            'severity': 60,
            'problem': f'{opp_amp:.1f}% 有效波动段内信号方向与波动方向相反',
            'cause': 'mpr60 大周期方向过滤与日内拐点错位；反向段无反手逻辑',
            'direction': 'mpr_periods/mpr_enable 寻优（R2）；评估反手信号的可行性（大版本）',
        })
    # 4) 有信号未配对
    if pool_amp > 0 and unpaired_amp / pool_amp > 0.2:
        opps.append({
            'severity': 50,
            'problem': f'{unpaired_amp:.1f}% 有效波动段有信号但未完成 round-trip 配对',
            'cause': '单仓位模型下同向重复信号被忽略；出场信号缺位致持仓空置',
            'direction': '评估 S 侧反手/加仓规则；出场完整性由 exit_v3（R4）兜底',
        })
    # 5) 显著段捕获率低
    if pool_sig > 0:
        sig_rate = pool_cap / pool_sig * 100
        if sig_rate < 30:
            opps.append({
                'severity': 40,
                'problem': f'显著段（≥3×成本线）捕获率仅 {sig_rate:.1f}%——大波动段基本没吃到',
                'cause': '进出场参数保守：trail 0.4/0.6 激活早回撤紧，趋势段提前下车；进场等待确认过久',
                'direction': 'trail 网格寻优首跑已出候选（0.5/0.5 池级 +3.7~12.5pp），待两段式复核后灰度',
            })
    if not opps:
        opps.append({
            'severity': 10,
            'problem': '当日未发现结构性捕获短板',
            'cause': '有效波动段均有信号覆盖',
            'direction': '维持当前参数，继续每日寻优监控漂移',
        })
    opps.sort(key=lambda x: -x['severity'])
    return opps[:5]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    a = ap.parse_args()
    date = a.date

    wl = json.load(open(WATCHLIST, encoding='utf-8'))
    audit_all = R.load_push_audit(AUDIT, None) if hasattr(R, 'load_push_audit') else []
    # load_push_audit(path, date)：date=None 时返回全部（按行解析）
    if not audit_all:
        audit_all = []
        with open(AUDIT, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        audit_all.append(json.loads(line))
                    except Exception:
                        pass
    today_push = [r for r in audit_all if str(r.get('ts', '')).startswith(date) and r.get('ok') and r.get('sym') in wl]
    today_push.sort(key=lambda r: r['ts'])
    # 同分钟同型去重（2026-08-04 实证：并发进程 09:44:15/09:44:43 连推两张 B 卡。
    # 与 prod_vs_bt_reconcile 合并规则一致：sym+op+同分钟视为同一信号；重复笔不进配对，进 orphans 披露）
    _seen, _dedup, dup_orphans = set(), [], []
    for r in today_push:
        key = (r['sym'], r.get('type'), str(r.get('ts', ''))[:16])
        if key in _seen:
            dup_orphans.append({'sym': r['sym'], 'ts': r['ts'], 'type': r.get('type'),
                                'note': '同分钟同型重复推送（并发进程），配对去重'})
        else:
            _seen.add(key)
            _dedup.append(r)
    today_push = _dedup

    ds = MootdxDataSource()
    trips_all, orphans_all, per_sym, vol = [], dup_orphans[:], {}, {}
    syms_with_push = sorted({r['sym'] for r in today_push})

    # 当日：有推送标的配对 + 全部 5 标的波动段分析
    for sym in wl:
        closes, times, pc, src = fetch_day(ds, sym, date)
        if closes is None:
            per_sym[sym] = {'error': src}
            continue
        pushes = [r for r in today_push if r['sym'] == sym]
        trips, orphans = pair_trips(sym, pushes, closes, times, use_push_price=(src == 'mootdx'))
        # 每单最大有利波动（出场规则失效判据）
        for t in trips:
            i0 = t['entry_time']; seg = closes
            try:
                ei = time_to_idx(times, t['entry_time'] + ':00')
                xi = time_to_idx(times, (t['exit_time'] + ':00') if 'EOD' not in t['exit_time'] else None)
                xi = xi if xi >= 0 else len(closes) - 1
                fwd = closes[ei + 1:xi + 1] if 0 <= ei < xi else []
                if fwd:
                    fav = ((max(fwd) - t['entry_price']) / t['entry_price'] * 100) if t['dir'] == '正T' \
                        else ((t['entry_price'] - min(fwd)) / t['entry_price'] * 100)
                    t['max_fav_pct'] = round(fav, 3)
                else:
                    t['max_fav_pct'] = None
            except Exception:
                t['max_fav_pct'] = None
            t['loss_tags'] = [] if t['valid'] else attribute_loss(t, t.get('max_fav_pct'))
            t['name'] = wl[sym]
        trips_all.extend(trips)
        orphans_all.extend([{'sym': sym, **o} for o in orphans])
        per_sym[sym] = {'n_push': len(pushes), 'n_trips': len(trips), 'data_src': src}
        vol[sym] = analyze_capture(sym, closes, times, pushes, trips)
        vol[sym]['name'] = wl[sym]
        vol[sym]['data_src'] = src

    n_valid = sum(1 for t in trips_all if t['valid'])
    summary = {
        'n_pushes': len(today_push), 'n_trips': len(trips_all),
        'n_valid': n_valid, 'valid_rate_pct': round(n_valid / len(trips_all) * 100, 1) if trips_all else None,
        'net_sum_pct': round(sum(t['net_ret_pct'] for t in trips_all), 3),
        'gross_sum_pct': round(sum(t['gross_ret_pct'] for t in trips_all), 3),
        'avg_net_pct': round(sum(t['net_ret_pct'] for t in trips_all) / len(trips_all), 3) if trips_all else None,
        'n_loss': len(trips_all) - n_valid, 'orphans': orphans_all,
    }

    # 近5交易日基线（实盘推送 round-trip 口径；历史数据 F盘兜底）
    baseline_days = []
    prev = R.prev_trading_days(datetime.date.fromisoformat(date), 5) if hasattr(R, 'prev_trading_days') else []
    for d in prev:
        dpush = [r for r in audit_all if str(r.get('ts', '')).startswith(d) and r.get('ok') and r.get('sym') in wl]
        dpush.sort(key=lambda r: r['ts'])
        dtrips = []
        for sym in wl:
            sp = [r for r in dpush if r['sym'] == sym]
            if not sp:
                continue
            closes, times, pc, src = fetch_day(ds, sym, d)
            if closes is None:
                continue
            tt, _ = pair_trips(sym, sp, closes, times, use_push_price=(src == 'mootdx'))
            dtrips.extend(tt)
        nv = sum(1 for t in dtrips if t['valid'])
        baseline_days.append({
            'date': d, 'n_pushes': len(dpush), 'n_trips': len(dtrips),
            'valid_rate_pct': round(nv / len(dtrips) * 100, 1) if dtrips else None,
            'net_sum_pct': round(sum(t['net_ret_pct'] for t in dtrips), 3),
        })
    bd = [d for d in baseline_days if d['n_trips'] > 0]
    total_trips_bl = sum(d['n_trips'] for d in bd)
    baseline = {
        'days': baseline_days,
        'sample_enough': total_trips_bl >= 10,
        'note': None if total_trips_bl >= 10 else f'样本不足（近5日实盘配对仅 {total_trips_bl} 笔 <10）：push_audit 自 07-23 才有完整记录，基线随交易日累积',
        'mean': {
            'n_pushes': round(sum(d['n_pushes'] for d in bd) / len(bd), 1) if bd else None,
            'valid_rate_pct': round(sum(d['valid_rate_pct'] for d in bd if d['valid_rate_pct'] is not None) / max(len([d for d in bd if d['valid_rate_pct'] is not None]), 1), 1) if bd else None,
            'net_sum_pct': round(sum(d['net_sum_pct'] for d in bd) / len(bd), 3) if bd else None,
        },
    }

    out = {
        'date': date, 'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'cost_model': '佣金万1+印花(仅股票万5.641卖边)+滑点2bps/边；有效=净盈亏>0',
        'trips': trips_all, 'summary': summary, 'per_sym': per_sym,
        'baseline': baseline, 'volatility': vol,
        'opportunities': build_opportunities(vol, per_sym),
        'syms_with_push': syms_with_push,
    }
    path = os.path.join(OUT, f'live_review_{date}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'[ok] {path}')
    print(f"  推送 {summary['n_pushes']} → 配对 {summary['n_trips']}（有效 {n_valid}，净盈亏合计 {summary['net_sum_pct']}%)")
    for sym, v in vol.items():
        print(f"  {sym}: 有效段{v['n_valid_segs']} 幅度{v['total_amp_pct']}% 捕获{v['captured_pct']}% 率{v['capture_rate_pct']}%")


if __name__ == '__main__':
    main()
