"""smoke_5m_index.py — v9.1.1 5分钟K线+大盘指数门控 冒烟测试

两层验证：
  A) 纯函数门控单测(合成数据)：证明"K线形态 AND 指数伴随条件"才放行
  B) 真实5分钟数据端到端：pytdx 直连(已验证可达)取 个股+沪深300 5m,
     注入 ShimDS 跑真实公共 API detect_miji_signals_5m_index

不依赖 mootdx 在沙箱内的服务器可达性(mootdx 由生产 monitor 单独验证)。
"""
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.miji_alpha import (  # noqa: E402
    detect_miji_signals_5m_index, index_buy_at, index_sell_at,
    _gate_signals_by_index, _prev_close_map, _merge_5m,
    compute_trend, detect_miji_signals, compute_miji_indicators,
)

PASS = []


def check(name, cond):
    PASS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


# ========== A) 纯函数门控单测 ==========
print("=== A) 纯函数门控单测 ===")

# --- index_buy_at / index_sell_at 直接断言 ---
check("buy_ok: trend+1 & chg+2%", index_buy_at(1, 0.02) is True)
check("buy_block: trend-1", index_buy_at(-1, 0.02) is False)
check("buy_block: trend+1 & chg-2%(暴跌)", index_buy_at(1, -0.02) is False)
check("sell_ok: trend+1 & chg+2%(锁利)", index_sell_at(1, 0.02) is True)
check("sell_block: trend+1 & chg-2%(强多却下跌, 持仓待涨)",
      index_sell_at(1, -0.02) is False)
check("sell_ok: trend-1(走弱可卖)", index_sell_at(-1, -0.02) is True)

# --- _gate_signals_by_index 端到端门控 ---
N = 20
dates = ['2026-07-14'] * N
idx_up = np.linspace(100, 115, N // 2)
idx_dn = np.linspace(115, 100, N // 2)
idx_c = np.concatenate([idx_up, idx_dn])
trend = compute_trend(idx_c, 5, 20)          # 前半 +1, 后半 -1
pc_map = {'2026-07-14': 100.0}
# 候选: B@5(多头上行, 应放行) / B@15(空头, 应拦截)
#        S@15(空头, 应放行) / S@5(强多绿涨, 应放行)
cand = [
    {'type': 'B', 'idx': 5, 'price': float(idx_c[5]), 'factors': {}},
    {'type': 'B', 'idx': 15, 'price': float(idx_c[15]), 'factors': {}},
    {'type': 'S', 'idx': 15, 'price': float(idx_c[15]), 'factors': {}},
    {'type': 'S', 'idx': 5, 'price': float(idx_c[5]), 'factors': {}},
]
final = _gate_signals_by_index(cand, idx_c, trend, pc_map, dates)
final_types = [(s['type'], s['idx'], s['index_state']['gate']) for s in final]
check("B@5 多头上行 -> 放行(buy_ok)", ('B', 5, 'buy_ok') in final_types)
check("B@15 空头 -> 拦截(缺失)", ('B', 15, 'buy_ok') not in final_types)
check("S@15 空头 -> 放行(sell_ok)", ('S', 15, 'sell_ok') in final_types)
check("S@5 强多绿涨 -> 放行(sell_ok)", ('S', 5, 'sell_ok') in final_types)
check("门控后总数=3 (B@15被剔)", len(final) == 3)

# --- _prev_close_map / _merge_5m ---
import datetime as dt
t0 = pd.Timestamp('2026-07-13 09:35:00')
t1 = pd.Timestamp('2026-07-14 09:35:00')
stock_df = pd.DataFrame({
    'trade_time': [t0, t0 + pd.Timedelta(minutes=5), t1, t1 + pd.Timedelta(minutes=5)],
    'trade_date': ['2026-07-13', '2026-07-13', '2026-07-14', '2026-07-14'],
    'open': [10, 10.1, 10.2, 10.3], 'high': [10.2, 10.3, 10.4, 10.5],
    'low': [9.9, 10.0, 10.1, 10.2], 'close': [10.1, 10.2, 10.3, 10.4],
    'volume': [1000, 1100, 1200, 1300],
})
idx_df = pd.DataFrame({
    'trade_time': [t0, t0 + pd.Timedelta(minutes=5), t1, t1 + pd.Timedelta(minutes=5)],
    'trade_date': ['2026-07-13', '2026-07-13', '2026-07-14', '2026-07-14'],
    'open': [3000, 3001, 3002, 3003], 'high': [3002, 3003, 3004, 3005],
    'low': [2999, 3000, 3001, 3002], 'close': [3001, 3002, 3003, 3004],
    'volume': [1e6, 1.1e6, 1.2e6, 1.3e6],
})
m = _merge_5m(stock_df, idx_df)
check("_merge_5m 内连接保留4根共同时段", m is not None and len(m) == 4)
pcm = _prev_close_map(m['trade_date'].tolist(), m['idx_close'].tolist())
check("_prev_close_map: 07-14 前收=07-13末收(3002)",
      abs(pcm.get('2026-07-14', 0) - 3002.0) < 1e-6)


# ========== B) 真实5分钟数据 端到端 ==========
print("\n=== B) 真实5分钟数据 端到端 (pytdx 直连) ===")

SHIM_STOCK = None
SHIM_IDX = None
try:
    sys.path.insert(0, os.path.join(ROOT, 'venv', 'Lib', 'site-packages'))
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    api.connect('180.153.18.170', 7709)
    print("  pytdx 已连接 180.153.18.170:7709")

    def _to_df_5m(raw, is_index):
        d = api.to_df(raw)
        if d is None or len(d) == 0:
            return None
        d = d.copy()
        if 'datetime' in d.columns:
            dt = pd.to_datetime(d['datetime'])
        else:
            dt = pd.to_datetime(d['year'].astype(int).astype(str) + '-' +
                                  d['month'].astype(int).map(lambda m: f"{int(m):02d}") + '-' +
                                  d['day'].astype(int).map(lambda d: f"{int(d):02d}"))
        out = pd.DataFrame({
            'trade_time': dt,
            'trade_date': dt.dt.strftime('%Y-%m-%d'),
            'open': d['open'].astype(float), 'high': d['high'].astype(float),
            'low': d['low'].astype(float), 'close': d['close'].astype(float),
            'volume': d['vol'].astype(float) if 'vol' in d.columns else 0.0,
        })
        return out.sort_values('trade_time').reset_index(drop=True)

    raw_s = api.get_security_bars(0, 1, '600519', 0, 240)   # 5m, 沪, 茅台
    stock_df = _to_df_5m(raw_s, False)
    raw_i = api.get_index_bars(0, 1, '000300', 0, 240)     # 5m, 沪, 沪深300
    idx_df = _to_df_5m(raw_i, True)
    api.disconnect()

    if stock_df is not None and idx_df is not None:
        print(f"  真实数据: 个股5m {len(stock_df)}根, 指数5m {len(idx_df)}根")
        SHIM_STOCK, SHIM_IDX = stock_df, idx_df
    else:
        print("  真实数据取回为空(可能该服务器无此标的), 跳过 B 层")
except Exception as e:
    print(f"  pytdx 真实取数失败: {e} (跳过 B 层, 不影响 A 层结论)")


if SHIM_STOCK is not None:
    class _ShimDS:
        def __init__(self, s, i):
            self._s, self._i = s, i

        def get_5m(self, sym, count=240):
            return self._s

        def get_index_5m(self, sym, count=240, market=None):
            return self._i

    sigs, meta = detect_miji_signals_5m_index(
        '600519.SH', index_sym='000300', index_market=1,
        count=240, ds=_ShimDS(SHIM_STOCK, SHIM_IDX))
    print(f"  真实端到端 meta: {meta}")
    check("真实端到端: detect 返回 (list, dict)",
          isinstance(sigs, list) and isinstance(meta, dict) and meta.get('ok') is True)
    n_b = sum(1 for s in sigs if s['type'] == 'B')
    n_s = sum(1 for s in sigs if s['type'] == 'S')
    print(f"  真实信号: 候选={meta['n_cand']} 最终(指数门控后)={meta['n_final']} "
          f"(B={n_b}, S={n_s})")
    if sigs:
        s0 = sigs[0]
        print(f"  首条最终信号样例: type={s0['type']} idx={s0['idx']} "
              f"price={s0['price']} index_state={s0.get('index_state')}")
    check("真实端到端: 门控后信号数 <= 候选数",
          meta['n_final'] <= meta['n_cand'])
else:
    print("  (B 层未执行: 无真实数据, A 层已覆盖门控逻辑)")


# ========== 结论 ==========
print("\n=== 结论 ===")
ok = all(PASS)
print(f"  {'ALL PASS' if ok else 'SOME FAILED'} ({sum(PASS)}/{len(PASS)})")
sys.exit(0 if ok else 1)
