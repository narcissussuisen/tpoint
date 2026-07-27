"""tpoint 守护进程 v3 — 单实例 + PID 追踪防重复拉起。

策略：每 30s 检查 monitor.py / alert_engine.py 是否在跑；任一不在则重启。
独立 Python 进程，不依赖任何 cmd.exe / bat（OS 杀 cmd 时 watchdog 仍能恢复服务）。

v3 加固（2026-07-27，解决 09:15–09:18 重复拉起风暴）：
  1. 自身单实例：启动即检查 .watchdog.pid，若持有者仍存活则安静退出，防止多处启动造成多 watchdog。
  2. PID 追踪：spawn 后记录子进程 PID，下一轮优先用 os.kill(pid,0) 校验，
     避免 WMI 在子进程刚起时尚未可见导致误判"未运行"而重复 spawn。
  3. 不再调用 clear_locks()：monitor / alert_engine 各自 acquire_single_instance 已处理 stale lock，
     watchdog 盲目删 .alert_engine.pid 会破坏活实例的 PID 账目。
"""
import subprocess, os, sys, time, atexit

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
# 用 WorkBuddy 托管的 python（3.13.12，91KB 启动器）而非 venv 的 241KB python.exe——
# 实测 venv 的 python.exe 在 Windows 上启动即自复制出一模一样的子进程（parent→child 同 cmdline），
# 导致 monitor/engine/watchdog 每个都变成双进程，引发重复告警。托管 python 不会自复制。
PY   = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe'
DATA = os.path.join(BASE, 'data')
LOGS = os.path.join(BASE, 'logs')
WATCHDOG_LOG  = os.path.join(LOGS, 'watchdog.log')
WATCHDOG_PID  = os.path.join(DATA, '.watchdog.pid')

CHECK_INTERVAL = 30
SPAWN_GRACE    = 60   # spawn 后 60s 内优先信任本地 PID，不走 WMI

def log(msg):
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n'
    try:
        with open(WATCHDOG_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass
    try: sys.stdout.write(line); sys.stdout.flush()
    except Exception: pass

def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError, PermissionError):
        return False

def _self_single_instance():
    """watchdog 自身单实例：若 .watchdog.pid 指向存活进程则退出。"""
    try:
        if os.path.exists(WATCHDOG_PID):
            with open(WATCHDOG_PID, 'r') as _pf:
                _c = _pf.read().strip()
            if _c.isdigit():
                _holder = int(_c)
                if _holder != os.getpid() and _alive(_holder):
                    log(f'watchdog 已有活实例 pid={_holder}，本实例退出')
                    sys.exit(0)
    except Exception:
        pass
    try:
        with open(WATCHDOG_PID, 'w') as pf:
            pf.write(str(os.getpid()))
    except Exception as e:
        log(f'WARN 写 .watchdog.pid 失败: {e}')
    atexit.register(lambda: (
        os.remove(WATCHDOG_PID) if os.path.exists(WATCHDOG_PID) else None
    ))

def _wmi_has(script_basename):
    """通过 WMI Get-CimInstance 取真实命令行，校验是否有进程以 script_basename 启动。"""
    try:
        ps = (
            "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
            " | Where-Object { $_.CommandLine -and $_.CommandLine -like '*tpoint*' }"
            " | Select-Object -ExpandProperty CommandLine;"
            "if ($procs -match '" + script_basename + "') { exit 0 } else { exit 1 }"
        )
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception as e:
        log(f'_wmi_has {script_basename} ps failed: {e}')
        return False

class Supervisor:
    def __init__(self):
        self.spawned = {}      # label -> pid
        self.spawn_ts = {}     # label -> time.time()

    def is_running(self, label, basename):
        # 1) 本地刚 spawn 的 PID，优先用 os.kill 验证（WMI 在进程刚起时可能尚未可见）
        pid = self.spawned.get(label)
        if pid and _alive(pid):
            return True
        # grace 期内不再走 WMI，避免刚 spawn 的进程尚未被 WMI 枚举到而误判
        if label in self.spawn_ts and (time.time() - self.spawn_ts[label]) < SPAWN_GRACE:
            return True
        # 2) WMI 兜底（覆盖外部/manual 拉起的进程）
        return _wmi_has(basename)

    def spawn(self, label, script_path, log_path):
        env = os.environ.copy()
        env['PYTHONIOENCODING']    = 'utf-8'
        env['PYTHONUNBUFFERED']    = '1'
        env['PYTHONPATH']          = f'{BASE}\\venv\\Lib\\site-packages;{BASE}\\venv\\Lib;{BASE}'
        env['MACD_GATE_MODE']      = 'floor'
        env['TP_LAUNCHED_BY_V9LAUNCH'] = '1'
        env['TP_LOCK_BYPASS']       = '1'  # 跳过 alert_engine 的 msvcrt 文件锁（engine 自身 PID 单实例保证不重复）
        log_fh = open(log_path, 'a', encoding='utf-8', errors='ignore')
        p = subprocess.Popen(
            [PY, script_path], creationflags=0x8|0x200,
            cwd=BASE, env=env, stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=log_fh, close_fds=True,
        )
        log_fh.close()
        log(f'spawned {label} PID={p.pid}')
        self.spawned[label] = p.pid
        self.spawn_ts[label] = time.time()
        return p.pid

    def run(self):
        log('watchdog v3 started')
        targets = [
            ('monitor',      os.path.join(BASE, 'core', 'monitor.py'),
                os.path.join(LOGS, 'monitor_console.log')),
            ('alert_engine', os.path.join(BASE, 'core', 'alert_engine.py'),
                os.path.join(LOGS, 'alert_engine_console.log')),
        ]
        while True:
            for label, script_path, log_path in targets:
                base = os.path.basename(script_path)
                if not self.is_running(label, base):
                    log(f'{label} ({base}) not detected — restarting')
                    self.spawn(label, script_path, log_path)
            time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    _self_single_instance()
    Supervisor().run()
