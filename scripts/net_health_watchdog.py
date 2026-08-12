#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
net_health_watchdog.py — tpoint 网络连通性异常看门狗（独立于 monitor 扫描周期）

背景
----
2026-08-06 故障复盘：monitor 进程存活、心跳正常，但本机网关 DNS(192.168.110.1)
转发器宕掉数小时，导致所有行情/飞书域名解析失败 -> 整日零信号且零信号告警自身也发不出。
现有 monitor 的"静默零信号告警"与 alert_engine 的"心跳判活"都看不到「网络层」故障。

本看门狗职责（类似 tpoint 心跳异常，但下沉到网络层）：
1. 周期探测 DNS 解析能力（系统解析器）+ 公网 DNS 可达性，区分：
   - HEALTHY            : 系统 DNS 正常
   - DNS_GATEWAY_DOWN   : 系统 DNS 全挂，但公网可达 -> 切公共 DNS 即可恢复（8/6 即此）
   - CONNECTIVITY_DOWN  : 系统 DNS 与公网 DNS 都不可达 -> 完全断网
2. 状态变化（健康<->异常）写本地 state 文件 + 日志（不依赖网络），并在线恢复后广播失明窗口。
3. 异常告警走「绕过网关 DNS」的直连通道（解析飞书域名到公网 DNS 拿 IP，再 POST 到该 IP + Host 头，
   关闭证书校验），确保即使网关 DNS 死了也能把告警送达；失败再回退 notify.py。

仅用标准库。建议由 Windows 计划任务每 5 分钟触发一次（见 run_net_watchdog.bat）。

注意：CONNECTIVITY_DOWN（完全断网）时任何远程告警都无法送达，本看门狗只保证本地记录 +
恢复后补播；DNS_GATEWAY_DOWN 时绕过通道可正常送达。
"""
import os
import re
import json
import ssl
import time
import socket
import subprocess
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
STATE_FILE = os.path.join(DATA_DIR, "net_health.json")
WATCHDOG_LOG = os.path.join(LOG_DIR, "net_watchdog.log")

# 关键域名：行情源(gtimg/新浪) + 告警通道(feishu)
PROBE_DOMAINS = ["web.ifzq.gtimg.cn", "hq.sinajs.cn", "open.feishu.cn"]
# 公网 DNS（用于区分 网关DNS挂 vs 完全断网，并作为绕过通道的解析源）
PUBLIC_DNS = ["223.5.5.5", "119.29.29.29", "8.8.8.8"]

# 告警落地群（全局通知 webhook：任务状态/卡死/基础设施告警；与数据质量哨兵一致）
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/b4eba7a9-0504-4bd6-8aa3-a60fc8154103"
FEISHU_HOST = "open.feishu.cn"
# notify.py 回退路径（仅在绕过通道也失败时）
NOTIFY_PY = r"C:\Users\YZP\.workbuddy\notify.py"

STATUS_HEALTHY = "healthy"
STATUS_DNS_DOWN = "dns_gateway_down"
STATUS_CONN_DOWN = "connectivity_down"

_last_status = None  # 模块级，main 中从 state 载入


# ---------------- 解析/探测 ----------------
def system_resolve(domain, timeout=5):
    """用系统解析器解析（走网关 DNS）。成功返回 IP，失败返回 None。"""
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return None


def resolve_via_public(domain, dns_ip, timeout=8):
    """经指定公网 DNS 解析（绕过系统/网关 DNS）。返回 IP 或 None。"""
    try:
        r = subprocess.run(
            ["nslookup", domain, dns_ip],
            capture_output=True, timeout=timeout,
        )
        # Windows nslookup 输出为系统代码页(GBK)，避免 text=True 解码崩溃
        out = r.stdout.decode("utf-8", "ignore") if r.stdout else ""
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        low = line.lower()
        # 答案行含 address/Addresses；跳过 Server 自身地址行（含 dns_ip）
        if "address" in low and dns_ip not in line:
            m_ip = re.search(r"\d{1,3}(\.\d{1,3}){3}", line)
            if m_ip:
                return m_ip.group()
    return None


def public_resolve_any(domain):
    """依次尝试多个公网 DNS 解析，返回第一个成功 IP 或 None。"""
    for dns in PUBLIC_DNS:
        ip = resolve_via_public(domain, dns)
        if ip:
            return ip
    return None


def classify():
    """返回 (status, detail)。"""
    sys_ok = [d for d in PROBE_DOMAINS if system_resolve(d)]
    pub_ok = public_resolve_any(PROBE_DOMAINS[0]) is not None
    if sys_ok:
        return STATUS_HEALTHY, {
            "system_resolved": sys_ok,
            "system_failed": [d for d in PROBE_DOMAINS if d not in sys_ok],
            "public_dns_reachable": pub_ok,
        }
    if pub_ok:
        # 系统 DNS 失败但公网可达 -> 网关 DNS 转发器故障
        return STATUS_DNS_DOWN, {
            "system_resolved": [],
            "system_failed": PROBE_DOMAINS,
            "public_dns_reachable": True,
            "suspect": "网关 DNS(192.168.110.1) 转发器无响应；建议本机网卡改配公共 DNS 主用",
        }
    return STATUS_CONN_DOWN, {
        "system_resolved": [],
        "system_failed": PROBE_DOMAINS,
        "public_dns_reachable": False,
        "suspect": "完全断网（系统 DNS 与公网 DNS 均不可达）",
    }


# ---------------- 状态/日志 ----------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_line(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


# ---------------- 告警（绕过网关 DNS） ----------------
def feishu_post_bypass(text):
    """绕过系统 DNS，直接经公网 DNS 解析飞书域名并 POST 到该 IP（关闭证书校验）。
    返回 True/False。"""
    hook_id = FEISHU_WEBHOOK.rsplit("/", 1)[1]
    path = f"/open-apis/bot/v2/hook/{hook_id}"
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for dns in PUBLIC_DNS:
        ip = resolve_via_public(FEISHU_HOST, dns)
        if not ip:
            continue
        try:
            req = urllib.request.Request(
                f"https://{ip}{path}",
                data=body,
                headers={"Host": FEISHU_HOST, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def push_alert(text):
    """优先走绕过 DNS 通道；失败回退 notify.py（仍可能受 DNS 影响）。返回是否送达。"""
    if feishu_post_bypass(text):
        return True
    try:
        import sys
        p = subprocess.run(
            ["C:/Users/YZP/.workbuddy/binaries/python/versions/3.13.12/python.exe",
             NOTIFY_PY],
            input=text, capture_output=True, text=True, timeout=20,
        )
        return "FEISHU_PUSH_OK" in (p.stdout or "")
    except Exception:
        return False


# ---------------- 主流程 ----------------
def main():
    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    status, detail = classify()

    state = load_state()
    prev = state.get("status", STATUS_HEALTHY)
    last_unhealthy_start = state.get("last_unhealthy_start")

    # 心跳行（每次都写，供外部观测看门狗存活）
    log_line(f"net_check status={status} probes={len(detail.get('system_resolved', []))}/{len(PROBE_DOMAINS)} "
             f"pub_dns={detail.get('public_dns_reachable')}")

    transition = (status != prev)
    alert_sent = False
    blind_min = None

    if status != STATUS_HEALTHY:
        if prev == STATUS_HEALTHY or last_unhealthy_start is None:
            # 进入异常（或首次发现异常）
            last_unhealthy_start = now_iso
            label = {
                STATUS_DNS_DOWN: "⚠️【tpoint 网络看门狗】网关DNS转发器故障",
                STATUS_CONN_DOWN: "⚠️【tpoint 网络看门狗】网络完全中断",
            }[status]
            msg = (f"{label}\n"
                   f"时间：{now_iso}\n"
                   f"现象：系统 DNS 解析 {PROBE_DOMAINS} 全部失败"
                   f"（{'公网可达，疑似网关192.168.110.1 DNS宕' if status == STATUS_DNS_DOWN else '公网亦不可达，完全断网'}）。\n"
                   f"影响：monitor 取不到行情 -> 整日零信号；原零信号告警自身也发不出。\n"
                   f"建议：本机网卡 DNS 改配公共 DNS 主用（223.5.5.5 / 119.29.29.29）；"
                   f"本看门狗已尝试绕过网关DNS直报。\n"
                   f"（若本告警延迟到达，说明故障期间推送链路同样受影响）")
            alert_sent = push_alert(msg)
            log_line(f"ALERT {status} sent={alert_sent}")
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        if prev != STATUS_HEALTHY and last_unhealthy_start:
            # 恢复
            try:
                t0 = datetime.strptime(last_unhealthy_start, "%Y-%m-%dT%H:%M:%S")
                blind_min = int((now - t0).total_seconds() // 60)
            except Exception:
                blind_min = None
            msg = (f"✅【tpoint 网络看门狗】网络已恢复\n"
                   f"恢复时间：{now_iso}\n"
                   f"失明窗口：约 {blind_min} 分钟（自 {last_unhealthy_start}）\n"
                   f"类型：{prev} -> healthy\n"
                   f"建议：趁恢复窗口把本机网卡 DNS 改配公共 DNS 主用，避免复发。")
            alert_sent = push_alert(msg)
            log_line(f"RECOVER from {prev} blind_min={blind_min} sent={alert_sent}")
            last_unhealthy_start = None
        state["consecutive_failures"] = 0

    state.update({
        "status": status,
        "last_check": now_iso,
        "last_healthy": now_iso if status == STATUS_HEALTHY else state.get("last_healthy"),
        "last_unhealthy": now_iso if status != STATUS_HEALTHY else state.get("last_unhealthy"),
        "last_unhealthy_start": last_unhealthy_start,
        "last_blind_min": blind_min,
        "detail": detail,
    })
    save_state(state)

    # stdout 摘要（便于计划任务日志/手动运行查看）
    print(f"[net_health_watchdog] {now_iso} status={status} "
          f"transition={transition} alert_sent={alert_sent} blind_min={blind_min}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
