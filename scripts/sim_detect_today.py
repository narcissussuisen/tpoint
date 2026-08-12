import sys, os
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = BASE + r'\core'
sys.path.insert(0, CORE)
sys.path.insert(0, BASE + r'\venv\Lib\site-packages')
os.chdir(CORE)

import pandas as pd
from datetime import datetime

from datasource import MootdxDataSource as TickFlow
from miji_alpha import compute_miji_indicators
import monitor  # 真实生产模块(core/monitor.py)

SYM = '300757.SZ'; NAME = '罗博特科'
tf = TickFlow()

df = tf.klines.intraday(SYM, as_dataframe=True)
df = df.sort_values('trade_time').reset_index(drop=True)
today = datetime.now().strftime('%Y-%m-%d')
tt = pd.to_datetime(df['trade_time'])
df = df[tt.dt.strftime('%Y-%m-%d') == today].reset_index(drop=True)
o = df['open'].values.astype(float); h = df['high'].values.astype(float)
lo = df['low'].values.astype(float); c = df['close'].values.astype(float)
v = df['volume'].values.astype(float) if 'volume' in df.columns else None
pc = float(tf.klines.get(SYM, period='1d', count=6, as_dataframe=True)['close'].values[-2])

data = compute_miji_indicators(o, h, lo, c, v, pc)
data['df'] = df
monitor.STATE[SYM] = {'PC': pc, 'WARM': None}

# 模拟"首扫"在 cutoff_sim：把之前 bar 标记为已处理(抑制历史, 用 truthy 标记)
st = {}
import sys
_cut = sys.argv[1] if len(sys.argv) > 1 else '11:00'
_h, _m = _cut.split(':')
cutoff_sim = pd.Timestamp.now().replace(hour=int(_h), minute=int(_m), second=0, microsecond=0)
# 2026-08-12：detect_for 的 bar_key 已改为带日期 f"bar_{sym}_{YYYYMMDD}_{i}"（P0 跨日残留修复）。
# 本脚本若仍写旧格式，标记将永不命中 → 首扫抑制模拟静默失效（看起来"信号照出"实为未抑制）。
_bk_day = datetime.now().strftime('%Y%m%d')
for idx in range(len(df)):
    t = pd.to_datetime(df['trade_time'].iloc[idx])
    if t < cutoff_sim:
        st[f'bar_{SYM}_{_bk_day}_{idx}'] = 1  # truthy 标记=已处理

sigs = monitor.detect_for(SYM, NAME, data, st)
print(f'=== 模拟首扫@11:00 后 detect_for 返回信号数: {len(sigs)} ===')
times = pd.to_datetime(df['trade_time']).dt.strftime('%H:%M:%S').values
for s in sigs:
    print('  ', s[:8] if len(s) >= 8 else s)
print('>>> 若>0: 实时信号逻辑正常, 之前零信号是首扫抑制/重启发动掩盖; 若=0: 实时路径仍有bug')
