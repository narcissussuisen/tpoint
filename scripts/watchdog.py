"""tpoint 守护进程 v3.1 — 单实例 + PID 追踪防重复拉起 + 交易日感知。

v3.1（2026-08-02）：非交易日（周末/节假日）不 spawn monitor/alert_engine。
修复 respawn storm：周末 monitor 启动即退出（is_trading_today=False），watchdog
误判"未运行"而每 60s 无限拉起（实测 15:03-15:13 每分钟 spawn 一个僵尸 monitor）。
交易日照常保活；交易日内非交易时段（盘前/午休/收盘后）进程需保持常驻（心跳），
故只在"整天非交易日"维度拦截，不做盘中时段判断。

策略：每 30s 检查 monitor.py / alert_engine.py 是否在跑；任一不在且当天为交易日则重启。
独立 Python 进程，不依赖任何 cmd.exe / bat（OS 杀 cmd 时 watchdog 仍能恢复服务）。
"""
import subprocess, os, sys, time, atexit, threading

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
# 用 WorkBuddy 托管的 python（3.13.12，91KB 启动器）而非 venv 的 241KB python.exe——
# 实测 venv 的 python.exe 在 Windows 上启动即自复制出一模一样的子进程（parent→child 同 cmdline），
# 导致 monitor/engine/watchdog 每个都变成双进程，引发重复告警。托管 python 不会自复制。
# watchdog 自身与 monitor/engine 均用 pythonw（无窗口子系统），彻底无 cmd 弹窗。
# 子进程 stdout/stderr 用【PIPE + 父进程 tee 线程】写日志文件——不依赖句柄继承，
# 规避 pythonw 父进程文件句柄不可继承导致的 OSError 22 静默崩溃（旧方案已验证会偶发）。
PY     = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe'   # watchdog 自身（无窗口）
PY_CON = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe'   # monitor/engine（无窗口子系统）
DATA = os.path.join(BASE, 'data')
LOGS = os.path.join(BASE, 'logs')
WATCHDOG_LOG  = os.path.join(LOGS, 'watchdog.log')
WATCHDOG_PID  = os.path.join(DATA, '.watchdog.pid')

CHECK_INTERVAL = 30
SPAWN_GRACE    = 60   # spawn 后 60s 内优先信任本地 PID，不走 WMI


def is_trading_today():
    """交易日判断：周一至周五且非 2026 节假日（与 core/monitor.py 口径一致）。
    节假日表同步维护；不在表内的工作日视为交易日。"""
    try:
        now = time.localtime()
        if now.tm_wday >= 5:
            return False
        from core.monitor import is_trading_today as _m
        return bool(_m())
    except Exception:
        # import 失败（如 PYTHONPATH 异常）时退化为仅周末判断，保证工作日可用
        return time.localtime().tm_wday < 5


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
            "$projs = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\""
            " | Where-Object { $_.CommandLine -and $_.CommandLine -like '*tpoint*' }"
            " | Select-Object -ExpandProperty CommandLine;"
            "if ($projs -match '" + script_basename + "') { exit 0 } else { exit 1 }"
        )
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps],
            capture_output=True, timeout=15, creationflags=0x08000000,
        )
        return r.returncode == 0
    except Exception as e:
        log(f'_wmi_has {script_basename} ps failed: {e}')
        return False

class Supervisor:
    def __init__(self):
        self.procs = {}       # label -> Popen 对象（用 p.poll() 做可靠存活判断）
        self.spawn_ts = {}    # label -> time.time()
        self._log_lock = threading.Lock()  # 多 tee 线程写同一日志文件时串行化

    def is_running(self, label, basename):
        # 1) 本地子进程：直接用 Popen 对象判断（基于真实进程句柄，100% 可靠）
        p = self.procs.get(label)
        if p is not None and p.poll() is None:
            return True
        # grace 期内信任本地 PID，不走文件判定，避免刚 spawn 的进程尚未写 pid 文件而误判
        if label in self.spawn_ts and (time.time() - self.spawn_ts[label]) < SPAWN_GRACE:
            return True
        # 2) PID 文件权威判定（绕过 WMI）：仅认 watchdog 自己拉起的 monitor/engine 写入的
        #    .monitor.svc.pid / .alert_engine.pid。不再用 WMI 兜底——Session0 僵尸 monitor 的
        #    命令行含 'tpoint'/'monitor.py'，会被 WMI 误判为存活，导致 watchdog 不拉起自己的
        #    monitor（详见 2026-07-30 复盘）。僵尸用旧锁路径，本进程用 .monitor.svc.*，互不干扰。
        if label == 'monitor':
            pidf = os.path.join(DATA, '.monitor.svc.pid')
        elif label == 'alert_engine':
            pidf = os.path.join(DATA, '.alert_engine.pid')
        else:
            pidf = None
        if pidf:
            try:
                if os.path.exists(pidf):
                    _c = open(pidf).read().strip()
                    if _c.isdigit() and _alive(int(_c)):
                        return True
            except Exception:
                pass
        return False

    def _tee(self, stream, log_path):
        """后台线程：把子进程管道输出逐行追加写入日志文件。"""
        try:
            with open(log_path, 'a', encoding='utf-8', errors='ignore') as lf:
                for raw in iter(stream.readline, b''):
                    try:
                        line = raw.decode('utf-8', 'ignore')
                        with self._log_lock:
                            lf.write(line)
                            lf.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    def spawn(self, label, script_path, log_path):
        env = os.environ.copy()
        env['PYTHONIOENCODING']    = 'utf-8'
        env['PYTHONUNBUFFERED']    = '1'
        env['PYTHONPATH']          = f'{BASE}\\venv\\Lib\\site-packages;{BASE}\\venv\\Lib;{BASE}'
        env['MACD_GATE_MODE']      = 'floor'
        env['TP_LAUNCHED_BY_V9LAUNCH'] = '1'
        env['TP_LOCK_BYPASS']       = '1'  # 跳过 alert_engine 的 msvcrt 文件锁（engine 自身 PID 单实例保证不重复）
        # stdout/stderr 用 PIPE：子进程输出经父进程 tee 线程写日志文件，
        # 完全不依赖句柄继承 → 规避 pythonw 父进程文件句柄不可继承导致的 OSError 22 静默崩溃。
        p = subprocess.Popen(
            [PY_CON, script_path], creationflags=0x200|0x08000000,  # NEW_PROCESS_GROUP|CREATE_NO_WINDOW(无窗口)；移除 DETACHED 以规避 pythonw 父进程下子进程标准句柄异常退出
            cwd=BASE, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        )
        threading.Thread(target=self._tee, args=(p.stdout, log_path), daemon=True).start()
        threading.Thread(target=self._tee, args=(p.stderr, log_path), daemon=True).start()
        log(f'spawned {label} PID={p.pid}')
        self.procs[label] = p
        self.spawn_ts[label] = time.time()
        return p.pid

    def run(self):
        log('watchdog v3.1 started (trading-day aware)')
        targets = [
            ('monitor',      os.path.join(BASE, 'core', 'monitor.py'),
                os.path.join(LOGS, 'monitor_console.log')),
            ('alert_engine', os.path.join(BASE, 'core', 'alert_engine.py'),
                os.path.join(LOGS, 'alert_engine_console.log')),
        ]
        last_trading_state = None
        while True:
            trading = is_trading_today()
            if trading != last_trading_state:
                log(f'trading day state: {trading} (was {last_trading_state})')
                last_trading_state = trading
            for label, script_path, log_path in targets:
                base = os.path.basename(script_path)
                if not self.is_running(label, base):
                    if trading:
                        log(f'{label} ({base}) not detected — restarting')
                        self.spawn(label, script_path, log_path)
                    else:
                        # 非交易日：不 spawn（monitor 启动即退出→误判→respawn storm 根因，v3.1）
                        log(f'{label} ({base}) not running, non-trading day — skip spawn')
            time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    _self_single_instance()
    Supervisor().run()
