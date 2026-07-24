#!/usr/bin/env python3
"""watchlist.json 归一化加载 + 停牌(suspended)过滤。

两种文件格式兼容：
  旧格式: {"161129.SZ": "原油LOF易方达"}
  新格式: {"161129.SZ": {"name": "原油LOF易方达",
                         "status": "active"|"suspended",
                         "suspended_until": "2026-07-24T10:30:00"|null}}

设计要点：
  - monitor 扫描前用 active_symbols(entries) 过滤掉停牌标的：不拉数据、不计 err_count、
    不累加 miss、不推"数据源中断"告警。suspended_until 过期自动视为复牌。
  - alert_engine 只消费 metrics.json 的聚合 errors（monitor 已不把停牌标的计入 err_count），
    因此无需在 alert_engine 内逐标的改动——"跳过 suspended 的 err_count"在源头已成立。
"""
import json
import os
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


def _parse_dt(s):
    """ISO 时间字符串 -> CST datetime；无法解析返回 None。"""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=CST)
    try:
        s2 = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def normalize_entry(code, val):
    """单条 watchlist 项 -> 归一化 {'name','status','suspended_until'(datetime|None)}。"""
    if isinstance(val, str):
        return {"name": val, "status": "active", "suspended_until": None}
    if isinstance(val, dict):
        name = val.get("name") or val.get("code") or code
        status = val.get("status", "active")
        if status not in ("active", "suspended"):
            status = "active"
        return {
            "name": name,
            "status": status,
            "suspended_until": _parse_dt(val.get("suspended_until")),
        }
    return {"name": str(code), "status": "active", "suspended_until": None}


def load_raw(path):
    """读原始 watchlist.json（dict）。文件缺失/空/损坏返回 {}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def load_targets(path):
    """返回 {code: name}（归一化名字），与旧格式契约一致——下游 TARGETS[sym] 用法不变。"""
    raw = load_raw(path)
    return {c: normalize_entry(c, v)["name"] for c, v in raw.items()}


def load_entries(path):
    """返回 {code: 归一化 entry}。"""
    raw = load_raw(path)
    return {c: normalize_entry(c, v) for c, v in raw.items()}


def is_suspended(entry, now=None):
    """True 表示该标的应被过滤：status=suspended 且 (无期限 或 期限未到)。
    suspended_until 在过去 -> 自动视为已复牌 -> 返回 False。"""
    if entry is None:
        return False
    if entry.get("status") != "suspended":
        return False
    su = entry.get("suspended_until")
    if su is None:
        return True
    if now is None:
        now = datetime.now(CST)
    return now < su


def active_symbols(entries, now=None):
    """返回非停牌标的 code 列表（顺序同 dict 插入序）。"""
    if now is None:
        now = datetime.now(CST)
    return [c for c, e in entries.items() if not is_suspended(e, now)]


def market_open(now=None):
    """真实开盘时段 (09:30-11:30 / 13:00-15:00, CST)。
    不含盘前 09:25-09:30 与午休，用于 miss 计数/告警门控，避免盘前无数据误报'数据源中断'。"""
    if now is None:
        now = datetime.now(CST)
    t = now.time()
    morning = t >= t.replace(hour=9, minute=30) and t <= t.replace(hour=11, minute=30)
    afternoon = t >= t.replace(hour=13, minute=0) and t <= t.replace(hour=15, minute=0)
    return morning or afternoon
