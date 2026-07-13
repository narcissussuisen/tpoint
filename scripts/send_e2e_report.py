#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 v9_e2e_report.json 的完整步骤详情以 markdown 形式发送到飞书。"""
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import feishu_alert


def main():
    cfg_path = os.path.join(BASE_DIR, 'config', 'monitor_config.json')
    report_path = os.path.join(BASE_DIR, 'data', 'v9_e2e_report.json')

    with open(cfg_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    with open(report_path, 'r', encoding='utf-8') as f:
        report = json.load(f)

    lines = [
        f"## {report['title']}",
        f"**开始时间**: {report['start_time']}  ",
        f"**结束时间**: {report['end_time']}  ",
        f"**总耗时**: {report['total_cost_ms']} ms  ",
        f"**主机**: {report['environment']['hostname']}  ",
        f"**总体状态**: {'✅ PASS' if report['status'] == 'PASS' else '⚠️ ' + report['status']}",
        "",
        "### 各环节执行详情",
        "",
    ]

    status_emoji = {'PASS': '✅', 'FAIL': '🔴', 'WARN': '⚠️', 'ALERT': '🔶'}
    for step in report['steps']:
        emoji = status_emoji.get(step['status'], 'ℹ️')
        lines.append(f"**{step['step_id']} {step['name']}** {emoji} `{step['status']}`  ")
        lines.append(f"触发时间: {step['timestamp']} | 耗时: {step['detail'].get('cost_ms', '-')} ms  ")
        detail = step['detail']

        if step['step_id'] == 'P1':
            lines.append(f"- webhook 已配置: {detail.get('webhook_configured')}  ")
            lines.append(f"- 告警总开关: {detail.get('alerts_enabled')}  ")
            metrics = detail.get('metrics', {})
            lines.append(f"- 心跳年龄: {detail.get('heartbeat_age_s')} s  ")
            lines.append(f"- 服务状态: {detail.get('service_status')}  ")
            lines.append(f"- 标数量: {metrics.get('symbols')}, 信号数: {metrics.get('signals')}, 错误数: {metrics.get('errors')}  ")

        elif step['step_id'] == 'P2':
            lines.append(f"- symbols: {detail.get('symbols')}, signals: {detail.get('signals')}, errors: {detail.get('errors')}  ")
            lines.append(f"- scan_duration_s: {detail.get('scan_duration_s')}  ")
            lines.append(f"- data_lag_s: {detail.get('data_lag_s')}  ")

        elif step['step_id'] == 'P3':
            lines.append(f"- 评估规则数: {detail.get('rules_evaluated')}  ")
            lines.append(f"- 触发告警数: {detail.get('triggered_count')}  ")
            if detail.get('triggered_names'):
                lines.append(f"- 触发告警: {', '.join(detail['triggered_names'])}  ")

        elif step['step_id'] == 'P4':
            lines.append(f"- 发送告警数: {detail.get('alerts_sent')}  ")
            for r in detail.get('results', []):
                lines.append(f"- `{r['alert']}` -> {'成功' if r['ok'] else '失败'} ({r['info']})  ")

        lines.append("")

    if report['status'] == 'PASS':
        lines.append("---  ")
        lines.append("✅ v9 监控全链路模拟通过，系统当前运行正常，未触发任何真实告警。")
    else:
        lines.append("---  ")
        lines.append("⚠️ 模拟过程中存在异常，请检查上述 FAIL/WARN/ALERT 环节。")

    markdown = "\n".join(lines)

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "v9 监控全链路模拟报告"}
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": markdown}}
            ]
        }
    }

    webhook = cfg.get('feishu', {}).get('webhook_url', '')
    secret = cfg.get('feishu', {}).get('secret', '')

    ok, info = feishu_alert.send(webhook, {
        'name': 'v9 监控全链路模拟报告',
        'severity': 'normal',
        'rule': 'e2e_report',
        'value': report['status'],
        'threshold': 'PASS',
        'description': markdown,
        'trigger_time': report['end_time'],
        'source': 'e2e-simulation',
    }, secret=secret)

    print(f"Feishu send: ok={ok}, info={info}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
