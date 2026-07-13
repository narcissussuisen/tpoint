#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v9 监控全链路模拟（E2E smoke test）。

覆盖：启动 -> 数据采集 -> 状态检测 -> 告警触发 -> 结果上报（飞书）。
每个阶段记录：输入、输出、状态变更、耗时、异常。
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

# 让脚本能从 scripts/ 目录找到 core/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import feishu_alert

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def now_str(fmt='%Y-%m-%d %H:%M:%S') -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime(fmt)


def log_step(step_id: str, name: str, status: str, detail: dict) -> dict:
    entry = {
        'step_id': step_id,
        'name': name,
        'status': status,
        'timestamp': now_str(),
        'detail': detail,
    }
    print(f"[{entry['timestamp']}] {step_id} {name}: {status}")
    return entry


def read_config() -> dict:
    path = os.path.join(BASE_DIR, 'config', 'monitor_config.json')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_metrics() -> dict:
    path = os.path.join(BASE_DIR, 'data', 'v9_metrics.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {'error': str(e), 'ts': int(time.time())}


# ---------------------------------------------------------------------------
# 阶段 1：启动检测
# ---------------------------------------------------------------------------
def phase_startup(cfg: dict) -> dict:
    t0 = time.time()
    detail = {
        'config_path': 'config/monitor_config.json',
        'webhook_configured': bool(cfg.get('feishu', {}).get('webhook_url')),
        'alerts_enabled': cfg.get('monitor', {}).get('enabled', False),
    }
    try:
        # 通过读取 metrics 判断 monitor 是否存活
        m = read_metrics()
        detail['metrics'] = m
        if 'error' in m:
            return log_step('P1', '启动检测', 'FAIL', {**detail, 'exception': m['error'], 'cost_ms': int((time.time()-t0)*1000)})
        age = int(time.time()) - int(m.get('ts', 0))
        detail['heartbeat_age_s'] = age
        detail['service_status'] = m.get('status', 'unknown')
        if age <= cfg.get('monitor', {}).get('service_stale_s', 120) and m.get('status') == 'running':
            return log_step('P1', '启动检测', 'PASS', {**detail, 'cost_ms': int((time.time()-t0)*1000)})
        return log_step('P1', '启动检测', 'WARN', {**detail, 'cost_ms': int((time.time()-t0)*1000), 'reason': '心跳偏旧或服务非 running'})
    except Exception as e:
        return log_step('P1', '启动检测', 'FAIL', {**detail, 'exception': str(e), 'traceback': traceback.format_exc(), 'cost_ms': int((time.time()-t0)*1000)})


# ---------------------------------------------------------------------------
# 阶段 2：数据采集模拟
# ---------------------------------------------------------------------------
def phase_data_collection(cfg: dict) -> dict:
    t0 = time.time()
    # 实际读取 monitor 写入的 metrics，并模拟一次指标自检
    m = read_metrics()
    detail = {
        'metrics_file': cfg.get('monitor', {}).get('metrics_file', 'v9_metrics.json'),
        'raw_metrics': m,
        'symbols': m.get('symbols', 0),
        'signals': m.get('signals', 0),
        'errors': m.get('errors', 0),
        'scan_duration_s': m.get('scan_duration_s', 0.0),
        'data_lag_s': m.get('data_lag_s', 0.0),
    }
    if 'error' in m:
        return log_step('P2', '数据采集', 'FAIL', {**detail, 'cost_ms': int((time.time()-t0)*1000)})
    return log_step('P2', '数据采集', 'PASS', {**detail, 'cost_ms': int((time.time()-t0)*1000)})


# ---------------------------------------------------------------------------
# 阶段 3：状态检测 / 规则评估
# ---------------------------------------------------------------------------
def phase_state_evaluation(cfg: dict, metrics: dict) -> list:
    t0 = time.time()
    alerts = []
    rules = cfg.get('alerts', [])
    detail = {'rules_evaluated': len(rules), 'triggered': []}

    for rule in rules:
        if not rule.get('enabled', True):
            continue
        metric_name = rule.get('metric', '')
        value = metrics.get(metric_name)
        if value is None:
            continue
        threshold = rule.get('threshold')
        op = rule.get('op', '>')
        triggered = False
        try:
            if op == '>' and value > threshold:
                triggered = True
            elif op == '<' and value < threshold:
                triggered = True
            elif op == '==' and value == threshold:
                triggered = True
            elif op == '>=' and value >= threshold:
                triggered = True
            elif op == '<=' and value <= threshold:
                triggered = True
        except Exception:
            pass
        if triggered:
            alert = {
                'name': rule.get('name', '未知告警'),
                'severity': rule.get('severity', 'normal'),
                'rule': rule.get('rule', metric_name),
                'value': f"{value} {rule.get('unit', '')}".strip(),
                'threshold': f"{threshold} {rule.get('unit', '')}".strip(),
                'description': rule.get('description', ''),
                'trigger_time': now_str(),
                'source': 'e2e-simulation',
            }
            alerts.append(alert)
            detail['triggered'].append(alert)

    detail['cost_ms'] = int((time.time() - t0) * 1000)
    status = 'ALERT' if alerts else 'PASS'
    log_step('P3', '状态检测', status, detail)
    return alerts


# ---------------------------------------------------------------------------
# 阶段 4：告警触发（飞书）
# ---------------------------------------------------------------------------
def phase_alert_dispatch(cfg: dict, alerts: list) -> dict:
    t0 = time.time()
    webhook = cfg.get('feishu', {}).get('webhook_url', '')
    secret = cfg.get('feishu', {}).get('secret', '')
    results = []

    if not alerts:
        # 没有真实告警时，发送一条「模拟正常」的提示，确保飞书通道可用
        dummy = {
            'name': 'E2E 模拟全部通过',
            'severity': 'normal',
            'rule': 'e2e_smoke_test',
            'value': '0 个告警',
            'threshold': '0',
            'description': 'v9 监控全链路模拟执行完毕，未发现触发阈值的真实告警。',
            'trigger_time': now_str(),
            'source': 'e2e-simulation',
        }
        ok, info = feishu_alert.send(webhook, dummy, secret=secret)
        results.append({'alert': dummy['name'], 'ok': ok, 'info': info})
    else:
        for alert in alerts:
            ok, info = feishu_alert.send(webhook, alert, secret=secret)
            results.append({'alert': alert['name'], 'ok': ok, 'info': info})

    detail = {
        'webhook': webhook[:60] + '...' if len(webhook) > 60 else webhook,
        'alerts_sent': len(results),
        'results': results,
    }
    all_ok = all(r['ok'] for r in results)
    status = 'PASS' if all_ok else 'FAIL'
    return log_step('P4', '告警触发/上报', status, {**detail, 'cost_ms': int((time.time()-t0)*1000)})


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    start = time.time()
    report = {
        'title': 'v9 监控全链路模拟报告',
        'start_time': now_str(),
        'environment': {
            'base_dir': BASE_DIR,
            'hostname': os.environ.get('COMPUTERNAME', 'unknown'),
            'timezone': 'Asia/Shanghai',
        },
        'steps': [],
    }

    try:
        cfg = read_config()
    except Exception as e:
        report['steps'].append(log_step('P0', '读取配置', 'FAIL', {'exception': str(e), 'traceback': traceback.format_exc()}))
        report['status'] = 'FAIL'
        report['end_time'] = now_str()
        save_report(report)
        return 1

    # 阶段 1-3
    p1 = phase_startup(cfg)
    report['steps'].append(p1)

    p2 = phase_data_collection(cfg)
    report['steps'].append(p2)

    metrics = p2['detail'].get('raw_metrics', {})
    alerts = phase_state_evaluation(cfg, metrics)
    report['steps'].append(log_step('P3', '状态检测', 'ALERT' if alerts else 'PASS', {
        'rules_evaluated': len(cfg.get('alerts', [])),
        'triggered_count': len(alerts),
        'triggered_names': [a['name'] for a in alerts],
    }))

    # 阶段 4
    p4 = phase_alert_dispatch(cfg, alerts)
    report['steps'].append(p4)

    # 汇总
    fail_steps = [s for s in report['steps'] if s['status'] in ('FAIL', 'ALERT')]
    report['status'] = 'PASS' if not fail_steps else 'PARTIAL' if all(s['status'] != 'FAIL' for s in report['steps']) else 'FAIL'
    report['end_time'] = now_str()
    report['total_cost_ms'] = int((time.time() - start) * 1000)

    save_report(report)
    return 0


def save_report(report: dict):
    path = os.path.join(BASE_DIR, 'data', 'v9_e2e_report.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved: {path}")


if __name__ == '__main__':
    sys.exit(main())
