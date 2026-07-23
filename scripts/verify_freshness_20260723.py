#!/usr/bin/env python3
"""
verify_freshness_20260723.py — 离线验证「新鲜度校验 + 强制轮换 + 告警」根因修复。

纯单元验证，不触网、不真发飞书：
  1) _bar_freshness_seconds：新鲜/陈旧 df 的滞后秒数计算
  2) _handle_staleness：
       - 新鲜(交易时段) → 不判陈旧、不轮换、不告警、数据保留
       - 陈旧第1轮     → 判陈旧、剔除数据、计数=1、未到轮换/告警阈值
       - 陈旧第2轮     → 触发强制重连(tf.reconnect) + 推告警
       - 午休(盘外)    → 即使陈旧也不判陈旧(无盘外误杀)、数据保留
       - 持续冻结多轮 → 触发"持续陈旧"告警(区分临时/持续)
  3) 验证确实未向真实飞书 webhook 发任何请求（_send_alert 被替换为录制器）

运行：core/venv/Scripts/python.exe scripts/verify_freshness_20260723.py
"""
import os
import sys
import json

# 把 core/ 加入 sys.path 以便 import monitor / datasource
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(BASE, 'core')
sys.path.insert(0, CORE)

from datetime import datetime, timedelta, timezone

import pandas as pd

# ---- 导入被测模块（导入仅定义函数/常量，不触发 run()）----
import datasource  # noqa: E402
import monitor     # noqa: E402

CST = timezone(timedelta(hours=8))


# ============ 录制器（替代真实飞书推送 / 重连）============
class Recorder:
    def __init__(self):
        self.alerts = []
        self.reconnects = 0
    def send_alert(self, text):
        self.alerts.append(text)
    def reconnect(self):
        self.reconnects += 1


def build_df(lag_s):
    """构造含 N 根 1min 的 df，最新一根距 now 滞后 lag_s 秒（naive 本地时间）。"""
    now = datetime.now()
    rows = []
    base = now - timedelta(seconds=lag_s) - timedelta(minutes=10)
    for i in range(11):
        t = base + timedelta(minutes=i)
        rows.append({'trade_time': t, 'trade_date': t.strftime('%Y-%m-%d'),
                     'close': 100.0 + i})
    df = pd.DataFrame(rows)
    return df


def t_aware(h=14, m=0):
    return datetime(2026, 7, 23, h, m, 0, tzinfo=CST)


PASS = 0
FAIL = 0
def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


print("="*60)
print("1) _bar_freshness_seconds")
df_fresh = build_df(10)
df_stale = build_df(600)
fresh_s = monitor._bar_freshness_seconds(df_fresh)
stale_s = monitor._bar_freshness_seconds(df_stale)
check("_bar_freshness_seconds 新鲜(~10s)<300", fresh_s is not None and 0 <= fresh_s < 300, f"fresh_s={fresh_s}")
check("_bar_freshness_seconds 陈旧(~600s)>300", stale_s is not None and stale_s > 300, f"stale_s={stale_s}")
check("_bar_freshness_seconds 空df→None", monitor._bar_freshness_seconds(pd.DataFrame()) is None)

print("="*60)
print("2) _handle_staleness — 新鲜(交易时段) 不应判陈旧")
rec = Recorder()
monitor._send_alert = rec.send_alert
TARGETS = {'161129.SZ': '原油LOF', '688347.SH': '华虹'}
st = {}
sym_data = {'161129.SZ': {'_fresh_s': 10.0}, '688347.SH': {'_fresh_s': 12.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,0), True, rec)
check("新鲜→stale_syms 为空", stale == set())
check("新鲜→数据全部保留", set(sym_data2.keys()) == set(TARGETS))
check("新鲜→未触发重连", rec.reconnects == 0)
check("新鲜→未触发告警", len(rec.alerts) == 0)
check("新鲜→_stale_ 计数清零", all(st.get(f'_stale_{s}',0)==0 for s in TARGETS))

print("="*60)
print("3) _handle_staleness — 陈旧第1轮：判陈旧+剔除，但未到轮换/告警阈值")
rec = Recorder()
monitor._send_alert = rec.send_alert
st = {}
sym_data = {'161129.SZ': {'_fresh_s': 600.0}, '688347.SH': {'_fresh_s': 11.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,0), True, rec)
check("陈旧→stale_syms={161129.SZ}", stale == {'161129.SZ'}, f"stale={stale}")
check("陈旧→冻结标的从 sym_data 剔除(不据冻结价出信号)", '161129.SZ' not in sym_data2 and '688347.SH' in sym_data2)
check("陈旧第1轮→_stale_161129=1", st.get('_stale_161129.SZ') == 1)
check("陈旧第1轮→未触发重连(阈值2)", rec.reconnects == 0)
check("陈旧第1轮→未触发告警(阈值2)", len(rec.alerts) == 0)
check("陈旧→_miss_161129 不因剔除而误计缺数(置0)", st.get('_miss_161129.SZ') == 0)

print("="*60)
print("4) _handle_staleness — 陈旧第2轮：触发强制重连 + 告警")
rec = Recorder()
monitor._send_alert = rec.send_alert
st = {'_stale_161129.SZ': 1}  # 延续第1轮状态
sym_data = {'161129.SZ': {'_fresh_s': 605.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,1), True, rec)
check("陈旧第2轮→_stale_=2", st.get('_stale_161129.SZ') == 2)
check("陈旧第2轮→触发强制重连(tf.reconnect)", rec.reconnects == 1, f"reconnects={rec.reconnects}")
check("陈旧第2轮→触发告警", len(rec.alerts) == 1, f"alerts={rec.alerts}")
check("告警文案含'行情陈旧'", '行情陈旧' in rec.alerts[0], rec.alerts[0] if rec.alerts else '')
check("告警文案含滞后秒数", '605' in rec.alerts[0] or '约' in rec.alerts[0])

print("="*60)
print("5) _handle_staleness — 午休(盘外)：即使陈旧也不判陈旧(无盘外误杀)")
rec = Recorder()
monitor._send_alert = rec.send_alert
st = {}
# 午休 12:00，最新 bar 滞后 600s(11:30 收盘) 属正常
sym_data = {'161129.SZ': {'_fresh_s': 600.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(12,0), False, rec)
check("午休→stale_syms 为空", stale == set())
check("午休→冻结数据仍保留(不剔除)", '161129.SZ' in sym_data2)
check("午休→未触发重连", rec.reconnects == 0)
check("午休→未触发告警", len(rec.alerts) == 0)

print("="*60)
print("6) _handle_staleness — 持续冻结多轮：触发'持续陈旧'告警")
rec = Recorder()
monitor._send_alert = rec.send_alert
# 模拟已连续 40 轮陈旧(阈值 persistent = RECONNECT_ROUNDS*20 = 40)
st = {'_stale_161129.SZ': 40, '_stale_rc_at_161129.SZ': (t_aware(14,0).timestamp() - 120)}
sym_data = {'161129.SZ': {'_fresh_s': 700.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,5), True, rec)
check("持续冻结→_stale_=41", st.get('_stale_161129.SZ') == 41)
check("持续冻结→触发告警", len(rec.alerts) == 1)
check("持续冻结→告警文案含'持续陈旧'", '持续陈旧' in rec.alerts[0], rec.alerts[0] if rec.alerts else '')

print("="*60)
print("7) 轮换冷却：连续陈旧但冷却未到→不重复狂重连")
rec = Recorder()
monitor._send_alert = rec.send_alert
# _stale_=2 但 _stale_rc_at 设为'刚刚'(冷却内) → 不应重连
st = {'_stale_161129.SZ': 2, '_stale_rc_at_161129.SZ': t_aware(14,0).timestamp()}
sym_data = {'161129.SZ': {'_fresh_s': 605.0}}
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,0), True, rec)
check("冷却内→不重复重连", rec.reconnects == 0, f"reconnects={rec.reconnects}")
check("冷却内→仍告警(告警与轮换独立限频)", len(rec.alerts) == 1)

print("="*60)
print("8) 恢复新鲜：陈旧计数清零")
rec = Recorder()
monitor._send_alert = rec.send_alert
st = {'_stale_161129.SZ': 5, 'alerted_stale_161129.SZ': 5}
sym_data = {'161129.SZ': {'_fresh_s': 8.0}}  # 数据恢复新鲜
sym_data2, stale = monitor._handle_staleness(sym_data, TARGETS, st, t_aware(14,0), True, rec)
check("恢复新鲜→_stale_ 清零", st.get('_stale_161129.SZ') == 0)
check("恢复新鲜→无告警", len(rec.alerts) == 0)

print("="*60)
print("9) datasource._server_ok 新鲜度门控(单元)")
now_in = pd.Timestamp('2026-07-23 14:00:00')
class MockClient:
    def __init__(self, stale_dt=None, empty=False):
        self.stale_dt = stale_dt; self.empty = empty
    def bars(self, symbol, frequency, offset, market=0):
        if frequency == 9:
            if self.empty: return None
            return pd.DataFrame({'datetime': ['2026-07-22 15:00:00']})
        else:
            if self.stale_dt is None:
                return pd.DataFrame({'datetime': [now_in.strftime('%Y-%m-%d %H:%M:%S')]})
            return pd.DataFrame({'datetime': [self.stale_dt.strftime('%Y-%m-%d %H:%M:%S')]})
ok_fresh, _ = datasource._server_ok(MockClient(), now=now_in)
ok_stale, r_stale = datasource._server_ok(MockClient(stale_dt=now_in - timedelta(seconds=600)), now=now_in)
ok_off, _ = datasource._server_ok(MockClient(stale_dt=now_in - timedelta(seconds=600)), now=None)
ok_empty, r_empty = datasource._server_ok(MockClient(empty=True), now=now_in)
check("_server_ok 新鲜→True", ok_fresh is True)
check("_server_ok 陈旧(盘内)→False(stale)", ok_stale is False and 'stale' in r_stale, r_stale)
check("_server_ok 陈旧(盘外)→True(跳过校验)", ok_off is True)
check("_server_ok 空→False(empty)", ok_empty is False and 'empty' in r_empty, r_empty)

print("="*60)
print(f"结果: PASS={PASS}  FAIL={FAIL}")
if FAIL == 0:
    print("🎉 全部通过：新鲜度校验+强制轮换+告警 逻辑正确，且未触网/未真发飞书。")
    sys.exit(0)
else:
    print("⚠️ 存在失败用例，请检查。")
    sys.exit(1)
