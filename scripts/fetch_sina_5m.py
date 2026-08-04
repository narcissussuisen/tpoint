# -*- coding: utf-8 -*-
"""
fetch_sina_5m.py — 新浪 5m K线落地（免费源，供回测筛选器初筛）

背景：tickflow 分钟K权限已失效(PermissionError)，mootdx 免费源 1m 历史仅 3-4 天，
      新浪 5m K线可拉 1000 根(~21 交易日)，免费。5m 粒度对做T偏粗，但足以先验证
      回测框架 + 对候选池做初筛（1m 上更不可能优于 5m 结果太多）。

用法：
  python scripts/fetch_sina_5m.py [--syms 688146.SH,600206.SH] [--all-candidates]
  --all-candidates  拉取候选池(7只) + watchlist(4只)
输出：data/sina_5m/{sym}_5m.csv
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, 'data', 'sina_5m')
os.makedirs(DATA_DIR, exist_ok=True)

# 候选池 7 只 + watchlist 5 只（名称）
DEFAULT_SYMS = [
    ('688146.SH', '中船特气'), ('600206.SH', '有研新材'), ('688048.SH', '长光华芯'),
    ('688008.SH', '澜起科技'), ('688347.SH', '华虹宏力'), ('600584.SH', '长电科技'),
    ('688766.SH', '普冉股份'),
    ('161129.SZ', '原油LOF易方达'), ('513310.SH', '中韩半导体ETF'),
    ('300058.SZ', '蓝色光标'), ('600570.SH', '恒生电子'), ('688111.SH', '金山办公'),
]

_SINA_URL = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/'
             'CN_MarketDataService.getKLineData?symbol={tcode}&scale=5&ma=no&datalen=1000')


def to_tcode(sym):
    code, mkt = sym.split('.')
    return ('sh' if mkt == 'SH' else 'sz') + code


def fetch_5m(sym):
    """拉取新浪 5m K线，返回 list[dict] 或 None。"""
    url = _SINA_URL.format(tcode=to_tcode(sym))
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read().decode('utf-8')
    except Exception as e:
        print(f'  ⚠️ {sym} 拉取失败: {e}')
        return None
    # JSONP 剥壳: /*...*/ var _=([...])
    m = re.search(r'\((\[.*\])\)', data, re.S)
    if not m:
        print(f'  ⚠️ {sym} 响应无 JSON 数组')
        return None
    try:
        arr = json.loads(m.group(1))
    except Exception as e:
        print(f'  ⚠️ {sym} JSON 解析失败: {e}')
        return None
    if not arr:
        return None
    out = []
    for r in arr:
        out.append({
            'symbol': sym,
            'name': '',
            'trade_time': r['day'],
            'open': float(r['open']),
            'high': float(r['high']),
            'low': float(r['low']),
            'close': float(r['close']),
            'volume': float(r.get('volume', 0)),
            'amount': float(r.get('amount', 0)),
        })
    return out


def save_csv(sym, rows):
    import csv as _csv
    path = os.path.join(DATA_DIR, f'{sym}_5m.csv')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = _csv.DictWriter(f, fieldnames=['symbol', 'name', 'trade_time', 'open',
                                           'high', 'low', 'close', 'volume', 'amount'])
        w.writeheader()
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser(description='新浪 5m K线落地（免费源）')
    ap.add_argument('--syms', help='逗号分隔标的，如 688146.SH,600206.SH')
    ap.add_argument('--all-candidates', action='store_true', help='拉候选池+watchlist 11只')
    args = ap.parse_args()

    if args.all_candidates:
        syms = [s for s, _ in DEFAULT_SYMS]
    elif args.syms:
        syms = [s.strip() for s in args.syms.split(',') if s.strip()]
    else:
        ap.print_help()
        return

    print(f'🎯 拉取 {len(syms)} 只标的 5m K线 → {DATA_DIR}')
    ok = 0
    for sym in syms:
        rows = fetch_5m(sym)
        if rows:
            path = save_csv(sym, rows)
            days = len(set(r['trade_time'][:10] for r in rows))
            print(f'  ✅ {sym}: {len(rows)}根/{days}日 → {os.path.basename(path)}')
            ok += 1
        else:
            print(f'  ❌ {sym}: 无数据')
    print(f'\n完成: {ok}/{len(syms)} 成功')


if __name__ == '__main__':
    main()
