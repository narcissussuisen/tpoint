#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Forensic parser: reconstruct real Feishu push timeline from monitor logs.
Handles mixed GBK/UTF-8 logs. Maps each push to (timestamp, symbol, type, code).
"""
import os, re, json, glob

LOGS = [
    r"C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console.log",
    r"C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console_new.log",
]

SYM_HINTS = [
    ("161129", ["原油", "LOF", "161129"]),
    ("688347", ["华虹", "688347"]),
    ("513310", ["中韩", "半导体", "513310"]),
]
TYPE_HINTS = [
    ("B", ["买入", "开多", "BUY", "'B'"]),
    ("S", ["卖出", "开空", "SELL", "'S'"]),
    ("X", ["出场", "止损", "止盈", "平仓", "平多", "回补", "STOP", "TRAIL", "'X'"]),
]

def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()

def to_text(b):
    # try utf-8 then gbk
    for enc in ('utf-8', 'gbk'):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode('utf-8', errors='replace')

def find_symbol(payload):
    for sym, hints in SYM_HINTS:
        for h in hints:
            if h in payload:
                return sym
    return "??"

def find_type(payload):
    # order matters: B/S before X fallback
    res = []
    if "买入" in payload or "BUY" in payload or "'B'" in payload:
        res.append("B")
    if "卖出" in payload or "SELL" in payload or "'S'" in payload:
        res.append("S")
    if any(k in payload for k in ["出场", "止损", "止盈", "平仓", "平多", "回补", "STOP", "TRAIL", "'X'"]):
        res.append("X")
    if not res:
        return "?"
    return "/".join(res)

def parse_log(path):
    text = to_text(read_bytes(path))
    lines = text.splitlines()
    events = []
    i = 0
    n = len(lines)
    re_batch = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]\s*本轮信号\s*(\d+)\s*条\s*→\s*推送")
    re_prep = re.compile(r"PUSH\(card\)\s*准备")
    re_resp = re.compile(r"PUSH\s*响应:\s*status=(\d+)\s*code=(\d+)\s*msg=(\S+)")
    re_fail = re.compile(r"PUSH\s*失败")
    while i < n:
        m = re_batch.search(lines[i])
        if m:
            ts = m.group(1)
            cnt = int(m.group(2))
            # collect next cnt (准备, 响应) pairs
            j = i + 1
            pairs = []
            while len(pairs) < cnt and j < n:
                if re_prep.search(lines[j]):
                    payload = lines[j]
                    # next non-empty line should be response
                    k = j + 1
                    while k < n and lines[k].strip() == "":
                        k += 1
                    if k < n:
                        rm = re_resp.search(lines[k])
                        if rm:
                            code = int(rm.group(2))
                            msg = rm.group(3)
                            pairs.append((payload, code, msg))
                            j = k + 1
                            continue
                    # response not found inline; record unknown
                    pairs.append((payload, -1, "?"))
                    j += 1
                    continue
                # if we hit another batch or end, stop
                if re_batch.search(lines[j]):
                    break
                j += 1
            for (payload, code, msg) in pairs:
                events.append({
                    "ts": ts,
                    "sym": find_symbol(payload),
                    "type": find_type(payload),
                    "code": code,
                    "msg": msg,
                    "ok": (code == 0),
                    "log": os.path.basename(path),
                })
            i = j
            continue
        i += 1
    return events

def main():
    all_events = []
    for p in LOGS:
        if os.path.exists(p):
            evs = parse_log(p)
            print(f"[LOG] {os.path.basename(p)}: {len(evs)} push events")
            all_events.extend(evs)
    # sort by ts
    all_events.sort(key=lambda e: e["ts"])
    # summary
    print("\n=== PUSH TIMELINE (real, from logs) ===")
    fail = [e for e in all_events if not e["ok"]]
    print(f"TOTAL pushes logged: {len(all_events)} | FAILED(11232 etc): {len(fail)} | OK: {len(all_events)-len(fail)}")
    print("\n--- failed pushes ---")
    for e in fail:
        print(f"  {e['ts']} sym={e['sym']} type={e['type']} code={e['code']} log={e['log']}")
    # per symbol breakdown
    print("\n--- per symbol (all pushes) ---")
    from collections import defaultdict
    bysym = defaultdict(lambda: {"ok":0,"fail":0,"types":defaultdict(int)})
    for e in all_events:
        d = bysym[e["sym"]]
        if e["ok"]: d["ok"]+=1
        else: d["fail"]+=1
        d["types"][e["type"]]+=1
    for sym, d in bysym.items():
        print(f"  {sym}: ok={d['ok']} fail={d['fail']} types={dict(d['types'])}")
    # save json
    out = r"C:\Users\YZP\WorkBuddy\Claw\tpoint\output\_push_timeline_2026-07-22.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_events, f, ensure_ascii=False, indent=2)
    print(f"\n[saved] {out}")

if __name__ == "__main__":
    main()
