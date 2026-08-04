# -*- coding: utf-8 -*-
"""
mtf_direction_lab.py — 多周期方向标注研究脚本（P1-1 迭代交付）

对 watchlist（或任意标的）计算 1m/5m/1h 三级大方向快照，输出 JSON。
用途：监控参考（"顺周期做T vs 逆周期抓反弹"），不融合进 1m 信号触发。

用法：
  python scripts/mtf_direction_lab.py --today        # 当日 1m 数据（盘中/盘后）
  python scripts/mtf_direction_lab.py --date 2026-07-31
  python scripts/mtf_direction_lab.py --sym 688111.SH --date 2026-07-31
输出：output/mtf_direction_{date}.json
"""
import argparse
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'core'))

CST = datetime.timezone(datetime.timedelta(hours=8))


def load_watchlist():
    with open(os.path.join(BASE, 'data', 'watchlist.json'), encoding='utf-8') as f:
        return json.load(f)


def load_tick_cache_1m(sym, max_days=15):
    """[轮次2-4 迭代] 从 data/tick_cache/{sym}_{yyyymmdd}.csv 加载历史 tick 数据并聚合成 1m K线。

    tick_cache 是逐笔成交 CSV（time,price,vol,buyorsell,volume,date），
    覆盖 04 月至 07-24（161129/513310/688347 有，688111/588000 无）。
    聚合规则：按 (date, HH:MM) 分组 → open=首笔价, close=末笔价, high=max, low=min, volume=Σvol。
    返回 DataFrame[trade_time, trade_date, open, close, high, low, volume] 按时间排序；
    缓存不足 max_days 个交易日 → 返回 None（调用方再决定降级 mootdx）。
    """
    import glob
    import pandas as pd
    cache_dir = os.path.join(BASE, 'data', 'tick_cache')
    if not os.path.isdir(cache_dir):
        return None
    files = sorted(glob.glob(os.path.join(cache_dir, f'{sym}_2*.csv')))
    if not files:
        return None
    # 只取最近 max_days 个文件（各为一天）
    files = files[-max_days:]
    frames = []
    for fp in files:
        try:
            tdf = pd.read_csv(fp, dtype={'date': str})
        except Exception:
            continue
        if tdf.empty or 'time' not in tdf.columns or 'price' not in tdf.columns:
            continue
        tdf['trade_date'] = tdf['date'].astype(str)
        tdf['hhmm'] = tdf['time'].astype(str).str[:5]  # 'HH:MM'
        g = tdf.groupby(['trade_date', 'hhmm']).agg(
            open=('price', 'first'), close=('price', 'last'),
            high=('price', 'max'), low=('price', 'min'),
            volume=('volume', 'sum') if 'volume' in tdf.columns else ('vol', 'sum'),
        ).reset_index()
        g['trade_time'] = pd.to_datetime(g['trade_date'] + ' ' + g['hhmm'] + ':00', format='%Y%m%d %H:%M:%S')
        g['trade_date'] = pd.to_datetime(g['trade_date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
        frames.append(g[['trade_time', 'trade_date', 'open', 'close', 'high', 'low', 'volume']])
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).sort_values('trade_time').reset_index(drop=True)
    n_days = df['trade_date'].nunique()
    if n_days < 2:
        return None  # 不足 2 天（缓存断档）
    df.attrs['n_cache_days'] = n_days
    return df


def main():
    ap = argparse.ArgumentParser(description='多周期方向标注（1m/5m/1h）')
    ap.add_argument('--sym', help='单标的代码（默认全部 watchlist）')
    ap.add_argument('--date', default=datetime.datetime.now(CST).strftime('%Y-%m-%d'),
                    help='数据日期（默认今天）')
    ap.add_argument('--days', type=int, default=1,
                    help='回溯天数（1h 方向需约 10 个交易日，默认 1=仅当日，1h 显示数据不足）')
    args = ap.parse_args()

    from core.datasource import MootdxDataSource
    from core import miji_alpha as ma
    import pandas as pd

    syms = [args.sym] if args.sym else list(load_watchlist().keys())
    ds = MootdxDataSource()
    out = {'date': args.date, 'days': args.days,
           'generated_at': datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
           'note': '1m/5m/1h 方向为 1m 序列聚合近似，仅供人工参考，不参与 1m 信号触发；1h 方向需 ≥10 交易日数据',
           'symbols': {}}

    for sym in syms:
        try:
            # [轮次2-4 迭代] 优先历史 CSV 缓存（多日跨度，支持 1h 方向）；
            # 缓存缺失/不足时降级 mootdx（免费源 1m 仅回溯 ~4 交易日 → 1h 数据不足）。
            df = None
            src = ''
            if args.days > 1:
                df = load_tick_cache_1m(sym, max_days=args.days)
                if df is not None:
                    src = f'tick_cache({df.attrs.get("n_cache_days", "?")}日)'
            if df is None:
                # 多日 1m：直接用 mootdx client.bars 拉 offset 根（historical_1m 只返回单日）
                from core.datasource import _to_mootdx_sym
                code, _ = _to_mootdx_sym(sym)
                offset = args.days * 260 + 300
                raw = ds.client.bars(symbol=code, frequency=8, offset=offset)
                if raw is None or len(raw) < 20:
                    out['symbols'][sym] = {'error': '数据不足(<20根1m)'}
                    continue
                df = raw.copy()
                dt = pd.to_datetime(df['datetime'], errors='coerce')
                bad = dt.isna()
                if bad.any():
                    df = df[~bad].copy(); dt = dt[~bad]
                df['trade_time'] = dt
                df['trade_date'] = dt.dt.strftime('%Y-%m-%d')
                src = 'mootdx'
            df = df.sort_values('trade_time').reset_index(drop=True)
            c = df['close'].values.astype(float)
            snap = ma.mtf_direction_snapshot(c)
            # 当日涨跌用当日首末
            today_df = df[df['trade_date'] == args.date]
            if len(today_df):
                c_today = today_df['close'].values.astype(float)
                day_chg = (c_today[-1] / c_today[0] - 1) * 100 if c_today[0] else 0.0
            else:
                day_chg = 0.0
            # 语义化
            def label(v, n):
                if n < 40 and v == 0:
                    return '数据不足'
                return {1: '多头↑', -1: '空头↓', 0: '震荡→'}.get(v, '—')
            out['symbols'][sym] = {
                '1m': snap['1m'], '5m': snap['5m'], '1h': snap['1h'],
                'label': (f"1m {label(snap['1m'], 99)} / 5m {label(snap['5m'], snap['n_5m_agg'])}"
                          f" / 1h {label(snap['1h'], snap['n_1h_agg'])}"),
                'day_chg_pct': round(day_chg, 2),
                'n_1m_total': len(c), 'n_5m_agg': snap['n_5m_agg'], 'n_1h_agg': snap['n_1h_agg'],
                'span_days': len(set(df['trade_date'])) if 'trade_date' in df.columns else args.days,
                'source': src,
            }
            print(f"  {sym}: {out['symbols'][sym]['label']}  (日涨跌 {day_chg:+.2f}%, 跨度{out['symbols'][sym]['span_days']}日, 源={src})")
        except Exception as e:
            out['symbols'][sym] = {'error': str(e)[:120]}
            print(f"  ⚠️ {sym}: {e}")

    op = os.path.join(BASE, 'output', f'mtf_direction_{args.date}.json')
    with open(op, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'✅ 方向快照已写入 {op}')


if __name__ == '__main__':
    main()
