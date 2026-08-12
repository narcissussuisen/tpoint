"""2026-08-12 实时信号冒烟探针。
用新浪 1m（真实今日 bar，与 monitor 同源 OHLC 口径）重建当日分钟K，
以生产同源 compute_miji_indicators + detect_for 重放（空仓+无首扫抑制=理想上界），
判断「今天 0 个 B 信号」是真实行情还是检测静默漏判。
PC（前收）取新浪日K上一交易日收盘。
"""
import json, time, sys
import urllib.request
import pandas as pd
import numpy as np

sys.path.insert(0, 'core')
import monitor as M

SYMS = {'161129.SZ': 'sz161129', '513310.SH': 'sh513310', '300757.SZ': 'sz300757'}
NAMES = {'161129.SZ': '原油LOF', '513310.SH': '中韩半导体ETF', '300757.SZ': '罗博特科'}
TODAY = '2026-08-12'

def fetch_jsonp(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.cn'})
    raw = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'ignore')
    import re
    m = re.search(r'\[.*\]', raw, re.S)
    if m:
        return json.loads(m.group(0))
    m = re.search(r'\(.*\)', raw, re.S)
    if m:
        return json.loads(m.group(0)[1:-1])
    raise ValueError('no json array in response: ' + raw[:120])

def sina_1m(tcode):
    url = (f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService'
           f'.getKLineData?symbol={tcode}&scale=1&ma=no&datalen=260')
    rows = fetch_jsonp(url)
    out = []
    for r in rows:
        if r['day'][:10] != TODAY:
            continue
        out.append({
            'trade_date': r['day'][:10],
            'trade_time': r['day'],
            'open': float(r['open']), 'high': float(r['high']),
            'low': float(r['low']), 'close': float(r['close']),
            'volume': float(r.get('volume', 0)) * 100,  # 手→股
        })
    return pd.DataFrame(out)

def sina_prev_close(tcode):
    url = (f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService'
           f'.getKLineData?symbol={tcode}&scale=240&ma=no&datalen=10')
    rows = fetch_jsonp(url)
    # 取 < TODAY 的最近一个交易日收盘 = 前收
    prev = [r for r in rows if r['day'][:10] < TODAY]
    if prev:
        return float(prev[-1]['close'])
    return None

print(f"=== 实时信号探针 {TODAY} (新浪1m + 生产同源 detect_for) ===\n")
total = 0
for sym, tcode in SYMS.items():
    name = NAMES[sym]
    try:
        df = sina_1m(tcode)
    except Exception as e:
        print(f"[{name}] 1m 拉取失败: {e}")
        continue
    if len(df) < 5:
        print(f"[{name}] 1m 不足5根 ({len(df)})，跳过")
        continue
    pc = sina_prev_close(tcode)
    df = df.sort_values('trade_time').reset_index(drop=True)
    print(f"--- {name}({sym}) | 1m={len(df)}根 | 前收PC={pc} | 区间 "
          f"{df['trade_time'].iloc[0][11:16]}~{df['trade_time'].iloc[-1][11:16]} "
          f"收 {df['close'].iloc[0]:.2f}→{df['close'].iloc[-1]:.2f} "
          f"({ (df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:+.2f}%) ---")
    if not pc or pc <= 0:
        print(f"  ⚠️ PC 无效({pc})，detect_for 将零信号返回（这正是静默零信号根因之一）")
    c = df['close'].values.astype(float); h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float); o = df['open'].values.astype(float)
    v = df['volume'].values.astype(float)
    # 生产同源：compute() → compute_miji_indicators
    M.STATE.setdefault(sym, {'PC': 0, 'WARM': None})
    M.STATE[sym]['PC'] = pc
    data = M.compute_miji_indicators(o, h, lo, c, v, pc, has_vol=True)
    data['df'] = df
    data['is_morning'] = None
    # 空仓起步 + 无首扫抑制（理想上界），但透传生产 per-symbol 参数（mpr/atr 门控）
    import json as _j
    cfg = _j.load(open('data/monitor_config.json', encoding='utf-8')).get(sym, {})
    _mpr_e = cfg.get('mpr_enable')
    _mpr_p = cfg.get('mpr_periods')
    _atr_p = cfg.get('atr_min_pct')
    st = {}
    sigs = M.detect_for(sym, name, data, st,
                        mpr_enable=_mpr_e, mpr_periods=_mpr_p, atr_min_pct=_atr_p)
    if sigs:
        print(f"  → 重放检出 {len(sigs)} 条信号，结构示例: {repr(sigs[0])[:160]}")
        for s in sigs:
            # 兼容 tuple 结构：按 detect_for 返回顺序打印
            try:
                stype = s[0] if isinstance(s, (tuple, list)) else s.get('type')
                sprice = s[1] if isinstance(s, (tuple, list)) else s.get('price')
                sk = s[5] if (isinstance(s, (tuple, list)) and len(s) > 5) else (s.get('bar_trade_time','') if isinstance(s,dict) else '')
                print(f"  🔔 {stype} @ {sprice:.2f} [{sk[11:16] if isinstance(sk,str) and len(sk)>=16 else sk}]")
            except Exception as e:
                print(f"  🔔 sig={repr(s)[:120]} (fmt err {e})")
        total += len(sigs)
    else:
        print(f"  → 重放检出 0 条信号（与生产一致：今日该标的确无触发）")
    print()
print(f"=== 重放合计 {total} 条信号 ===")
print("若 total>0 → 实时 monitor 应产生这些信号却没推 → 检测/推送链路有漏；"
      "若 total=0 → 今日三只确实无触发，用户'没信号'是真实行情。")
