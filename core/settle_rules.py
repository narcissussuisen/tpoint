#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""settle_rules.py — T+0 / T+1 结算制度规则模块（2026-08-04 v9.4.0 拆分）

拆分动机：watchlist 混合两类标的，结算制度不同 → 做T规则必须分离：
  - T+0（LOF/ETF：161129.SZ 原油LOF、513310.SH 中韩半导体ETF）当日买入可当日卖出，
    可多次往返，无底仓前提，无印花税。
  - T+1（A股个股：300058/600570/688111）当日买入不可卖，做T必须依托底仓：
    正T = 先买（加仓）→ 卖出（卖的是底仓）；反T = 先卖（减底仓）→ 当日回补。
    惯例每方向每日至多 1 次完整往返（防过度交易+底仓磨损）。

三类差异的处理口径：
  ① 信号生成：共用 core/miji_alpha 单一信号源（铁律：同源不分叉），
     差异只在「信号是否允许推送」的结算闸门（本模块 filter_signals）。
  ② 调仓频率：T0 不限制往返次数（沿用 MAX_B/S_DAILY=12 与 COLDOWN_BARS=3）；
     T1 每方向每日 1 次完整往返（B启动 trip 与 S启动 trip 各一次），X 出场仅在持仓 trip 中放行。
  ③ 成交规则：T0 进出场均按信号即成交；T1 卖出数量≤底仓（监控层不掌握真实底仓，
     以「每方向一次往返」近似约束频率；底仓规模属用户侧管理）。
     成本模型沿用 exit_manager.cost_for_symbol（ETF/LOF 无印花，个股有印花），不在此重复。

开关（默认关，灰度纪律）：monitor_config.json 顶层 "_global": {"settle_split_enable": true}，
per-symbol 可用 "settle_mode": "T0"/"T1" 覆盖自动判定（1/5开头代码默认 T0，其余 T1）。
开关关闭时本模块完全不介入（生产行为与 v9.3.1 完全一致）。
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_FILE = os.path.join(ROOT, 'data', 'monitor_config.json')

# T1 频率约束：每方向每日最多 1 次完整往返（A股做T惯例，防底仓磨损）
T1_MAX_TRIPS_PER_DIR = 1

_cfg_cache = {'mtime': 0, 'raw': {}}


def _load_cfg():
    """带 mtime 缓存读 monitor_config.json（每轮调用也只会 1 次磁盘 IO/15s）。"""
    try:
        mt = os.path.getmtime(CFG_FILE)
        if mt != _cfg_cache['mtime']:
            with open(CFG_FILE, encoding='utf-8') as f:
                _cfg_cache['raw'] = json.load(f)
            _cfg_cache['mtime'] = mt
    except Exception:
        pass
    return _cfg_cache['raw']


def split_enabled():
    return bool((_load_cfg().get('_global') or {}).get('settle_split_enable'))


def get_settle_mode(sym):
    """T0/T1 判定：config per-symbol settle_mode 覆盖 > 代码前缀规则（1xx/5xx=LOF/ETF=T0）。"""
    ov = (_load_cfg().get(sym) or {}).get('settle_mode')
    if ov in ('T0', 'T1'):
        return ov
    code = sym.split('.')[0]
    return 'T0' if code.startswith(('1', '5')) else 'T1'


def filter_signals(sym, sigs, st, today_str, log=print):
    """T1 结算闸门：按当日 trip 状态机过滤信号。T0/开关关闭 → 原样返回。
    st 键（内存态，save_state 持久化）：
      _trip_open_{sym}      : None | 'long'(B启动正T中) | 'short'(S启动反T中)
      _t1_done_B_{sym}_{ymd}: 当日 B 启动已完成往返数
      _t1_done_S_{sym}_{ymd}: 当日 S 启动已完成往返数
    """
    if not sigs or not split_enabled() or get_settle_mode(sym) != 'T1':
        return sigs
    open_key = f'_trip_open_{sym}'
    done_b_key = f'_t1_done_B_{sym}_{today_str}'
    done_s_key = f'_t1_done_S_{sym}_{today_str}'
    open_trip = st.get(open_key)
    done_b = st.get(done_b_key, 0)
    done_s = st.get(done_s_key, 0)
    out = []
    for s in sigs:
        typ = s[0]
        if open_trip is None:
            if typ == 'B' and done_b < T1_MAX_TRIPS_PER_DIR:
                open_trip = 'long'
                out.append(s)
            elif typ == 'S' and done_s < T1_MAX_TRIPS_PER_DIR:
                open_trip = 'short'
                out.append(s)
            elif typ == 'X':
                log(f'  ⛔ T1闸门 {sym}: 无持仓trip的X信号丢弃')
            else:
                log(f'  ⛔ T1闸门 {sym}: {typ} 抑制（该方向当日{T1_MAX_TRIPS_PER_DIR}次往返已用完）')
        elif open_trip == 'long':
            if typ in ('S', 'X'):
                open_trip = None
                done_b += 1
                out.append(s)
            else:
                log(f'  ⛔ T1闸门 {sym}: 正T持仓中忽略同向B')
        else:  # short（反T卖空底仓，待回补）
            if typ == 'B':
                open_trip = None
                done_s += 1
                out.append(s)
            else:
                log(f'  ⛔ T1闸门 {sym}: 反T待回补中忽略{typ}')
    st[open_key] = open_trip
    st[done_b_key] = done_b
    st[done_s_key] = done_s
    return out
