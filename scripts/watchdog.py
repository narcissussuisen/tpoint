"""tpoint 守护进程 v3.2 — 单实例 + PID 追踪防重复拉起 + 交易日感知。

v3.1（2026-08-02）：非交易日（周末/节假日）不 spawn monitor/alert_engine。
修复 respawn storm：周末 monitor 启动即退出（is_trading_today=False），watchdog
误判"未运行"而每 60s 无限拉起（实测 15:03-15:13 每分钟 spawn 一个僵尸 monitor）。
交易日照常保活；交易日内非交易时段（盘前/午休/收盘后）进程需保持常驻（心跳），
故只在"整天非交易日"维度拦截，不做盘中时段判断。

v3.2（2026-09-25）★ 修复「工作日节假日被判为交易日」的静默降级 —— v3.1 的修复其实从未生效：
`is_trading_today()` 里的 `from core.monitor import is_trading_today` 在本进程的 sys.path 下
**必然抛 ModuleNotFoundError**（详见该函数 docstring），被 `except: pass` 吞掉后退化为
「仅周末判断」⇒ 所有落在周一~周五的法定节假日（含 2026 中秋 9/25）全被判为交易日，
monitor 被每 60s 拉起一次（启动 7s 后即 `EXIT not trading today`），**全天 spawned 705 次**。
现改为直连零依赖的 core/trading_calendar.py，且失败一律 **fail-closed + CRITICAL 告警**，绝不静默。
并在同一版把非交易日的 `skip spawn` 日志由「每轮记」改为**边沿触发 + 每小时心跳**
（原写法 30s×2 行 ⇒ 假期约 5,760 行/日纯噪声，与 alert_engine_console.log 膨胀同源）。

策略：每 30s 检查 monitor.py / alert_engine.py 是否在跑；任一不在且当天为交易日则重启。
独立 Python 进程，不依赖任何 cmd.exe / bat（OS 杀 cmd 时 watchdog 仍能恢复服务）。
"""
import subprocess, os, sys, time, atexit, threading, datetime, json, shutil

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
# 用 WorkBuddy 托管的 python（3.13.12，91KB 启动器）而非 venv 的 241KB python.exe——
# 实测 venv 的 python.exe 在 Windows 上启动即自复制出一模一样的子进程（parent→child 同 cmdline），
# 导致 monitor/engine/watchdog 每个都变成双进程，引发重复告警。托管 python 不会自复制。
# watchdog 自身用 pythonw（无窗口）；monitor/engine 必须用 python.exe（v3.2 起）——
# pythonw 拉起的子进程会静默早夭/僵死（进程存在但零日志零心跳，08-03/08-05 两次实证），
# python.exe + CREATE_NO_WINDOW(0x08000000) 同样无弹窗但输出/心跳正常。
# 子进程 stdout/stderr 用【PIPE + 父进程 tee 线程】写日志文件——不依赖句柄继承。
PY     = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe'   # watchdog 自身（无窗口）
PY_CON = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe'    # monitor/engine（v3.2: python.exe，禁用 pythonw）
DATA = os.path.join(BASE, 'data')
LOGS = os.path.join(BASE, 'logs')
WATCHDOG_LOG  = os.path.join(LOGS, 'watchdog.log')
WATCHDOG_PID  = os.path.join(DATA, '.watchdog.pid')

CHECK_INTERVAL = 30
SPAWN_GRACE    = 60   # spawn 后 60s 内优先信任本地 PID，不走 WMI
QUIET_HEARTBEAT_S = 3600   # 非交易日空闲心跳间隔（替代每 30s 刷两行的噪声写法）


def _resolve_powershell():
    """定位 Windows PowerShell —— **多候选探测，不依赖 PATH**。

    [2026-09-25] 原 `_wmi_has` 直接 `['powershell', ...]`。在 PATH 不含
    WindowsPowerShell\\v1.0 的会话下（计划任务 / 受限沙箱）subprocess 抛 WinError 2，
    而 `_wmi_has` 的 except 返回 **False = "进程没在跑"** ⇒ watchdog 会**重复拉起**
    monitor/alert_engine（重复告警 + 日志翻倍）。失败方向是 fail-open，故必须修。
    """
    sysroot = os.environ.get('SystemRoot') or r'C:\Windows'
    for c in (os.path.join(sysroot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
              os.path.join(sysroot, 'SysWOW64', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
              shutil.which('pwsh'), shutil.which('powershell')):
        if c and os.path.isfile(c):
            return c
    return 'powershell'


PS_EXE = _resolve_powershell()


def is_trading_today(now=None):
    """交易日判定 —— 单一真源 `core/trading_calendar.py`。

    [2026-09-25 修复] 原实现 `from core.monitor import is_trading_today` 在本进程的
    sys.path 下**必然失败**：本进程由 launch_watchdog.py 拉起，PYTHONPATH 为
    `venv\\Lib\\site-packages;venv\\Lib;BASE`（见 launch_watchdog.py:9），
    sys.path[0] 又是脚本目录 `BASE\\scripts` —— **都不含 `BASE\\core`**。
    而 `core/monitor.py` 模块级就 `from datasource import ...`，datasource.py 只存在于
    core/ ⇒ `ModuleNotFoundError` ⇒ 被 `except` 静默吞掉 ⇒ 退化为「仅周末判断」
    ⇒ **所有落在周一~周五的法定节假日都被判为交易日**。

    实测后果（2026-09-25 中秋）：monitor 每 60s 被拉起一次（启动 7s 后即
    `EXIT not trading today`），全天 `spawned` **705 次**，日志暴涨、CPU 反复加载
    numpy/pandas。周末分支之所以从未暴露该缺陷，是因为 `tm_wday >= 5` 在 import 之前
    就 return 了（9/19 周六的 `trading day state: False` 正是这条路）。

    修复三要点：
      1. 直连 `core/trading_calendar.py` —— 该模块只 `import datetime`，**零重依赖**，
         不会再被 numpy/pandas/requests/datasource 的导入链拖累；
      2. 失败**不静默**：落 CRITICAL 日志 + 飞书告警（30 分钟去重）；
      3. 失败一律 **fail-closed（返回 False = 不拉起）** —— 宁可漏跑一天，也不要在休市日
         空转；漏跑会被 selfcheck 的 respawn-storm / 心跳检查发现，而空转不会。

    now: 可选注入点（date / datetime / None=当前），仅供回归测试逐日验证，生产不传。
    """
    if now is None:
        now = datetime.datetime.now()
    if isinstance(now, datetime.datetime):
        _d, _wd = now.date(), now.weekday()
    elif isinstance(now, datetime.date):
        _d, _wd = now, now.weekday()
    else:
        raise TypeError('is_trading_today 需要 date/datetime/None，得到 %r' % type(now))
    if _wd >= 5:
        # 周末：不可能判错，且省掉一次 import；保持 v3.1 起的短路语义
        return False
    _core = os.path.join(BASE, 'core')
    if _core not in sys.path:
        sys.path.insert(0, _core)
    try:
        from trading_calendar import is_trading_day as _f
        return bool(_f(_d))
    except Exception as e:  # noqa: BLE001
        log('CRITICAL 交易日历不可用 (%s: %s) → fail-closed 按非交易日处理（本日不会 spawn monitor/alert_engine）'
            % (type(e).__name__, e))
        _alert_calendar_down(e)
        return False


def log(msg):
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n'
    try:
        with open(WATCHDOG_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass
    try: sys.stdout.write(line); sys.stdout.flush()
    except Exception: pass

_ALERT_LAST = {'ts': 0.0}
ALERT_DEDUP_S = 1800          # 30 分钟去重：日历坏了会每 30s 触发一次判定，不能每轮都告警


def _alert_calendar_down(err):
    """交易日历加载失败 → 飞书告警（30 分钟去重）。

    [2026-09-25] 建立原因：v3.1 的 `except: pass` 让日历故障**完全无声**，
    缺陷潜伏了一整天（705 次空转）才被人发现。任何"降级"都必须可观测。
    webhook 从 `config/monitor_config.json` 的 `feishu.webhook_url` 读取（与 alert_engine
    同一配置真源，不硬编码）；纯 stdlib POST，不依赖任何第三方包。
    """
    now = time.time()
    if now - _ALERT_LAST['ts'] < ALERT_DEDUP_S:
        return
    _ALERT_LAST['ts'] = now
    try:
        cfg_path = os.path.join(BASE, 'config', 'monitor_config.json')
        with open(cfg_path, encoding='utf-8') as f:
            hook = (json.load(f).get('feishu') or {}).get('webhook_url') or ''
        if not hook:
            log('WARN 未取到 feishu.webhook_url，CRITICAL 告警仅落本地日志')
            return
        import urllib.request as _u
        text = ('[tpoint watchdog CRITICAL] 交易日历加载失败，已 fail-closed 按非交易日处理 '
                '—— 若今天本是交易日，则 monitor/alert_engine 不会被拉起，会漏监控。'
                'err=%s: %s' % (type(err).__name__, err))
        body = json.dumps({'msg_type': 'text', 'content': {'text': text}}).encode('utf-8')
        req = _u.Request(hook, data=body, headers={'Content-Type': 'application/json'})
        _u.urlopen(req, timeout=10).read()
        log('CRITICAL 告警已推送飞书（%ds 内不再重复）' % ALERT_DEDUP_S)
    except Exception as e:  # noqa: BLE001
        log('WARN CRITICAL 告警推送失败: %s' % e)

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
            [PS_EXE, '-NoProfile', '-Command', ps],
            capture_output=True, timeout=15, creationflags=0x08000000,
        )
        return r.returncode == 0
    except Exception as e:
        # ⚠️ fail-open：无法确认时返回 False = "没在跑" ⇒ 会重复 spawn。
        # 故此处必须让异常可见（原实现只在日志里留一行，容易被忽略）。
        log(f'CRITICAL _wmi_has {script_basename} 探测失败 ({type(e).__name__}: {e})'
            f' → 返回 False 可能导致重复拉起；检查 PS_EXE={PS_EXE}')
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
        log('watchdog v3.2 started (trading-day aware, fail-closed calendar)')
        targets = [
            ('monitor',      os.path.join(BASE, 'core', 'monitor.py'),
                os.path.join(LOGS, 'monitor_console.log')),
            ('alert_engine', os.path.join(BASE, 'core', 'alert_engine.py'),
                os.path.join(LOGS, 'alert_engine_console.log')),
        ]
        last_trading_state = None
        _quiet_last_beat = 0.0          # 非交易日心跳节流（见下 QUIET_HEARTBEAT_S）
        while True:
            trading = is_trading_today()
            state_changed = (trading != last_trading_state)
            if state_changed:
                log(f'trading day state: {trading} (was {last_trading_state})')
                last_trading_state = trading
            for label, script_path, log_path in targets:
                base = os.path.basename(script_path)
                if not self.is_running(label, base):
                    if trading:
                        log(f'{label} ({base}) not detected — restarting')
                        self.spawn(label, script_path, log_path)
                    elif state_changed:
                        # 非交易日：不 spawn（v3.1 修复 respawn storm 根因）。
                        # v3.2 起改为**边沿触发**：原来每轮都记，而本循环 30s 一轮、每轮两行
                        # ⇒ 假期约 5,760 行/日纯噪声（与 alert_engine_console.log 膨胀同类）。
                        # 现在只在交易日状态翻转的那一拍记一次，其余靠每小时心跳证明存活。
                        log(f'{label} ({base}) not running, non-trading day — skip spawn')
            if not trading and (time.time() - _quiet_last_beat) >= QUIET_HEARTBEAT_S:
                _quiet_last_beat = time.time()
                log('non-trading day — idle heartbeat（monitor/alert_engine 均未运行且不拉起，符合预期）')
            time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    _self_single_instance()
    Supervisor().run()
