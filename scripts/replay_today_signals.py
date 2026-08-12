import sys, json
sys.path.insert(0, r'C:\Users\YZP\WorkBuddy\Claw\tpoint')
sys.path.insert(0, r'C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages')
import numpy as np
import pandas as pd
from datetime import datetime

from core import datasource as ds
from core import miji_alpha as ma

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
SYMS = {'161129.SZ': '原油LOF易方达', '513310.SH': '中韩半导体ETF华泰柏瑞', '300757.SZ': '罗博特科'}

tf = ds.MootdxDataSource()

def get_prev_close(sym):
    d = tf.klines.get(sym, period='1d', count=6, as_dataframe=True)
    closes = d['close'].values.astype(float)
    return float(closes[-2])  # 倒数第二根=昨日收盘(最后一根为当日进行中)

def load_cfg():
    try:
        with open(f'{BASE}/data/monitor_config.json') as f:
            return json.load(f)
    except Exception as e:
        return {}

cfg_all = load_cfg()
today = datetime.now().strftime('%Y-%m-%d')

total = 0
for sym, name in SYMS.items():
    try:
        df = tf.klines.intraday(sym, as_dataframe=True)
    except Exception as e:
        print(f'{name}({sym}): intraday 异常 {e}')
        continue
    if df is None or len(df) < 10:
        print(f'{name}({sym}): 无日内数据 (len={None if df is None else len(df)})')
        continue
    df = df.copy()
    if 'trade_time' in df.columns:
        tt = pd.to_datetime(df['trade_time'])
        df = df[tt.dt.strftime('%Y-%m-%d') == today].sort_values('trade_time').reset_index(drop=True)
    if len(df) < 10:
        print(f'{name}({sym}): 日内数据不足今日 {len(df)} 根')
        continue
    o = df['open'].values.astype(float); h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    try:
        pc = get_prev_close(sym)
    except Exception as e:
        print(f'{name}({sym}): 取昨收失败 {e}')
        continue
    data = ma.compute_miji_indicators(o, h, lo, c, v, pc)
    cfg = cfg_all.get(sym, {})
    atr_p = cfg.get('atr_min_pct')
    mpr_e = cfg.get('mpr_enable'); mpr_p = cfg.get('mpr_periods')
    # 生产同源触发：monitor 用 check_b_trigger/check_s_trigger 包裹 check_miji_trigger
    # B: floor + min_hist_diff=0.15(env TP_MHD_THRESHOLD) + per-symbol atr/mpr
    # S: floor + min_hist_diff=0.15, 不过滤 atr/mpr
    times = pd.to_datetime(df['trade_time']).dt.strftime('%H:%M:%S').values
    dt = pd.to_datetime(df['trade_time'])
    # 首扫抑制分界：假设最近一次 monitor 重启约 10:47，首扫会抑制 (now-3min)=10:44 之前的信号
    cutoff = pd.Timestamp.now().replace(hour=10, minute=44, second=0, microsecond=0)
    fired = []
    for i in range(2, data['n']):
        if data['atr'][i] <= 0:
            continue
        b_trig, s_trig, b_det, s_det, _snap = ma.check_miji_trigger(
            data, i, macd_gate_mode='floor', min_hist_diff=0.15,
            atr_min_pct=atr_p, mpr_enable=mpr_e, mpr_periods=mpr_p)
        if b_trig:
            fired.append(('B', i, times[i], data['c'][i], b_det))
        if s_trig:
            fired.append(('S', i, times[i], data['c'][i], s_det))
    live = [f for f in fired if dt.iloc[f[1]] >= cutoff]
    print(f'\n=== {name}({sym}) pc={pc:.2f} bars={len(df)} 候选触发={len(fired)} (atr={atr_p}, mpr={mpr_e}) ===')
    for f in fired:
        tag = ' [LIVE]' if dt.iloc[f[1]] >= cutoff else ' [首扫抑制]'
        print(f"  {f[0]} @bar{f[1]} t={f[2]} px={f[3]:.2f} {f[4]}{tag}")
    print(f"  >> 实时窗口(>=10:44)应发={len(live)} 条")
    total += len(live)

print(f'\n>>> 实时窗口(>=10:44)本应触发信号合计: {total} 条 (若>0 而 monitor 实发0 => 漏推/bug)')
