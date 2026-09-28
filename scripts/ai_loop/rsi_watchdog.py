# -*- coding: utf-8 -*-
r"""rsi_watchdog.py — RSI 循环自健康看门狗（P2.2，2026-09-28；schtasks 21:30 机械跑）

历史教训（本看门狗的存在理由）：
  - diag_r2p_probe.py 失踪 8 周无人发现（automation 每周失败但静默）；
  - p1_gray_monitor.py 7 周零采集（n_days 卡死 2 无人告警）。
⇒ 「存在性」检查不够，必须查「新鲜度」（文件龄）。

检查项（交易日跑；非交易日静默退出）：
  A. 当日产物存在性：digest / 决策JSON / review / reconcile / live_review / effect_ledger 当日条目
  B. 新鲜度：最新 random_control_*.json 龄 >14 交易日；iteration_log.jsonl 末条龄 >5 个 RSI 日
  C. 队列：validity_queue 超期未复核项
  D. backlog：open 项堆积趋势（>40 告警）

告警去重：data/rsi/watchdog_alert_state.json 记已告警 issue；
同 issue 首日告警 + 每 7 天重发一次；恢复后清除状态。

用法：venv/Scripts/python.exe scripts/ai_loop/rsi_watchdog.py [--dry-run]
推送：loop_engine core.py push()（a35d7f52 自迭代群；卡死类升 b4eba7a9 全局群）。
"""
import argparse
import datetime
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'core'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_calendar import is_trading_day  # noqa: E402

RSI_DIR = os.path.join(ROOT, 'data', 'rsi')
OUT = os.path.join(ROOT, 'output')
STATE_FP = os.path.join(RSI_DIR, 'watchdog_alert_state.json')

FRESH_RANDOM_CONTROL_MAX_TRADE_DAYS = 14
FRESH_ITERLOG_MAX_RSI_DAYS = 5
BACKLOG_OPEN_WARN = 40
RESEND_DAYS = 7


def _load(fp, default=None):
    try:
        with open(fp, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)


def _trading_days_between(d1, d2):
    """d1..d2 之间的交易日数（含端点；用于文件龄换算）。"""
    a = datetime.date.fromisoformat(d1)
    b = datetime.date.fromisoformat(d2)
    if a > b:
        a, b = b, a
    cnt = 0
    cur = a
    while cur <= b:
        if is_trading_day(cur.isoformat()):
            cnt += 1
        cur += datetime.timedelta(days=1)
    return cnt


def collect_issues(today):
    issues = []  # (issue_id, severity, text)

    # A. 当日产物存在性
    for name, fp, sev in (
        ('digest', os.path.join(ROOT, 'data', 'ai_decisions', f'{today}.digest.json'), 'warn'),
        ('decision', os.path.join(ROOT, 'data', 'ai_decisions', f'{today}.json'), 'warn'),
        ('review', os.path.join(OUT, f'review_{today}.json'), 'warn'),
        ('reconcile', os.path.join(OUT, f'reconcile_{today}.json'), 'warn'),
        ('live_review', os.path.join(OUT, f'live_review_{today}.json'), 'warn'),
    ):
        if not os.path.exists(fp):
            issues.append((f'missing:{name}', sev, f'当日产物缺失：{name}（{fp}）'))

    # effect_ledger 当日 daily 条目
    ledger = os.path.join(ROOT, 'data', 'effect_ledger.jsonl')
    has_daily = False
    try:
        with open(ledger, encoding='utf-8') as f:
            for ln in f:
                if today in ln and '"daily"' in ln:
                    has_daily = True
                    break
    except Exception:
        pass
    if not has_daily:
        issues.append(('missing:effect_ledger_daily', 'warn',
                       f'effect_ledger 无 {today} 的 daily 条目'))

    # B. 新鲜度：最新 random_control 产物龄
    rcs = sorted(glob.glob(os.path.join(OUT, 'random_control_*.json')))
    if rcs:
        import re
        dates = []
        for fp in rcs:
            m = re.search(r'_(\d{8})(?:_|$|\.json)', os.path.basename(fp))
            if m:
                dates.append(m.group(1))
        if dates:
            latest = max(dates)
            age = _trading_days_between(latest[:4] + '-' + latest[4:6] + '-' + latest[6:], today)
            if age > FRESH_RANDOM_CONTROL_MAX_TRADE_DAYS:
                issues.append(('stale:random_control', 'warn',
                               f'最新随机对照产物已 {age} 个交易日未更新（>{FRESH_RANDOM_CONTROL_MAX_TRADE_DAYS}）'
                               f'——静默失效风险（diag_r2p_probe 教训）'))
    else:
        issues.append(('stale:random_control', 'warn', 'output/ 下无任何 random_control_*.json'))

    ilog = os.path.join(RSI_DIR, 'iteration_log.jsonl')
    if os.path.exists(ilog):
        last_date = None
        with open(ilog, encoding='utf-8') as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                    last_date = e.get('at', '')[:10] or last_date
                except Exception:
                    pass
        if last_date:
            age = _trading_days_between(last_date, today)
            if age > FRESH_ITERLOG_MAX_RSI_DAYS:
                issues.append(('stale:rsi_iteration', 'warn',
                               f'RSI iteration_log 已 {age} 个交易日无新条目——循环停摆风险'))
    else:
        issues.append(('stale:rsi_iteration', 'warn', 'data/rsi/iteration_log.jsonl 不存在'))

    # C. 队列超期未复核
    queue = _load(os.path.join(RSI_DIR, 'validity_queue.json'), [])
    for q in queue:
        if q.get('status') == 'active' and q.get('queue_until', '') < today:
            issues.append((f"queue-overdue:{q.get('proposal_id')}", 'warn',
                           f"validity_queue 项 {q.get('proposal_id')} 观察期至 {q.get('queue_until')} 已到期未复核"))

    # D. backlog open 堆积
    n_open = 0
    bl = os.path.join(ROOT, 'data', 'feedback_backlog.jsonl')
    try:
        with open(bl, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = json.loads(ln)
                    if e.get('status') == 'open':
                        n_open += 1
                except Exception:
                    pass
    except Exception:
        pass
    if n_open > BACKLOG_OPEN_WARN:
        issues.append(('backlog-open-high', 'info',
                       f'backlog open 项 {n_open} > {BACKLOG_OPEN_WARN}——消费速度不足'))

    # 好消息：当日 random_control 新产物（用于清除陈旧告警状态）
    return issues


def main():
    ap = argparse.ArgumentParser(description='RSI 循环自健康看门狗')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()

    if not is_trading_day(a.date):
        return 0
    issues = collect_issues(a.date)
    if a.dry_run:
        for iid, sev, text in issues:
            print(f'[{sev}] {iid}: {text}')
        print(f'{len(issues)} issue(s)')
        return 0

    # 推送（去重）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'le_core', os.path.join(ROOT, 'scripts', 'loop_engine', 'core.py'))
    le = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(le)

    state = _load(STATE_FP, {})
    today_d = datetime.date.fromisoformat(a.date)
    sent = []
    for iid, sev, text in issues:
        rec = state.get(iid)
        if rec:
            last = datetime.date.fromisoformat(rec['last_sent'])
            if (today_d - last).days < RESEND_DAYS:
                continue
        try:
            le.push(f'⚠️ [RSI看门狗] {text}')
        except Exception:
            pass
        state[iid] = {'last_sent': a.date}
        sent.append(iid)
    # 恢复清除：issue 消失的旧状态项删除
    active_ids = {iid for iid, _, _ in issues}
    for iid in list(state):
        if iid not in active_ids:
            del state[iid]
    _save(STATE_FP, state)
    print(f'watchdog: {len(issues)} issue(s), sent {len(sent)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
