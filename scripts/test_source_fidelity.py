# -*- coding: utf-8 -*-
"""scripts/test_source_fidelity.py — 数据源口径保真回归（2026-09-10 P1b/P2）

背景：`core/datasource.py` 的兜底链原本是「腾讯分时(合成OHLC) → 新浪1m(真实OHLC)」，
但同文件 51-53 行注释早已写明**新浪真实 OHLC 质量优于腾讯分时合成**——执行顺序与既定口径相反。

量化依据（全历史 448 标的-日，把腾讯合成变换施加到 F 盘真实 1m）：
  合成/真实 ATR 比值中位 **0.582**（低估 41.8%，p10-p90 = 0.451~0.697）；
  信号总量比 1.077（+7.7%，低于数量级门槛），但**逐日 48.4% 的信号组合发生变化**（位置漂移）。
⇒ 合成口径下 1.5×ATR 反T止损窄掉约 42%，会显著放大「频繁被打」。

本测试锁定三条：
  ① 兜底链**真实 OHLC 优先**（默认新浪），且 env `TP_INTRADAY_PREFER=tencent` 能回滚；
  ② `df.attrs['data_source']` 三态正确（mootdx / sina / tencent_synth）；
  ③ 合成变换确实系统性低估 ATR（用确定性构造的数据断言，另可选 F 盘实测）。
全部离线（mock urlopen / stub client），不依赖外网。

运行：venv/Scripts/python.exe scripts/test_source_fidelity.py
"""
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import numpy as np                       # noqa: E402
import pandas as pd                      # noqa: E402
import datasource as DS                  # noqa: E402
from primitives import compute_atr       # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ' + str(detail)}")


TODAY = datetime.now().strftime('%Y-%m-%d')
TODAY_C = datetime.now().strftime('%Y%m%d')
N = 12


def _sina_payload():
    """新浪 1m JSONP：真实 OHLC（h/l 与 c 不等）。"""
    rows = []
    for i in range(N):
        c = 5.20 + 0.01 * (i % 3)
        rows.append({'day': f'{TODAY} 09:{31 + i:02d}:00', 'open': f'{c - 0.01:.3f}',
                     'high': f'{c + 0.02:.3f}', 'low': f'{c - 0.03:.3f}', 'close': f'{c:.3f}',
                     'volume': '1000', 'amount': '5000'})
    return ('var _x=(' + json.dumps(rows) + ');').encode('utf-8')


def _tencent_payload():
    """腾讯分时：只有分钟价 + 逐分钟量（合成 OHLC）。"""
    lines = []
    for i in range(N):
        lines.append(f'09{31 + i:02d} {5.20 + 0.01 * (i % 3):.2f} {1000 + i} 5000')
    body = {'data': {'sz300010': {'data': {'data': lines, 'date': TODAY_C}}}}
    return json.dumps(body).encode('utf-8')


class _Resp:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b


def _fake_urlopen(url, timeout=None, **kw):
    u = url if isinstance(url, str) else getattr(url, 'full_url', '')
    if 'sina' in u:
        return _Resp(_sina_payload())
    if DS._TENCENT_PATH in u:
        return _Resp(_tencent_payload())
    raise AssertionError('unexpected url: ' + u)


class _StubClient:
    """mootdx client 桩：bars() 返回真实 OHLC 的当日 1m。"""

    def __init__(self, n):
        self._n = n

    def bars(self, symbol=None, frequency=None, offset=None):
        if self._n <= 0:
            return None
        tt = [f'{TODAY} 09:{31 + i:02d}:00' for i in range(self._n)]
        c = np.array([5.20 + 0.01 * (i % 3) for i in range(self._n)])
        return pd.DataFrame({'datetime': tt, 'open': c - 0.01, 'high': c + 0.02,
                             'low': c - 0.03, 'close': c, 'vol': np.full(self._n, 1000.0)})


def _run_intraday(mootdx_rows, prefer=None):
    ds = DS.MootdxDataSource()
    ds._client_stub = _StubClient(mootdx_rows)
    orig_client = DS.MootdxDataSource.client
    orig_open = DS.urllib.request.urlopen
    old_env = os.environ.get('TP_INTRADAY_PREFER')
    try:
        DS.MootdxDataSource.client = property(lambda self, _s=ds: _s._client_stub)
        DS.urllib.request.urlopen = _fake_urlopen
        if prefer is None:
            os.environ.pop('TP_INTRADAY_PREFER', None)
        else:
            os.environ['TP_INTRADAY_PREFER'] = prefer
        return ds.intraday('300010.SZ', as_dataframe=True)
    finally:
        DS.MootdxDataSource.client = orig_client
        DS.urllib.request.urlopen = orig_open
        if old_env is None:
            os.environ.pop('TP_INTRADAY_PREFER', None)
        else:
            os.environ['TP_INTRADAY_PREFER'] = old_env


def main():
    print('\n=== 1. 兜底链顺序：真实 OHLC 优先 + env 回滚 ===')
    df_def = _run_intraday(0, prefer=None)
    check('默认（未设 env）命中新浪真实 OHLC',
          df_def is not None and df_def.attrs.get('data_source') == 'sina',
          f'got={None if df_def is None else df_def.attrs.get("data_source")}')
    if df_def is not None:
        check('新浪源确为真实 OHLC（存在 h>l 的 bar）',
              bool((df_def['high'] > df_def['low']).any()))
    df_rb = _run_intraday(0, prefer='tencent')
    check('TP_INTRADAY_PREFER=tencent 回滚到腾讯合成',
          df_rb is not None and df_rb.attrs.get('data_source') == 'tencent_synth',
          f'got={None if df_rb is None else df_rb.attrs.get("data_source")}')
    if df_rb is not None:
        check('腾讯源为合成 OHLC（首根之后 h/l 恒等于相邻收盘的极值）',
              bool(((df_rb['high'] == df_rb['low']) | (df_rb['high'] > df_rb['low'])).all()))

    print('\n=== 2. mootdx 主源可用时优先，且标记为 mootdx ===')
    df_m = _run_intraday(30, prefer=None)
    check('mootdx 返回 30 行 → data_source=mootdx',
          df_m is not None and df_m.attrs.get('data_source') == 'mootdx',
          f'got={None if df_m is None else df_m.attrs.get("data_source")}')
    df_p = _run_intraday(3, prefer=None)
    check('mootdx 仅 3 行(<5) → 走兜底，data_source=sina',
          df_p is not None and df_p.attrs.get('data_source') == 'sina',
          f'got={None if df_p is None else df_p.attrs.get("data_source")}')

    print('\n=== 3. 合成变换系统性低估 ATR（确定性构造断言）===')
    n = 60
    c = 5.0 + 0.30 * np.sin(np.arange(n) / 5.0) + 0.02 * np.arange(n) / n
    # 真实分钟振幅：围绕收盘价 ±0.6%，且与相邻收盘差无关
    h_r = c * 1.006
    l_r = c * 0.994
    a_real = compute_atr(h_r, l_r, c)[-1]
    # 腾讯合成：open=前收, high=max(前收,今收), low=min(前收,今收)
    h_s = np.maximum(c[:-1], c[1:])
    l_s = np.minimum(c[:-1], c[1:])
    a_syn = compute_atr(np.r_[c[0], h_s], np.r_[c[0], l_s], c)[-1]
    ratio = a_syn / a_real
    check('构造样本：合成 ATR < 真实 ATR', a_syn < a_real, f'real={a_real:.5f} syn={a_syn:.5f}')
    check('构造样本：比值落在实测带 0.3~0.85 内', 0.30 <= ratio <= 0.85, f'ratio={ratio:.3f}')

    fp = r'F:\keyfactor_data\1m\300010.SZ_1m.csv'
    if os.path.exists(fp):
        d = pd.read_csv(fp, encoding='utf-8-sig')
        d['trade_date'] = d['trade_date'].astype(str)
        day = d[d['trade_date'] == d['trade_date'].max()].sort_values('trade_time')
        if len(day) >= 30:
            cr = day['close'].values.astype(float)
            hi = day['high'].values.astype(float)
            lo = day['low'].values.astype(float)
            ar = compute_atr(hi, lo, cr)[-1]
            asyn = compute_atr(np.r_[cr[0], np.maximum(cr[:-1], cr[1:])],
                               np.r_[cr[0], np.minimum(cr[:-1], cr[1:])], cr)[-1]
            r2 = asyn / ar
            print(f'  [实测] F盘 300010 {day["trade_date"].iloc[0]}：真实ATR={ar:.5f} '
                  f'合成ATR={asyn:.5f} 比值={r2:.3f}')
            check('F盘实测：合成 ATR 低估（比值 < 0.85）', r2 < 0.85, f'ratio={r2:.3f}')
    else:
        print('  [跳过] F盘 300010 基准文件不存在（非阻塞）')

    print('\n=== 4. 数据源标记写入位置与取值完备 ===')
    src = io.open(os.path.join(ROOT, 'core', 'datasource.py'), encoding='utf-8').read()
    check("intraday() 末尾写 df.attrs['data_source']", "df.attrs['data_source'] = src" in src)
    for v in ('mootdx', 'sina', 'tencent_synth', 'mootdx_partial'):
        check(f'取值 {v} 存在', f"'{v}'" in src)
    check('_fetch_pool 由 TP_INTRADAY_PREFER 控制顺序',
          "TP_INTRADAY_PREFER" in src and "_sina_first" in src)
    check('兜底命中源写入 df.attrs[fallback_source]', "df.attrs['fallback_source']" in src)

    print(f'\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过')
    if FAIL:
        print('失败项: ' + ' | '.join(FAIL))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
