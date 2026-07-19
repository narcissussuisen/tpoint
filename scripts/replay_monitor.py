#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_monitor.py — 模拟 tpoint 上线监控，用历史 1m CSV 回放完整做 T 闭环到飞书。

复用 core/monitor.py 的真实 detect_for / emit / push_batch / EXIT_CFG / STATE，
不改 core 任何代码。绕开 run() 的 today/时段/sleep 障碍。

用法:
  python scripts/replay_monitor.py                           # 自动挑信号日 + 真推飞书
  python scripts/replay_monitor.py --sym 300975 --day 2026-07-10
  python scripts/replay_monitor.py --dry-run                  # 只打印不推飞书
  python scripts/replay_monitor.py --webhook https://...      # 覆盖 webhook
"""
import os, sys, time, argparse
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tpoint root
sys.path.insert(0, os.path.join(BASE, 'core'))   # 让 import monitor 可解析
sys.path.insert(0, BASE)

import monitor as M   # 模块级执行 load_targets()/EXIT_CFG；tf=None 懒加载，import 安全

DATA_1M = os.path.join(BASE, 'backtest', 'keyfactor_data', '1m')
LOG_DIR = os.path.join(BASE, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# 持仓股候选（长飞光纤 601869 缺 CSV，已排除）
HELD = ['300975.SZ', '603938.SH', '300395.SZ', '301526.SZ']

# 本地名称覆盖（不在 live 持仓文件 TARGETS 内、仅模拟推送用的标的）
NAME_OVERRIDE = {'161129.SZ': '原油LOF易方达'}

def sym_name(sym):
    return NAME_OVERRIDE.get(sym, M.TARGETS.get(sym, sym))


def load_day(sym, day):
    """读 sym 的 1m CSV，返回 (day_df, pc, warm) 或 None。"""
    csv = os.path.join(DATA_1M, f'{sym}_1m.csv')
    if not os.path.exists(csv):
        return None
    df = pd.read_csv(csv, dtype={'symbol': str})
    df['trade_time'] = pd.to_datetime(df['trade_time'])
    df = df.sort_values('trade_time').reset_index(drop=True)
    all_dates = sorted(df['trade_date'].unique())
    if day not in all_dates:
        return None
    day_df = df[df['trade_date'] == day].sort_values('trade_time').reset_index(drop=True)
    di = all_dates.index(day)
    if di > 0:
        prev_day = all_dates[di - 1]
        pc = float(df[df['trade_date'] == prev_day]['close'].iloc[-1])
    else:
        pc = float(day_df['close'].iloc[0])
    prior_closes = [float(df[df['trade_date'] == d]['close'].iloc[-1])
                    for d in all_dates[max(0, di - 30):di]]
    warm = np.array(prior_closes, dtype=float)
    return day_df, pc, warm


def build_data(day_df, pc):
    """复刻 monitor.compute() L226-235，去掉 tf/today 检查。"""
    o = day_df['open'].values.astype(float)
    h = day_df['high'].values.astype(float)
    lo = day_df['low'].values.astype(float)
    c = day_df['close'].values.astype(float)
    v = np.clip(day_df['volume'].values.astype(float), 0, None)  # 防 volume 噪声
    has_vol = float(np.sum(v)) > 0
    data = M.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=has_vol)
    data['df'] = day_df
    data['n'] = len(day_df)
    return data


def list_days(sym):
    csv = os.path.join(DATA_1M, f'{sym}_1m.csv')
    if not os.path.exists(csv):
        return []
    df = pd.read_csv(csv, usecols=['trade_date'])
    return sorted(df['trade_date'].unique())


def dry_run_full(sym, day):
    """fresh st 跑一次全天 detect_for，返回信号列表（不推）。"""
    res = load_day(sym, day)
    if res is None:
        return None
    day_df, pc, warm = res
    data = build_data(day_df, pc)
    M.STATE[sym] = {'PC': pc, 'WARM': warm}
    st = {}
    sigs = M.detect_for(sym, sym_name(sym), data, st)
    return sigs, day_df, pc, warm, data


def auto_pick():
    """挑有 B+X 闭环的 (sym, day)。优先级: ①B+TRAIL ②B+S/任意X ③仅B ④仅S。"""
    cands = []
    for sym in HELD:
        for day in list_days(sym):
            try:
                r = dry_run_full(sym, day)
                if r is None:
                    continue
                sigs = r[0]
            except Exception:
                continue
            types = [s[0] for s in sigs]
            has_b = 'B' in types
            has_trail = any(s[0] == 'X' and s[10] == 'TRAIL' for s in sigs)
            has_x_s = any(s[0] == 'X' and s[10] == 'S' for s in sigs)
            has_x = 'X' in types
            if has_b and has_trail:
                pri = 1
            elif has_b and (has_x_s or has_x):
                pri = 2
            elif has_b:
                pri = 3
            elif 'S' in types:
                pri = 4
            else:
                continue
            cands.append((pri, sym, day, types, len(sigs)))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0]  # (pri, sym, day, types, n)


def replay(sym, day, dry_run=False, webhook=None, window=30, gap=1.5):
    r = dry_run_full(sym, day)
    if r is None:
        print(f'❌ {sym} {day} 无数据')
        return False
    sigs_preview, day_df, pc, warm, data = r
    name = sym_name(sym)
    full_n = len(day_df)

    if webhook:
        M.WEBHOOK_URL = webhook
    log_path = os.path.join(LOG_DIR, f'replay_{sym.replace(".", "_")}_{day}.log')
    logf = open(log_path, 'w', encoding='utf-8')

    def log(msg):
        print(msg)
        logf.write(msg + '\n'); logf.flush()

    log(f'=== 模拟回放 {name} {sym} {day} ===')
    log(f'PC={pc:.2f}  bars={full_n}  webhook={"DRY-RUN(不推)" if dry_run else M.WEBHOOK_URL[:50]}')
    log(f'全天信号预览: {len(sigs_preview)}条  types={[s[0] for s in sigs_preview]}')
    log(f'出场原因: {[s[10] for s in sigs_preview if s[0]=="X"] or "无"}')

    # 重置：fresh st，分轮增量跑（与真实 30s 扫描增量逻辑一致）
    st = {}
    M.STATE[sym] = {'PC': pc, 'WARM': warm}
    all_emitted = []
    scan = 0
    push_ok = 0
    push_fail = 0

    for end in range(window, full_n + window, window):
        scan += 1
        end = min(end, full_n)
        data['n'] = end
        bar_time = str(day_df['trade_time'].iloc[end - 1])[11:16] if end <= full_n else ''
        sigs = M.detect_for(sym, name, data, st)
        if not sigs:
            log(f'[scan {scan}] bar~{end}({bar_time}) 无信号')
            continue
        msgs = []
        for s in sigs:
            msg = M.emit_signal(s, sym=sym, sim=True)   # 卡片内 footer 已加 [SIM] 前缀
            msgs.append(msg)
            all_emitted.append(s)
            sig_time = s[12][11:16] if len(s) > 12 and s[12] and len(s[12]) >= 16 else bar_time
            log(f'[scan {scan}] {s[0]} @{sig_time} price={s[1]:.2f} chg={s[2]:+.2f}% '
                f'level={s[4]} reason={s[10]}')
        if dry_run:
            log(f'[scan {scan}] DRY-RUN 不推送，{len(msgs)}条预览:')
            import json as _json
            for m in msgs:
                if isinstance(m, dict):
                    log('  ----\n' + _json.dumps(m, ensure_ascii=False, indent=1))
                else:
                    log('  ----\n' + str(m))
        else:
            ok = M.push_batch(msgs, sim=True)
            if ok:
                push_ok += 1
            else:
                push_fail += 1
            log(f'[scan {scan}] PUSH {len(msgs)}条 ok={ok}')
        if gap > 0:
            time.sleep(gap)

    log(f'=== 回放结束: {scan}轮, 发出{len(all_emitted)}条信号, 推送成功{push_ok}失败{push_fail} ===')
    log('--- 信号汇总 ---')
    for s in all_emitted:
        sig_time = s[12][11:16] if len(s) > 12 and s[12] and len(s[12]) >= 16 else '?'
        log(f'  {s[0]:1} @{sig_time} price={s[1]:.2f} chg={s[2]:+.2f}% level={s[4]} reason={s[10]}')
    logf.close()
    print(f'\n日志: {log_path}')
    return True


def main():
    ap = argparse.ArgumentParser(description='模拟 tpoint 上线监控，飞书走一遍做T流程')
    ap.add_argument('--sym', default=None, help='股票代码(如 300975.SZ)，缺省自动挑')
    ap.add_argument('--day', default=None, help='日期 YYYY-MM-DD，缺省自动挑')
    ap.add_argument('--dry-run', action='store_true', help='只打印信号文本不推飞书')
    ap.add_argument('--webhook', default=None, help='覆盖飞书 webhook URL')
    ap.add_argument('--list', action='store_true', help='列出候选(sym,day)信号统计后退出')
    args = ap.parse_args()

    if args.list:
        print('候选(sym,day)信号扫描:')
        for sym in HELD:
            for day in list_days(sym):
                try:
                    r = dry_run_full(sym, day)
                    if r is None:
                        continue
                    sigs = r[0]
                    if not sigs:
                        continue
                    types = [s[0] for s in sigs]
                    print(f'  {sym} {day}  {len(sigs)}条 types={types}')
                except Exception as e:
                    print(f'  {sym} {day}  ERR {e}')
        return

    if args.sym and args.day:
        sym, day = args.sym, args.day
        print(f'指定回放: {sym} {day}')
    else:
        print('自动挑选有 B+X 闭环的(sym, day)...')
        pick = auto_pick()
        if pick is None:
            print('❌ 4只持仓股所有日均无信号，无法回放')
            return
        pri, sym, day, types, n = pick
        print(f'选定: {sym} {day}  优先级={pri}  信号{n}条 types={types}')

    replay(sym, day, dry_run=args.dry_run, webhook=args.webhook)


if __name__ == '__main__':
    main()
