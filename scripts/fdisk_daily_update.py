#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""fdisk_daily_update.py — F盘 1m 数据每日增量更新器（R0 基建 · 自迭代优化计划）

解决问题：F:\keyfactor_data\1m\ tickflow 历史库无每日增量机制（08-03 盘点滞后 1 周），
导致「生产 vs 回测」对账在近一周无 F盘数据可用（G2 无法度量）。

机制：交易日收盘后（建议 15:10 跑，当日 1m 完整）从 mootdx 拉当日 1m，
转成 tickflow 格式（symbol/name/timestamp/trade_date/trade_time/OHLC/volume/amount），
幂等追加到 F:\\keyfactor_data\\1m\\<sym>_1m.csv（按 timestamp 去重，只追加更新的行）。

CLI：
  python scripts/fdisk_daily_update.py                      # 今日 × watchlist 5 只
  python scripts/fdisk_daily_update.py --date 2026-07-31    # 指定日（mootdx 3-4天上限内）
  python scripts/fdisk_daily_update.py --syms 161129.SZ,688111.SH
  python scripts/fdisk_daily_update.py --dry-run            # 只预览不写入

注意：mootdx 历史 1m 上限 3-4 天 → 本更新器必须每个交易日都跑（计划任务），
漏跑的日子无法事后回补（需 tickflow 官方源另行补）。
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, ROOT)

import pandas as pd

from datasource import MootdxDataSource

F_DATA = r'F:\keyfactor_data\1m'
WATCHLIST = os.path.join(ROOT, 'data', 'watchlist.json')

# 与 daily_signal_review 一致的 2026 节假日表（非交易日 skip）
HOLIDAYS_2026 = {
    '2026-01-01','2026-01-02','2026-01-26','2026-01-27','2026-01-28','2026-01-29','2026-01-30',
    '2026-02-02','2026-02-03','2026-04-06','2026-05-01','2026-05-04','2026-05-05',
    '2026-06-19','2026-06-22','2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07',
    '2026-12-25',
}

TICKFLOW_COLS = ['symbol', 'name', 'timestamp', 'trade_date', 'trade_time',
                 'open', 'high', 'low', 'close', 'volume', 'amount']


def is_trading_day(date_str):
    d = datetime.date.fromisoformat(date_str)
    return d.weekday() < 5 and date_str not in HOLIDAYS_2026


def to_tickflow(df, sym, name):
    """mootdx historical_1m df → tickflow 格式 DataFrame。
    ⚠️ timestamp 用 total_seconds() 显式转秒：pandas 3.0 datetime 默认分辨率为秒
    （非纳秒），astype('int64')//10**9 会再除10亿得到错误值（1785510 教训）。"""
    out = pd.DataFrame()
    tt = pd.to_datetime(df['trade_time'])
    epoch = pd.Timestamp('1970-01-01')
    out['timestamp'] = (tt - epoch).dt.total_seconds().astype('int64')
    out['symbol'] = sym        # 须在行索引建立后赋值（空DF赋标量不产生行，会NaN填充）
    out['name'] = name
    out['trade_date'] = tt.dt.strftime('%Y-%m-%d')
    out['trade_time'] = tt.dt.strftime('%Y-%m-%d %H:%M:%S')
    for col in ('open', 'high', 'low', 'close'):
        out[col] = df[col].astype(float) if col in df.columns else 0.0
    out['volume'] = df['volume'].astype(float) if 'volume' in df.columns else 0.0
    if 'amount' in df.columns:
        out['amount'] = df['amount'].astype(float)
    else:
        out['amount'] = out['close'] * out['volume']   # 近似（mootdx 缺 amount 时）
    return out[TICKFLOW_COLS]


def append_symbol(ds, sym, name, date, dry_run=False):
    """单标的：拉当日 → 幂等追加 F盘 csv。返回 dict 结果。"""
    csv_path = os.path.join(F_DATA, f'{sym}_1m.csv')
    res = {'sym': sym, 'date': date, 'csv': csv_path}

    # ---- 拉取当日 ----
    try:
        df = ds.historical_1m(sym, date)
    except Exception as e:
        res.update(ok=False, error=f'fetch_fail: {e}')
        return res
    if df is None or len(df) < 5:
        res.update(ok=False, error='empty_or_too_few_bars')
        return res
    new = to_tickflow(df, sym, name)
    res['fetched_bars'] = len(new)

    # ---- 幂等：只追加比现有最大 timestamp 更新的行 ----
    if os.path.exists(csv_path):
        try:
            old_ts = pd.read_csv(csv_path, usecols=['timestamp'])['timestamp']
            # ⚠️ 兼容历史毫秒级时间戳（>1e12 视为毫秒，归一化为秒再比较；
            # 300058/600570/688111 曾混入毫秒行 1784185200000 致 max_ts 虚高误判）
            old_ts = old_ts.where(old_ts < 1e12, old_ts // 1000)
            max_ts = int(old_ts.max()) if len(old_ts) else 0
        except Exception:
            max_ts = 0
    else:
        max_ts = 0
    add = new[new['timestamp'] > max_ts]
    res['existing_max_ts'] = max_ts
    res['new_bars'] = len(add)
    if len(add) == 0:
        res.update(ok=True, skipped=True, note='already_up_to_date')
        return res

    # 数据量合理性校验：全日约 240 根（09:30-11:30 + 13:00-15:00）
    day_rows = new[new['trade_date'] == date]
    res['day_total_bars'] = len(day_rows)
    res['warn'] = 'bars_incomplete' if len(day_rows) < 200 else ''

    if dry_run:
        res.update(ok=True, dry_run=True)
        return res

    header = not os.path.exists(csv_path)
    add.to_csv(csv_path, mode='a', header=header, index=False, encoding='utf-8')
    res.update(ok=True, written=len(add))
    return res


def main():
    ap = argparse.ArgumentParser(description='F盘 1m 每日增量更新器（R0）')
    ap.add_argument('--date', default=datetime.datetime.now().strftime('%Y-%m-%d'))
    ap.add_argument('--syms', default='', help='逗号分隔；默认 watchlist')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not is_trading_day(args.date):
        print(f'[fdisk_update] {args.date} 非交易日，skip')
        return

    watch = json.load(open(WATCHLIST, encoding='utf-8'))
    syms = [s for s in (args.syms.split(',') if args.syms else list(watch.keys())) if s]

    ds = MootdxDataSource()
    results = []
    for sym in syms:
        name = watch.get(sym, sym)
        r = append_symbol(ds, sym, name, args.date, dry_run=args.dry_run)
        results.append(r)
        status = ('OK' if r.get('ok') else 'FAIL')
        extra = (f"new={r.get('new_bars')} fetched={r.get('fetched_bars')} {r.get('warn','')}"
                 if r.get('ok') else r.get('error', ''))
        print(f'  [{status}] {sym} {extra}')

    n_ok = sum(1 for r in results if r.get('ok') and not r.get('skipped'))
    n_skip = sum(1 for r in results if r.get('skipped'))
    n_fail = sum(1 for r in results if not r.get('ok'))
    print(f'[fdisk_update {args.date}] ok={n_ok} skip={n_skip} fail={n_fail} / total={len(results)}')
    if n_fail:
        sys.exit(2)


if __name__ == '__main__':
    main()
