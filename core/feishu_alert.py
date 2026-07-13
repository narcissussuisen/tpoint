#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书交互卡片告警发送（v9 监控系统）。

特性：
  - 三档严重等级卡片模板：普通(blue) / 警告(orange) / 严重(red)
  - 卡片固定含：告警名称、触发时间、当前指标值、阈值、严重等级、简要描述
  - 可选 HMAC 签名（机器人开启"签名校验"时使用）
  - dry-run 模式：仅打印 payload，不真正发请求（便于调试/无 webhook 时验证）
"""
import os, sys, json, time, base64, hashlib, hmac

try:
    import requests
except ImportError:
    requests = None

# 严重等级 → 卡片配色 / 图标 / 中文标签
SEVERITY = {
    'normal':   {'color': 'blue',   'emoji': 'ℹ️', 'label': '普通'},
    'warning':  {'color': 'orange', 'emoji': '⚠️', 'label': '警告'},
    'critical': {'color': 'red',    'emoji': '🔴', 'label': '严重'},
}


def build_card(alert):
    """构造飞书 interactive 卡片。alert 字段见 v9_alert_engine.evaluate()。"""
    sev = alert.get('severity', 'normal')
    tpl = SEVERITY.get(sev, SEVERITY['normal'])
    color, emoji, label = tpl['color'], tpl['emoji'], tpl['label']

    title = f"{emoji} v9 监控告警 · {alert.get('name', '')}"
    md = lambda t: {"tag": "lark_md", "content": t}
    fields = [
        {"is_short": True, "text": md(f"**严重等级**\n{label}")},
        {"is_short": True, "text": md(f"**触发时间**\n{alert.get('trigger_time', '')}")},
        {"is_short": True, "text": md(f"**当前指标值**\n{alert.get('value', '')}")},
        {"is_short": True, "text": md(f"**阈值**\n{alert.get('threshold', '')}")},
        {"is_short": False, "text": md(f"**告警名称**\n{alert.get('name', '')}")},
    ]
    elements = [
        {"tag": "div", "fields": fields},
        {"tag": "div", "text": md(f"**简要描述**\n{alert.get('description', '')}")},
        {"tag": "hr"},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": f"v9 监控系统 · 规则: {alert.get('rule', '')} · {alert.get('source', 'monitor')}"}
        ]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": title[:100]},
            },
            "elements": elements,
        },
    }


def _sign(webhook, secret):
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode('utf-8')
    return timestamp, sign


def send(webhook, alert, secret=None, dry_run=False, timeout=5):
    """发送一条告警卡片。

    返回 (ok: bool, info: str)。
    - webhook 为空且非 dry_run：按 dry-run 处理并打印提示（避免无配置时崩溃）。
    - secret 非空：自动附加 timestamp + sign（签名校验）。
    """
    if not webhook:
        dry_run = True
    card = build_card(alert)
    payload = card
    if secret:
        ts, sign = _sign(webhook, secret)
        payload = dict(card, timestamp=ts, sign=sign)

    if dry_run:
        print("=== [DRY-RUN] Feishu card payload ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True, "dry-run"

    if requests is None:
        return False, "requests 未安装"
    try:
        r = requests.post(webhook, json=payload, timeout=timeout)
        resp = r.json()
        ok = (r.status_code == 200 and resp.get('code') == 0)
        return ok, f"status={r.status_code} code={resp.get('code')} msg={resp.get('msg')}"
    except Exception as e:
        return False, f"请求异常: {e}"


if __name__ == '__main__':
    # 演示三档卡片
    for sev in ('normal', 'warning', 'critical'):
        send(None, {
            'name': f'演示告警-{sev}', 'severity': sev,
            'trigger_time': '2026-07-09 09:30:00', 'value': '12.3 s',
            'threshold': '10 s', 'description': '这是一条用于验证卡片渲染的演示告警。',
            'rule': 'scan_duration_s', 'source': 'demo',
        }, dry_run=True)
