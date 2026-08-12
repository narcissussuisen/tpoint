#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v9 监控告警引擎（watchdog sidecar）。

职责：
  1. 轮询 monitor 写入的 metrics.json，采集关键指标
     - 服务状态：心跳是否新鲜（进程存活/扫描正常）
     - 性能指标：单轮扫描耗时、行情数据延迟
     - 异常检测：信号突增、扫描错误、长时间无新数据
  2. 当指标触发阈值或检测到异常，经飞书 Webhook 发送分级交互卡片
  3. 卡片按严重等级（普通/警告/严重）使用不同模板
  4. 全部阈值/规则/飞书配置均来自 monitor_config.json，可热改

运行：
  python alert_engine.py                 # 常驻轮询
  python alert_engine.py --once          # 单次评估后退出（调试）
  python alert_engine.py --dry-run       # 不真正发请求，仅打印卡片
  python alert_engine.py --self-test     # 验证卡片渲染 + 模拟触发

数据来源：monitor 每轮扫描末写入 metrics.json（含 ts/scan_duration_s/
signals/errors/last_bar_ts/status）。monitor 崩溃 → 心跳过期 → 触发"服务中断"。
"""
import os, sys, json, time, argparse, atexit, random
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from feishu_alert import send as feishu_send

# ========== 单实例锁（2026-07-20 加固：防止 engine 静默重复运行 → 飞书告警重发） ==========
# 与 monitor 同款机制：data/.alert_engine.lock + data/.alert_engine.pid
DATA_DIR  = os.path.join(BASE_DIR, 'data')
LOCK_FILE = os.path.join(DATA_DIR, '.alert_engine.lock')
PID_FILE  = os.path.join(DATA_DIR, '.alert_engine.pid')

if os.name == 'nt':
    import msvcrt
    def _ae_acquire_lock(lf):
        lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
    def _ae_release_lock(lf):
        try:
            lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
else:
    import fcntl
    def _ae_acquire_lock(lf):
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def _ae_release_lock(lf):
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass

def _ae_is_process_alive(pid):
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # Windows/Unix 通用：仅检测进程存在性
        return True
    except (ProcessLookupError, OSError, PermissionError):
        return False

def _ae_remove_if_exists(path):
    try:
        if os.path.exists(path):
            os.remove(path); return True
    except (FileNotFoundError, OSError, PermissionError):
        pass
    return False

def _ae_clear_stale_lock(lock_file, pid_file):
    """若锁/PID 指向的进程已死或文件残留，则清理。"""
    holder = None
    try:
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as _pf:
                _c = _pf.read().strip()
            if _c.isdigit():
                holder = int(_c)
    except Exception:
        pass
    if holder is not None and not _ae_is_process_alive(holder):
        _ae_remove_if_exists(pid_file); _ae_remove_if_exists(lock_file); return True
    if holder is None and os.path.exists(lock_file):
        _ae_remove_if_exists(lock_file); return True
    return False

def _ae_read_holder_pid(pid_file):
    """读 pid 文件中的持有者 PID（不存在/损坏返回 None）。"""
    try:
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as _pf:
                _c = _pf.read().strip()
            if _c.isdigit():
                return int(_c)
    except Exception:
        pass
    return None


def acquire_single_instance(lock_file, pid_file, max_attempts=20):
    """获取单实例锁。

    设计要点（2026-07-22 加固，解决多 run_engine 循环竞争导致的死锁/崩溃循环）：
      - 每次尝试立即 open+lock，失败立即关闭句柄（不跨重试持锁，避免阻塞其它实例 open）。
      - 失败且 pid 文件指向的持有者仍存活 → 视为已有实例在跑，安静退出(sys.exit(0))，
        不再 crash-loop 刷 engine_crash.log。
      - 失败但无存活持有者（pid 缺失/已死）→ 抖动后退避重试，乐观接管。
      - 全部尝试后仍失败、且确有存活实例 → 安静退出；否则才告警退出。
    返回锁文件句柄(常驻)；若确有其它活实例则直接 exit(0)。

    2026-07-27 增：TP_LOCK_BYPASS=1 时跳过文件锁直接接管（依赖 watchdog 单一 spawn 保证不重复）。
      解决 msvcrt+Windows 文件锁在 fork/exec 边界间歇泄漏导致 20次重试全失败的死循环。
    """
    if os.environ.get('TP_LOCK_BYPASS') == '1':
        # 跳过 msvcrt 文件锁（解决 Windows 文件锁间歇泄漏死循环），但仍须 PID 单实例保证：
        # 防止多个 watchdog / 手动拉起各自 spawn 出重复 engine → 飞书告警重发。
        _holder = _ae_read_holder_pid(pid_file)
        if _holder is not None and _holder != os.getpid() and _ae_is_process_alive(_holder):
            print(f"[{time.strftime('%H:%M:%S')}] 告警引擎 LOCK_BYPASS 检测到已有活实例 pid={_holder}，本实例退出(不重复运行)")
            sys.exit(0)
        try:
            with open(pid_file, 'w') as pf:
                pf.write(str(os.getpid()))
            print(f"[{time.strftime('%H:%M:%S')}] 告警引擎 LOCK_BYPASS 接管 (pid={os.getpid()})，依赖 PID 单实例+watchdog 保证")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 告警引擎 LOCK_BYPASS 写 pid 失败: {e}")
        # 返回一个永不能 unlock 的 dummy 让 cleanup 不崩
        class _DummyLf:
            def close(self): pass
            def write(self, *_a, **_kw): pass
            def flush(self): pass
            def fileno(self): return -1
        atexit.register(lambda: (
            _ae_remove_if_exists(pid_file),
        ))
        return _DummyLf()
    attempt = 0
    lf = None
    while attempt < max_attempts:
        attempt += 1
        _ae_clear_stale_lock(lock_file, pid_file)
        try:
            lf = open(lock_file, 'w')
        except Exception:
            time.sleep(random.uniform(0.3, 1.0)); continue
        try:
            _ae_acquire_lock(lf); break
        except (IOError, OSError):
            # 关键：立即关闭句柄，释放给其他实例 open 的机会，避免持锁死锁
            try: lf.close()
            except Exception: pass
            lf = None
            # 若 pid 文件指向一个仍存活的实例 → 已有实例在跑，安静退出
            _holder = _ae_read_holder_pid(pid_file)
            if _holder is not None and _ae_is_process_alive(_holder):
                print(f"[{time.strftime('%H:%M:%S')}] 告警引擎已有活实例 pid={_holder}，本实例退出(不重复运行)")
                sys.exit(0)
            time.sleep(random.uniform(0.3, 1.0))
    else:
        # 重试耗尽：再看是否真有活实例
        _holder = _ae_read_holder_pid(pid_file)
        if _holder is not None and _ae_is_process_alive(_holder):
            print(f"[{time.strftime('%H:%M:%S')}] 告警引擎已有活实例 pid={_holder}，本实例退出(不重复运行)")
            sys.exit(0)
        print(f"[{time.strftime('%H:%M:%S')}] 告警引擎单实例锁获取失败({max_attempts}次)，退出")
        sys.exit(1)
    lf.write(str(os.getpid())); lf.flush()
    with open(pid_file, 'w') as pf:
        pf.write(str(os.getpid()))
    print(f"[{time.strftime('%H:%M:%S')}] 告警引擎单实例锁已获取 pid={os.getpid()}")
    acquired = True
    def _cleanup():
        # 仅当本实例真正获取锁时才清理锁文件，避免失败实例误删存活实例的锁
        if not acquired:
            return
        try: _ae_release_lock(lf)
        except Exception: pass
        try: lf.close()
        except Exception: pass
        _ae_remove_if_exists(lock_file); _ae_remove_if_exists(pid_file)
    atexit.register(_cleanup)
    return lf


def _cfg_path():
    return os.environ.get('V9_ALERT_CONFIG') or os.path.join(BASE_DIR, 'config', 'monitor_config.json')


def load_config(path):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    # 环境变量覆盖飞书 webhook / secret
    if os.environ.get('V9_ALERT_WEBHOOK_URL'):
        cfg.setdefault('feishu', {})['webhook_url'] = os.environ['V9_ALERT_WEBHOOK_URL']
    if os.environ.get('V9_ALERT_SECRET'):
        cfg.setdefault('feishu', {})['secret'] = os.environ['V9_ALERT_SECRET']
    return cfg


def read_metrics(path, _max_retries=2):
    """读取 metrics.json（monitor 心跳文件）。
    2026-07-20 fix: 增加重试机制（最多 _max_retries 次，间隔 0.3s），
    防止 monitor 正在原子替换 metrics.json 瞬间导致 PermissionError → 误报「未检测到心跳文件」。
    """
    import time as _time
    for attempt in range(_max_retries):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            if attempt < _max_retries - 1:
                _time.sleep(0.3)
            continue
    return None


def _cmp(op, a, b):
    if op == '>':  return a > b
    if op == '>=': return a >= b
    if op == '<':  return a < b
    if op == '<=': return a <= b
    if op == '==': return a == b
    if op == '!=': return a != b
    return False


def _fmt(v, unit=''):
    if isinstance(v, bool):
        return '是' if v else '否'
    if isinstance(v, float):
        return f"{v:.2f}{unit}"
    return f"{v}{unit}"


def is_trading_today():
    """是否A股交易日 (与 monitor.is_trading_today 一致)。
    alert_engine 在休市日跳过评估, 避免 monitor 按设计退出(not trading today)、
    心跳不维护时误报 service_down。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    today_str = now.strftime('%Y-%m-%d')
    holidays_2026 = {
        '2026-01-01', '2026-01-02',
        '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
        '2026-02-02', '2026-02-03',
        '2026-04-06',
        '2026-05-01', '2026-05-04', '2026-05-05',
        '2026-06-19',
        '2026-09-25', '2026-09-28', '2026-09-29', '2026-09-30',
        '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
    }
    return today_str not in holidays_2026


def _session_window(cfg):
    """从配置中读取盘中评估窗口，返回 (open_h, open_m, close_h, close_m)。"""
    s = (cfg.get('monitor') or {}).get('session') or {}
    return (int(s.get('open_h', 9)), int(s.get('open_m', 15)),
            int(s.get('close_h', 15)), int(s.get('close_m', 5)))


def in_trading_session(now_epoch, cfg):
    """是否处于 A 股盘中评估窗口（默认 09:15–15:05，可配置）。
    仅该窗口内评估 service_up，避免收盘后 monitor 仅保活（ts 陈旧）导致误报『服务中断』。
    self_test 场景不受影响——它直接调用 evaluate()，不走本门控。"""
    if not is_trading_today():
        return False
    now = datetime.fromtimestamp(now_epoch)
    oh, om, ch, cm = _session_window(cfg)
    t = now.time()
    open_t = t.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = t.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_t <= t <= close_t


def evaluate(sample, buffer, now, cfg):
    """返回需要发送的告警 dict 列表（不含冷却判断）。"""
    # 休市日 monitor 按设计退出(not trading today)、心跳不维护 -> 跳过评估, 避免误报 service_down
    if not is_trading_today():
        return []
    m = cfg['monitor']
    stale = m.get('service_stale_s', 120)

    # ---- 派生指标 ----
    service_up = bool(sample and (now - sample.get('ts', 0)) <= stale)
    stale_age = int(now - sample.get('ts', 0)) if sample else None
    scan_duration_s = sample.get('scan_duration_s') if sample else None
    last_bar_ts = sample.get('last_bar_ts') if sample else None
    data_lag_s = (now - last_bar_ts) if (sample and last_bar_ts) else None

    derived = {
        'service_up': service_up,
        'scan_duration_s': scan_duration_s,
        'data_lag_s': data_lag_s,
        'signals_window': None,
        'errors_window': None,
    }
    # 窗口聚合（信号突增 / 扫描异常）
    for rule in cfg.get('alerts', []):
        win = rule.get('window_s')
        if not win:
            continue
        sig_sum = sum(s.get('signals', 0) for s in buffer if now - s.get('ts', 0) <= win)
        err_sum = sum(s.get('errors', 0) for s in buffer if now - s.get('ts', 0) <= win)
        if rule['metric'] == 'signals_window':
            derived['signals_window'] = sig_sum
        elif rule['metric'] == 'errors_window':
            derived['errors_window'] = err_sum

    alerts = []
    for rule in cfg.get('alerts', []):
        if not rule.get('enabled', True):
            continue
        metric = rule['metric']
        value = derived.get(metric)
        if value is None:
            continue
        # require_up：仅当 monitor 存活（心跳新鲜）时才评估，避免服务中断时
        # 衍生指标（数据延迟/扫描耗时/窗口聚合）产生冗余告警，根因统一由 service_up 表达
        if rule.get('require_up') and not service_up:
            continue
        threshold = rule['threshold']
        breach = _cmp(rule['op'], value, threshold)
        if not breach:
            continue
        # 显示文本：service_up 用人类可读文案替代 是/否
        disp_value = value
        disp_threshold = threshold
        if metric == 'service_up':
            if sample is None:
                disp_value = '未检测到心跳文件'
            else:
                disp_value = f'中断（心跳停滞 {stale_age}s）'
            disp_threshold = f'需存活（心跳≤{stale}s）'
        alerts.append({
            'name': rule['name'],
            'rule': rule['rule'],
            'severity': rule['severity'],
            'trigger_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
            'value': _fmt(disp_value, rule.get('unit', '')),
            'threshold': _fmt(disp_threshold, rule.get('unit', '')),
            'description': rule['description'],
            'source': 'tpoint_alert_engine',
            '_cooldown_s': rule.get('cooldown_s', 300),
            '_rule_key': rule['rule'],
        })
    return alerts


def _trim_buffer(buffer, now, cfg):
    max_win = max([r.get('window_s', 0) for r in cfg.get('alerts', [])] + [cfg['monitor'].get('service_stale_s', 120)])
    cap = cfg['monitor'].get('max_buffer', 40)
    kept = [s for s in buffer if now - s.get('ts', 0) <= max_win]
    return kept[-cap:]


def main():
    ap = argparse.ArgumentParser(description='v9 监控告警引擎')
    ap.add_argument('--once', action='store_true', help='单次评估后退出')
    ap.add_argument('--dry-run', action='store_true', help='不真正发请求，仅打印卡片')
    ap.add_argument('--self-test', action='store_true', help='验证卡片渲染与规则触发')
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg = load_config(_cfg_path())
    webhook = cfg.get('feishu', {}).get('webhook_url', '')
    secret = cfg.get('feishu', {}).get('secret', '')
    m = cfg['monitor']
    metrics_path = os.path.join(BASE_DIR, 'data', m.get('metrics_file', 'metrics.json'))
    poll = m.get('poll_interval_s', 15)
    dry = args.dry_run or not webhook

    print(f"[{time.strftime('%H:%M:%S')}] v9 告警引擎启动 | webhook={'已配置' if webhook else '未配置(dry-run)'} | 轮询={poll}s")
    buffer = []
    last_fired = {}

    def run_once():
        nonlocal buffer
        now = time.time()
        if not in_trading_session(now, cfg):
            print("  · 非盘中时段，跳过评估（收盘后 monitor 仅保活，不报 service_down）")
            return []
        sample = read_metrics(metrics_path)
        if sample:
            buffer.append(sample)
        buffer = _trim_buffer(buffer, now, cfg)
        alerts = evaluate(sample, buffer, now, cfg)
        for a in alerts:
            key = a.pop('_rule_key'); cd = a.pop('_cooldown_s')
            if key in last_fired and (now - last_fired[key]) < cd:
                continue
            last_fired[key] = now
            ok, info = feishu_send(webhook, a, secret=secret, dry_run=dry)
            print(f"  🔔 [{a['severity']}] {a['name']} | 值={a['value']} 阈值={a['threshold']} | {info}")
        # 状态日志
        if sample:
            age = int(now - sample.get('ts', 0))
            print(f"  · 心跳年龄={age}s 扫描={sample.get('scan_duration_s')}s 信号={sample.get('signals')} 错误={sample.get('errors')}")
        else:
            print(f"  · 未读到 {os.path.basename(metrics_path)}（monitor 未运行？）")
        return alerts

    if args.once:
        run_once()
        return

    # 常驻守护模式：获取单实例锁，防止重复运行导致飞书告警重发
    acquire_single_instance(LOCK_FILE, PID_FILE)

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"  ⚠️ 引擎异常: {e}")
        time.sleep(poll)


def self_test():
    print("===== 1) 三档卡片渲染 =====")
    for sev in ('normal', 'warning', 'critical'):
        feishu_send(None, {
            'name': f'演示告警-{sev}', 'severity': sev,
            'trigger_time': '2026-07-09 09:30:00', 'value': '12.34 s',
            'threshold': '10 s', 'description': '用于验证卡片渲染的演示告警。',
            'rule': 'scan_duration_s', 'source': 'self-test',
        }, dry_run=True)

    print("\n===== 2) 规则触发模拟（dry-run）=====")
    cfg = load_config(_cfg_path())
    now = time.time()

    # 场景 A：服务存活但各项指标劣化（心跳新鲜）
    sample_a = {
        'ts': now, 'scan_duration_s': 23.5, 'signals': 9, 'errors': 2,
        'symbols': 5, 'last_bar_ts': now - 720, 'status': 'running',
    }
    buffer_a = [sample_a] * 5  # 5 个采样点 → 窗口内 signals=45, errors=10
    alerts_a = evaluate(sample_a, buffer_a, now, cfg)
    print(f"[场景A 服务存活] 触发告警数: {len(alerts_a)}")
    for a in alerts_a:
        feishu_send(None, a, dry_run=True)

    # 场景 B：服务中断（心跳过期，超过 service_stale_s）
    sample_b = {
        'ts': now - 1000, 'scan_duration_s': 23.5, 'signals': 9, 'errors': 2,
        'symbols': 5, 'last_bar_ts': now - 1720, 'status': 'running',
    }
    # 单点采样即可：require_up 规则因 service_up=False 被抑制，仅 service_up 触发
    alerts_b = evaluate(sample_b, [sample_b], now, cfg)
    print(f"\n[场景B 服务中断] 触发告警数: {len(alerts_b)}")
    for a in alerts_b:
        feishu_send(None, a, dry_run=True)

    total = len(alerts_a) + len(alerts_b)
    print(f"\nself-test 完成：共覆盖 {total} 条规则触发。")


if __name__ == '__main__':
    main()
