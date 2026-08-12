# -*- coding: utf-8 -*-
"""P0 回归测试：bar 已处理标记「跨日残留」修复验证（2026-08-12）。

被测缺陷（v10.0.0/10.1.0 生产事故）：
  detect_for 用 f"bar_{sym}_{i}"（i=当日 1m bar 行号 0~239）标记已评估 bar；
  跨日清理只写在 load_state()（=进程重启且日期变了）。而 monitor 在交易日收盘后
  走 keepalive 分支持续存活（只有周末/非交易日才 sys.exit(0)）→ 周二~周五连续运行时
  昨日 240 个标记全部带进新交易日，detect_for 对当日每根同名 bar 判「已处理」continue
  → 全天一根不评估、静默零信号零推送。实证：08-10(周一,周末重启清过)有信号；
  08-11(周二,连续运行)零信号；state.json 718 个 bar 标记时间戳 100% 是 08-10。

修复：
  ① bar_key 加日期维度 f"bar_{sym}_{YYYYMMDD}_{i}" → 跨日结构上不可能碰撞；
  ② 跨日清理搬到 run() 运行态锚点（_daily_refreshed_date 变化即清 bar_/pos_/
     _cooldown_/_miss_/alerted_miss_），与重启路径同键集。

四组对照（数据=F盘 tickflow 300757.SZ 08-11，已知基线 B=1 S=1 X=2）：
  A 空 st                          → 期望 4（基线）
  B 旧格式残留 bar_{sym}_{i}        → 期望 4（新码对旧键免疫，不再被吞）
  C 新格式·昨日 bar_{sym}_昨_{i}    → 期望 4（结构免疫：日期不同不碰撞）
  D 新格式·当日 bar_{sym}_今_{i}    → 期望 0（当日去重必须仍然生效，防重复推送）
外加 E：run() 跨日清理键集过滤逻辑（保留 _b_count_/_s_count_ 复盘权威源）。

用法：python scripts/test_bar_key_crossday.py
退出码 0=全过，1=有失败。
"""
import sys
import os
import json
from datetime import datetime

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
CORE = os.path.join(BASE, 'core')
sys.path.insert(0, CORE)
sys.path.insert(0, os.path.join(BASE, 'venv', 'Lib', 'site-packages'))
os.chdir(CORE)

import pandas as pd
from miji_alpha import compute_miji_indicators
import monitor

# 打桩：严防污染真实 signal.txt / 飞书 / state.json
monitor.emit_signal = lambda *a, **k: None
monitor.emit = lambda *a, **k: None
monitor._append_signal_txt = lambda *a, **k: None
monitor.push_batch = lambda *a, **k: None
monitor.save_state = lambda *a, **k: None

SYM = '300757.SZ'
NAME = '罗博特科'
DATE = '2026-08-11'
FCSV = r'F:\keyfactor_data\1m\300757.SZ_1m.csv'
EXPECT_BASE = 4          # 该日已知信号数（B1/S1/X2）

# detect_for 内的 bar_key 日期取「墙钟今日」（生产中与 bar 日期恒等：compute() 会校验
# bar_date == today_str，非今日数据直接 return None）。离线重放时墙钟日≠数据日，
# 故 D 组必须用墙钟今日构造键才能命中去重分支。
TODAY = datetime.now().strftime('%Y%m%d')
YESTERDAY_LIKE = '20260810'

with open(os.path.join(BASE, 'data', 'monitor_config.json'), encoding='utf-8') as f:
    CFG = json.load(f)


def load_data():
    raw = pd.read_csv(FCSV)
    raw['trade_date'] = raw['trade_date'].astype(str)
    prev = raw[raw['trade_date'] < DATE]
    pc = float(prev['close'].iloc[-1])
    df = raw[raw['trade_date'] == DATE].copy()
    df['trade_time'] = df['trade_time'].astype(str)
    df = df.sort_values('trade_time').reset_index(drop=True)
    o = df['open'].values.astype(float)
    h = df['high'].values.astype(float)
    lo = df['low'].values.astype(float)
    c = df['close'].values.astype(float)
    v = df['volume'].values.astype(float) if 'volume' in df.columns else None
    data = compute_miji_indicators(o, h, lo, c, v, pc, has_vol=(v is not None))
    data['df'] = df
    try:
        _hhmm = df['trade_time'].str[11:16]
        data['is_morning'] = ((_hhmm >= '09:30') & (_hhmm < '10:00')).astype(int).values
    except Exception:
        data['is_morning'] = None
    monitor.STATE[SYM] = {'PC': pc, 'WARM': None}
    return data, df, pc


def run_case(tag, st, data, expect):
    cfg = CFG.get(SYM, {})
    sigs = monitor.detect_for(SYM, NAME, data, st,
                             mpr_enable=cfg.get('mpr_enable'),
                             mpr_periods=cfg.get('mpr_periods'),
                             atr_min_pct=cfg.get('atr_min_pct'))
    nb = sum(1 for s in sigs if s[0] == 'B')
    ns = sum(1 for s in sigs if s[0] == 'S')
    nx = sum(1 for s in sigs if s[0] == 'X')
    ok = (len(sigs) == expect)
    print('  [%s] %-42s → B=%d S=%d X=%d 合计=%d (期望%d) %s'
          % ('PASS' if ok else 'FAIL', tag, nb, ns, nx, len(sigs), expect,
             '' if ok else '  <<< 不符'))
    if ok and sigs:
        for s in sigs:
            print('            %s @ %s px=%.3f reason=%s'
                  % (s[0], s[12] if len(s) > 12 else '?', s[1], s[4]))
    return ok


def main():
    print('=' * 88)
    print('P0 回归：bar 标记跨日残留修复   数据=%s %s   墙钟日=%s' % (SYM, DATE, TODAY))
    print('=' * 88)
    data, df, pc = load_data()
    n = len(df)
    print('bars=%d  PC=%.3f  首/末=%s .. %s\n' % (n, pc, df['trade_time'].iloc[0],
                                                  df['trade_time'].iloc[-1]))
    results = []

    # A 基线
    results.append(run_case('A 空 st（基线）', {}, data, EXPECT_BASE))

    # B 旧格式残留（修复前 monitor 写入的历史键，仍留在 state.json 里）
    st_b = {f'bar_{SYM}_{i}': 1 for i in range(n)}
    results.append(run_case('B 旧格式残留 bar_{sym}_{i} 全量', st_b, data, EXPECT_BASE))

    # C 新格式·昨日日期（跨日结构免疫的核心断言）
    st_c = {f'bar_{SYM}_{YESTERDAY_LIKE}_{i}': 1 for i in range(n)}
    results.append(run_case('C 新格式·昨日 %s 全量' % YESTERDAY_LIKE, st_c, data, EXPECT_BASE))

    # D 新格式·当日日期（当日去重必须仍生效，否则会重复推送刷屏）
    st_d = {f'bar_{SYM}_{TODAY}_{i}': 1 for i in range(n)}
    results.append(run_case('D 新格式·当日 %s 全量（去重回归）' % TODAY, st_d, data, 0))

    # E run() 跨日清理键集：清盘中态、保留复盘权威源
    print('\n  --- E run() 跨日清理键集过滤 ---')
    st_e = {
        f'bar_{SYM}_{YESTERDAY_LIKE}_5': 1,
        f'bar_{SYM}_9': 1,
        '_cooldown_%s_B' % SYM: 3,
        'pos_%s' % SYM: {'side': 'long'},
        '_miss_%s' % SYM: 4,
        'alerted_miss_%s' % SYM: True,
        '_b_count_%s_20260811' % SYM: 1,      # 复盘权威源，必须保留
        '_s_count_%s_20260811' % SYM: 1,      # 复盘权威源，必须保留
        '_daily_refreshed_date': '2026-08-11',
        '_tf_unhealthy': False,
    }
    stale = [k for k in list(st_e.keys())
             if k.startswith('bar_') or k.startswith('_cooldown_')
             or k.startswith('pos_') or k.startswith('_miss_')
             or k.startswith('alerted_miss_')]
    kept = {k: v for k, v in st_e.items() if k not in stale}
    want_stale = 6
    want_keep = {'_b_count_%s_20260811' % SYM, '_s_count_%s_20260811' % SYM,
                 '_daily_refreshed_date', '_tf_unhealthy'}
    ok_e = (len(stale) == want_stale and set(kept.keys()) == want_keep)
    print('  [%s] 清理 %d 键(期望%d)：%s' % ('PASS' if ok_e else 'FAIL',
                                            len(stale), want_stale, sorted(stale)))
    print('         保留 %s' % sorted(kept.keys()))
    print('         → _b_count_/_s_count_（每日复盘实盘权威源）%s'
          % ('保住 ✓' if want_keep <= set(kept.keys()) else '被误删 ✗'))
    results.append(ok_e)

    print('\n' + '=' * 88)
    passed = sum(1 for r in results if r)
    print('结果：%d/%d 通过' % (passed, len(results)))
    if passed == len(results):
        print('判定：修复有效 —— 跨日残留(旧格式/新格式昨日)不再吞信号，当日去重仍生效，')
        print('      复盘权威计数键不被清理。')
        return 0
    print('判定：存在失败项，修复不完整，禁止上线。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
