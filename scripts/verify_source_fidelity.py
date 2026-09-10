# -*- coding: utf-8 -*-
"""scripts/verify_source_fidelity.py — 数据源口径保真核对（P3）

用途：用**第三方独立源**的真实 1m OHLCV 去核对 monitor 当天实际使用的行情口径，
量化「兜底/降级」对 ATR 与止损宽度的影响，并把当日实盘信号清单并列出来供人工判断。

为什么需要它：2026-09-10 mootdx 接口级失效 → 全天走兜底；兜底数据（腾讯分时合成 OHLC）
实测 ATR 中位低估 41.8%（448 标的-日）。降级本身不会中断交易，但会让口径悄悄漂移。

⚠️ 边界（硬）：tdx-connector / westock-mcp 是**远端 HTTP MCP，只能在 agent 会话里被调用**，
   本脚本**不 import 任何 MCP 客户端**。用法是「agent 调 tdx_kline(period=7) 把 Rows 落 JSON
   → 本脚本读该 JSON 做核对」。禁止把 MCP 接进 monitor 生产路径。

用法：
  # ① agent 侧先取数（示例，在实际会话里调用工具）
  #    tdx_kline(code="300010", setcode="0", period="7", wantNum="250")
  #    把返回的 Rows 存成 JSON 数组 → output/tdx_1m_300010_20260910.json
  # ② 再跑本脚本
  python scripts/verify_source_fidelity.py --sym 300010.SZ --date 2026-09-10 \
      --bars-json output/tdx_1m_300010_20260910.json
  # ③ 仅看 monitor 当日用了哪个源（不核对）：
  python scripts/verify_source_fidelity.py --sym 300010.SZ --date 2026-09-10 --report-only
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import numpy as np                      # noqa: E402
import pandas as pd                     # noqa: E402
from primitives import compute_atr      # noqa: E402

AUDIT = os.path.join(ROOT, 'data', 'push_audit.jsonl')
SIGNAL_TXT = os.path.join(ROOT, 'data', 'signal.txt')
OUT_DIR = os.path.join(ROOT, 'output')


def synth_from_real(d):
    """复刻腾讯分时合成口径（datasource._tencent_intraday_fallback._fetch_host 409-413）。"""
    c = d['close'].values.astype(float)
    n = len(c)
    o = np.empty(n); h = np.empty(n); lo = np.empty(n)
    o[0] = c[0]; h[0] = c[0]; lo[0] = c[0]
    if n > 1:
        o[1:] = c[:-1]
        h[1:] = np.maximum(c[:-1], c[1:])
        lo[1:] = np.minimum(c[:-1], c[1:])
    out = d.copy(); out['open'] = o; out['high'] = h; out['low'] = lo
    return out


def load_connector_rows(path, date):
    """读 agent 从 tdx_kline 导出的 Rows（Second=当日秒偏移，如 41340 → 11:29）。"""
    raw = json.load(io.open(path, encoding='utf-8'))
    if isinstance(raw, dict):
        raw = raw.get('Rows') or raw.get('rows') or []
    rows = []
    for r in raw:
        try:
            sec = int(r.get('Second') or r.get('second'))
            tt = datetime.strptime(str(r.get('Data') or date).replace('-', ''), '%Y%m%d') \
                + pd.Timedelta(seconds=sec)
            rows.append({'trade_time': tt,
                         'open': float(r['Open']), 'high': float(r['High']),
                         'low': float(r['Low']), 'close': float(r['Close']),
                         'volume': float(r.get('Volume') or r.get('RawVolume') or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values('trade_time').reset_index(drop=True)
    df['trade_date'] = df['trade_time'].dt.strftime('%Y-%m-%d')
    return df[df['trade_date'] == date].reset_index(drop=True)


def atr_block(df, label):
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    atr = compute_atr(h, l, c)[-1]
    px = c[-1]
    return {'label': label, 'bars': len(df),
            'first': str(df['trade_time'].iloc[0])[11:16],
            'last': str(df['trade_time'].iloc[-1])[11:16],
            'ampl_mean_pct': round(float(((h - l) / c).mean() * 100), 4),
            'atr14': round(float(atr), 5),
            'atr14_pct': round(float(atr / px * 100), 3),
            'stop15_pct': round(float(1.5 * atr / px * 100), 3),
            'vol_sum': float(df['volume'].sum())}


def day_signals(date):
    """当日 push_audit.jsonl 实盘推送清单（口径=当时 monitor 实际用的数据源）。"""
    out = []
    if not os.path.exists(AUDIT):
        return out
    for ln in io.open(AUDIT, encoding='utf-8'):
        ln = ln.strip()
        if not ln or not ln.startswith('{'):
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if str(r.get('ts', '')).startswith(date) and r.get('ok'):
            out.append({'ts': r['ts'], 'sym': r.get('sym'), 'type': r.get('type'),
                        'price': r.get('price')})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sym', required=True)
    ap.add_argument('--date', required=True)
    ap.add_argument('--bars-json', default=None,
                    help='agent 从 tdx_kline(period=7) 导出的 Rows JSON 路径')
    ap.add_argument('--report-only', action='store_true')
    ap.add_argument('--min-bars', type=int, default=20,
                    help='连接器基准最少根数（默认20；短时段冒烟可下调，ATR14 需≥15 根才稳定）')
    args = ap.parse_args()

    print(f'=== 数据源口径保真核对 {args.sym} {args.date} ===')

    conn = None
    if args.bars_json:
        conn = load_connector_rows(args.bars_json, args.date)
        if conn is None or len(conn) < args.min_bars:
            print(f'❌ 连接器基准数据不足（{0 if conn is None else len(conn)} 根'
                  f' < {args.min_bars}），无法核对')
            return 2
    elif not args.report_only:
        print('⚠️ 未提供 --bars-json（agent 需先调 tdx_kline period=7 导出 Rows），'
              '仅输出实盘清单。')

    sigs = day_signals(args.date)
    print(f'\n当日实盘成功推送 {len(sigs)} 条：')
    from collections import Counter
    print('  ' + str(dict(Counter(s['type'] for s in sigs))))
    for s in sigs[:40]:
        print(f"   {s['ts'][11:]}  {s['type']:<2} @{s['price']}")

    result = {'sym': args.sym, 'date': args.date, 'generated_at': datetime.now().isoformat(),
              'signals': sigs}

    if conn is not None:
        print(f'\n=== 第三方（tdx 连接器真实 1m）vs 合成口径对照 ===')
        blocks = [atr_block(conn, '连接器真实OHLC')]
        syn = synth_from_real(conn)
        blocks.append(atr_block(syn, '（对照）合成口径'))
        print('%-18s %5s %6s %6s %9s %9s %10s' % (
            '口径', '根数', '首', '末', '振幅均值%', 'ATR%', '1.5ATR止损%'))
        for b in blocks:
            print('%-18s %5d %6s %6s %9.4f %9.3f %10.3f' % (
                b['label'], b['bars'], b['first'], b['last'],
                b['ampl_mean_pct'], b['atr14_pct'], b['stop15_pct']))
        r = blocks[1]['atr14'] / blocks[0]['atr14']
        print(f'\n>>> 合成/真实 ATR 比值 = {r:.3f}（合成低估 {(1 - r) * 100:.1f}%）')
        print('>>> 参考：全历史 448 标的-日 中位 0.582；今日实测 0.584 / F盘09-09 0.733')
        result['atr_compare'] = {'connector': blocks[0], 'synthetic': blocks[1],
                                 'ratio_syn_over_real': round(r, 4)}

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"source_fidelity_{args.sym.split('.')[0]}_{args.date}.json")
    json.dump(result, io.open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n已落盘 {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
