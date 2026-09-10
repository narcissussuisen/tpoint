# -*- coding: utf-8 -*-
"""scripts/datasource_benchmark.py — 行情多源基准（2026-09-10 D）

目的：用**数据**而不是印象**固定稳定数据源**。背景：2026-09-10 mootdx 因「服务器选择缺陷」
全天返空、降级 2.5 小时静默；而当天实测 103 台 TDX 服务器里 9 台完全可用 —— 说明
「哪个源稳定」必须持续测量，而不是拍脑袋。

候选源（4 个，全部可被 Python 进程直接调用；MCP 类连接器不参与，见下）：
  1. `mootdx`          通达信 TCP 7709（真实 OHLCV，主源候选）
  2. `sina`            新浪 1m K 线（真实 OHLC，免鉴权 HTTP，兜底首选）
  3. `tencent_synth`   腾讯分时（**合成 OHLC**，口径降级，末位兜底）
  4. `tencent_mkline`  腾讯 m1 K 线（真实 OHLC，免鉴权 HTTP，未启用的候选）

⚠️ 不纳入 tdx-connector / westock-mcp：它们是**远端 HTTP MCP + OAuth**，凭据存于宿主
   AES-256-GCM 加密库，monitor 进程拿不到 token（详见 docs/datasource_incident_runbook.md §5）。

每次采样记录：`{ts, source, sym, ok, bars, latency_ms, synth_sig, range_pct, vol_ok, bar_lag_s, err}`
（`synth_sig` = 合成口径签名，见 `_stat()` 注释）
落 `data/datasource_benchmark.jsonl`（append-only）。
汇总：`--summarize` → `output/datasource_benchmark_<date>.json` + 控制台表。

判稳标准（连续 3~5 个交易日达标才固化进 config/datasource_policy.json）：
  · 可用率 ≥ 99%
  · `synth_sig` < 0.90（合成口径签名的构造值恒为 1.0，见 _stat() 注释；真实源实测 0.55~0.75）
  · 跨源收盘一致率 ≥ 95%（跨源共识做当日保真度基准）

用法：
  python scripts/datasource_benchmark.py --once                    # 采样一轮（默认 watchlist 标的）
  python scripts/datasource_benchmark.py --once --sym 300010.SZ
  python scripts/datasource_benchmark.py --summarize               # 汇总今天
  python scripts/datasource_benchmark.py --summarize --date 2026-09-10
"""
import argparse
import io
import json
import os
import statistics as st
import numpy as np
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import datasource as DS          # noqa: E402
import trading_calendar as TC    # noqa: E402  （交易日单一真源；勿再内联节假日表）

OUT_JSONL = os.path.join(ROOT, 'data', 'datasource_benchmark.jsonl')
BENCH_LOG = os.path.join(ROOT, 'logs', 'datasource_benchmark.log')
OUT_DIR = os.path.join(ROOT, 'output')
SOURCES = ('mootdx', 'sina', 'tencent_synth', 'tencent_mkline')


def say(msg):
    """同时写日志文件与 stdout。

    ⚠️ 定时任务用 **pythonw.exe**（GUI 子系统，无控制台窗口——用户明确禁止任何可见 cmd 窗口），
    此时 `sys.stdout` 可能为 None，`print()` 会异常；且没有控制台就看不到输出。
    故所有定时路径的输出必须走 say() 落 `logs/datasource_benchmark.log`。
    """
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(BENCH_LOG), exist_ok=True)
        with io.open(BENCH_LOG, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def _watchlist_syms():
    try:
        with io.open(os.path.join(ROOT, 'data', 'watchlist.json'), encoding='utf-8') as f:
            return list(json.load(f).keys())
    except Exception:
        return []


def _http_json(url, enc='utf-8', referer=None, timeout=8.0):
    h = {'User-Agent': 'Mozilla/5.0'}
    if referer:
        h['Referer'] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(enc, 'ignore')


def _stat(df, t0):
    """把 DataFrame 变成一条采样指标。

    ⚠️ 关键指标 `synth_sig`（合成口径签名）：腾讯分时兜底的变换是
    `high = max(前收, 今收)`、`low = min(前收, 今收)`（datasource.py:_fetch_host），
    因此**每根 bar 的 high 或 low 必然等于前一根收盘价**（首根除外）。
    真实 OHLC 极少满足该恒等式 ⇒ `synth_sig ≈ 1.0` 即判定为合成口径。
    ⚠️ 反面教材：最初用「h>l 占比」当真实度指标，结果合成口径拿到 0.988、真实源 0.968/0.992，
    **完全无法区分**（任何有价格变动的分钟都会 h>l）。合成数据必须用签名式判别，不能靠振幅。
    """
    lat = round((time.time() - t0) * 1000, 1)
    if df is None or len(df) == 0:
        return {'ok': 0, 'bars': 0, 'latency_ms': lat, 'synth_sig': None,
                'range_pct': None, 'vol_ok': None, 'bar_lag_s': None, 'close': None}
    try:
        h = df['high'].values.astype(float)
        l = df['low'].values.astype(float)
        c = df['close'].values.astype(float)
        v = df['volume'].values.astype(float) if 'volume' in df.columns else None
        tt = df['trade_time'] if 'trade_time' in df.columns else None
        lag = None
        if tt is not None and len(tt):
            try:
                lag = round((datetime.now() - tt.iloc[-1].to_pydatetime()).total_seconds(), 1)
            except Exception:
                lag = None
        # 合成口径签名：high/low 是否等于前一根收盘
        sig = None
        if len(c) > 2:
            prev = c[:-1]
            hh, ll = h[1:], l[1:]
            hit = np.isclose(hh, prev, rtol=0, atol=1e-9) | np.isclose(ll, prev, rtol=0, atol=1e-9)
            sig = round(float(hit.mean()), 3)
        return {'ok': 1, 'bars': int(len(df)), 'latency_ms': lat,
                'synth_sig': sig,
                'range_pct': round(float(((h - l) / c).mean() * 100), 4),
                'vol_ok': (None if v is None else bool((v >= 0).all() and v.sum() > 0)),
                'bar_lag_s': lag, 'close': float(c[-1])}
    except Exception as e:
        return {'ok': 0, 'bars': 0, 'latency_ms': lat, 'synth_sig': None,
                'range_pct': None, 'vol_ok': None, 'bar_lag_s': None, 'close': None,
                'err': f'{type(e).__name__}: {e}'}


def _is_env_limited(err):
    """判断失败是不是**本机测量环境**造成（HTTP 代理隧道/域名被拦），而非源本身不可用。
    ⚠️ 生产 monitor 不经此代理（如腾讯 web.ifzq.gtimg.cn 在 monitor 侧正常、在采集侧 502）。"""
    e = str(err or '').lower()
    return any(k in e for k in ('tunnel connection failed', '502 bad gateway',
                                'getaddrinfo', 'proxy', 'connection refused'))


# ------------------------------ 四个源探针 ------------------------------ #
_mootdx_client = {'cli': None, 'tried': False}


def probe_mootdx(sym):
    t0 = time.time()
    try:
        if _mootdx_client['cli'] is None:
            if _mootdx_client['tried']:
                return {'ok': 0, 'bars': 0, 'latency_ms': 0.0, 'synth_sig': None,
                        'range_pct': None, 'vol_ok': None, 'bar_lag_s': None, 'close': None,
                        'err': 'TDX 客户端不可用（本进程内已失败过）'}
            _mootdx_client['tried'] = True
            _mootdx_client['cli'] = DS.tdx_client()
        code, _ = DS._to_mootdx_sym(sym)
        df = _mootdx_client['cli'].bars(symbol=code, frequency=8, offset=250)
        return _stat(df, t0)
    except Exception as e:
        _e = f'{type(e).__name__}: {e}'
        return {'ok': 0, 'bars': 0, 'latency_ms': round((time.time() - t0) * 1000, 1),
                'synth_sig': None, 'range_pct': None, 'vol_ok': None, 'bar_lag_s': None,
                'close': None, 'err': _e, 'err_env': _is_env_limited(_e)}


def probe_sina(sym):
    t0 = time.time()
    try:
        import pandas as pd
        import re
        tcode = DS._tencent_code(sym)
        raw = _http_json(DS._SINA_MINUTE_URL.format(tcode=tcode),
                         referer='https://finance.sina.com.cn/')
        rows = []
        for d in json.loads(re.search(r'\((\[.*\])\)', raw, re.S).group(1)):
            try:
                rows.append({'trade_time': pd.Timestamp(d['day']),
                             'trade_date': str(d['day'])[:10],
                             'open': float(d['open']), 'close': float(d['close']),
                             'high': float(d['high']), 'low': float(d['low']),
                             'volume': float(d.get('volume', 0))})
            except (KeyError, TypeError, ValueError):
                continue
        df = pd.DataFrame(rows)
        if len(df):
            today = datetime.now().strftime('%Y-%m-%d')
            df = df[df['trade_date'] == today]
        return _stat(df, t0)
    except Exception as e:
        _e = f'{type(e).__name__}: {e}'
        return {'ok': 0, 'bars': 0, 'latency_ms': round((time.time() - t0) * 1000, 1),
                'synth_sig': None, 'range_pct': None, 'vol_ok': None, 'bar_lag_s': None,
                'close': None, 'err': _e, 'err_env': _is_env_limited(_e)}


def probe_tencent_synth(sym):
    """腾讯分时（合成 OHLC）。

    ⚠️ 必须用 env `TP_INTRADAY_PREFER=tencent` 强制走腾讯分支：
    `_tencent_intraday_fallback()` 内部走 `_fetch_pool()`，而后者自 2026-09-10 起
    **默认新浪优先** —— 不加 env 会把新浪的数据当成「腾讯合成」记录（本基准首版即犯此错：
    腾讯行签名 0.586 ≈ 新浪 0.595，说明根本没测到腾讯）。
    拿到结果后还校验 `attrs['fallback_source']`，不一致即判为采样失败，避免再污染基准。
    """
    t0 = time.time()
    old = os.environ.get('TP_INTRADAY_PREFER')
    os.environ['TP_INTRADAY_PREFER'] = 'tencent'
    try:
        df = DS.MootdxDataSource()._tencent_intraday_fallback(sym)
        src = (getattr(df, 'attrs', None) or {}).get('fallback_source')
        if df is not None and src != 'tencent_synth':
            raise RuntimeError(f'来源标记不符（期望 tencent_synth，实得 {src}）')
        return _stat(df, t0)
    except Exception as e:
        _e = f'{type(e).__name__}: {e}'
        return {'ok': 0, 'bars': 0, 'latency_ms': round((time.time() - t0) * 1000, 1),
                'synth_sig': None, 'range_pct': None, 'vol_ok': None, 'bar_lag_s': None,
                'close': None, 'err': _e, 'err_env': _is_env_limited(_e)}
    finally:
        if old is None:
            os.environ.pop('TP_INTRADAY_PREFER', None)
        else:
            os.environ['TP_INTRADAY_PREFER'] = old


def probe_tencent_mkline(sym):
    """腾讯 m1 K 线（真实 OHLC）；未启用的候选源，测它是否值得上位。"""
    t0 = time.time()
    try:
        import pandas as pd
        tcode = DS._tencent_code(sym)
        url = (f'https://web.ifzq.gtimg.cn/appstock/app/kline/mkline'
               f'?param={tcode},m1,,250')
        j = json.loads(_http_json(url, referer='https://gu.qq.com/'))
        node = (j.get('data') or {}).get(tcode) or {}
        arr = node.get('m1') or []
        rows = []
        for it in arr:
            try:
                rows.append({'trade_time': pd.Timestamp(it[0]),
                             'trade_date': str(it[0])[:10],
                             'open': float(it[1]), 'close': float(it[2]),
                             'high': float(it[3]), 'low': float(it[4]),
                             'volume': float(it[5])})
            except (IndexError, TypeError, ValueError):
                continue
        df = pd.DataFrame(rows)
        if len(df):
            today = datetime.now().strftime('%Y-%m-%d')
            df = df[df['trade_date'] == today]
        return _stat(df, t0)
    except Exception as e:
        _e = f'{type(e).__name__}: {e}'
        return {'ok': 0, 'bars': 0, 'latency_ms': round((time.time() - t0) * 1000, 1),
                'synth_sig': None, 'range_pct': None, 'vol_ok': None, 'bar_lag_s': None,
                'close': None, 'err': _e, 'err_env': _is_env_limited(_e)}


PROBES = {'mootdx': probe_mootdx, 'sina': probe_sina,
          'tencent_synth': probe_tencent_synth, 'tencent_mkline': probe_tencent_mkline}


def once(syms, quiet=False):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    recs = []
    for sym in syms:
        closes = {}
        for src in SOURCES:
            r = PROBES[src](sym)
            r.update({'ts': ts, 'source': src, 'sym': sym})
            recs.append(r)
            if r.get('close'):
                closes[src] = r['close']
            if not quiet:
                ev = ('OK bars=%d %.0fms synth_sig=%s' % (r['bars'], r['latency_ms'] or 0, r['synth_sig'])
                      if r['ok'] else ('FAIL(env?) ' if r.get('err_env') else 'FAIL ')
                      + str(r.get('err'))[:56])
                say(f'  {sym:<12} {src:<16} {ev}')
        # 跨源共识（当日保真度基准）：两两收盘一致率（相对差 < 0.1% 视为一致）
        if len(closes) >= 2:
            for i, a in enumerate(list(closes)):
                for b in list(closes)[i + 1:]:
                    pa, pb = closes[a], closes[b]
                    agree = abs(pa - pb) / max(pb, 1e-9) < 0.001
                    recs.append({'ts': ts, 'source': f'consensus:{a}|{b}', 'sym': sym,
                                 'ok': 1 if agree else 0, 'bars': None, 'latency_ms': None,
                                 'synth_sig': None, 'range_pct': None, 'vol_ok': None, 'bar_lag_s': None,
                                 'close': None, 'agree': bool(agree),
                                 'diff_pct': round((pa - pb) / max(pb, 1e-9) * 100, 4)})
    os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)
    with io.open(OUT_JSONL, 'a', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    _ok = {}
    for r in recs:
        _ok.setdefault(r['source'], [0, 0])
        _ok[r['source']][1] += 1
        _ok[r['source']][0] += 1 if r.get('ok') else 0
    _brief = ' '.join('%s=%d/%d' % (k, v[0], v[1]) for k, v in _ok.items())
    say(f'已追加 {len(recs)} 条采样 → {os.path.relpath(OUT_JSONL, ROOT)} | {_brief}')
    return recs


def summarize(date=None):
    date = date or datetime.now().strftime('%Y-%m-%d')
    if not os.path.exists(OUT_JSONL):
        print('无采样数据'); return 1
    rows = []
    for ln in io.open(OUT_JSONL, encoding='utf-8'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if str(r.get('ts', '')).startswith(date):
            rows.append(r)
    if not rows:
        # 77 = 既有 SKIPPED 约定（见 run_daily_review.bat 注释）：当天没有样本不算失败
        print(f'{date} 无采样数据 → 跳过（rc=77 SKIPPED）')
        return 77

    print(f'\n=== 多源基准汇总 {date}（{len(rows)} 条采样）===')
    print('%-16s %6s %8s %10s %10s %11s %9s %9s' % (
        '源', '样本', '可用率', 'p50延迟ms', 'p95延迟ms', '合成签名', '均振幅%', '均bar数'))
    summary = {'date': date, 'n_samples': len(rows), 'sources': {}}
    for src in SOURCES:
        rs = [r for r in rows if r.get('source') == src]
        if not rs:
            continue
        ok = sum(1 for r in rs if r.get('ok'))
        lat = sorted([r['latency_ms'] for r in rs if r.get('latency_ms')])
        sig = [r['synth_sig'] for r in rs if r.get('synth_sig') is not None]
        rng = [r['range_pct'] for r in rs if r.get('range_pct') is not None]
        n_env = sum(1 for r in rs if r.get('err_env'))
        bars = [r['bars'] for r in rs if r.get('bars')]
        _sig_mean = round(st.mean(sig), 3) if sig else None
        entry = {'samples': len(rs), 'ok_rate': round(ok / len(rs), 4),
                 'env_limited': n_env,
                 'p50_ms': lat[len(lat) // 2] if lat else None,
                 'p95_ms': lat[int(len(lat) * 0.95)] if lat else None,
                 # 合成签名 ≈1.0 → 该源是合成口径（口径降级）；真实 OHLC 应显著 < 0.5
                 'synth_sig_mean': _sig_mean,
                 # 合成口径的签名在构造上恒为 1.0（每根 bar 的 h 或 l 必等于前收）；
                 # 真实源因 0.01 价格量化常在 0.55~0.75 ⇒ 阈值 0.95 可干净切开。
                 'is_synthetic': (None if _sig_mean is None else bool(_sig_mean >= 0.95)),
                 'range_pct_mean': round(st.mean(rng), 4) if rng else None,
                 'mean_bars': round(st.mean(bars), 1) if bars else None,
                 'verdict': ('稳定' if (ok / len(rs) >= 0.99 and _sig_mean is not None
                                        and _sig_mean < 0.90)
                             else ('口径降级(合成)' if _sig_mean is not None and _sig_mean >= 0.95
                                   else '未达标'))}
        summary['sources'][src] = entry
        print('%-16s %6d %7.1f%% %10s %10s %11s %9s %9s  %s' % (
            src, entry['samples'], entry['ok_rate'] * 100,
            f"{entry['p50_ms']:.0f}" if entry['p50_ms'] else '-',
            f"{entry['p95_ms']:.0f}" if entry['p95_ms'] else '-',
            f"{entry['synth_sig_mean']:.3f}" if entry['synth_sig_mean'] is not None else '-',
            f"{entry['range_pct_mean']:.4f}" if entry['range_pct_mean'] is not None else '-',
            entry['mean_bars'] if entry['mean_bars'] else '-',
            entry['verdict'] + (f" [env受限{n_env}]" if n_env else '')))

    cons = [r for r in rows if str(r.get('source', '')).startswith('consensus:')]
    if cons:
        print('\n跨源收盘一致率（当日保真度基准，|Δ|<0.1% 视为一致）：')
        pairs = {}
        for r in cons:
            pairs.setdefault(r['source'], []).append(1 if r.get('agree') else 0)
        for k, v in sorted(pairs.items()):
            rate = sum(v) / len(v)
            summary.setdefault('consensus', {})[k] = round(rate, 4)
            print(f'  {k:<40} {rate * 100:.1f}%  (n={len(v)})')

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f'datasource_benchmark_{date}.json')
    json.dump(summary, io.open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n已落盘 {os.path.relpath(out, ROOT)}')
    print('判稳标准：可用率≥99% 且 合成签名<0.90（真实OHLC），连续 3~5 个交易日达标后写 config/datasource_policy.json')
    print('说明：合成签名≥0.95 = 该源是**合成 OHLC**（口径降级，ATR 中位低估 41.8%），不可作主源；')
    print('      实测 2026-09-10：腾讯分时=1.000（构造上恒等），新浪 0.595 / mootdx 0.715（真实）。')
    print('[env受限] = 失败疑似本机 HTTP 代理所致（生产 monitor 不经该代理），需人工复核后再判定。')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--summarize', action='store_true')
    ap.add_argument('--date')
    ap.add_argument('--sym', action='append')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--force', action='store_true', help='非交易日也采样（默认跳过）')
    a = ap.parse_args()
    if a.summarize:
        return summarize(a.date)
    if a.once:
        # 交易日守门：休市日采集只会拿到"昨天收盘的静态数据"，把可用率/延迟统计搅浑。
        if not a.force and not TC.is_trading_today():
            say('非交易日 → 跳过采样（--force 可强制）')
            return 0
        syms = a.sym or _watchlist_syms()
        if not syms:
            say('无标的可测（watchlist 为空）'); return 1
        once(syms, quiet=a.quiet)
        return 0
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
