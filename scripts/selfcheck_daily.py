#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tpoint 系统每日自检脚本 (daily self-check)

在每个交易日早上 9:00 由 Windows 计划任务触发，对 tpoint 做全面自检：
  1. 启动状态     —— 核心文件/解释器/模块可用性
  2. 服务运行     —— monitor / alert_engine 进程存活 + 单实例锁一致性
  3. 监控状态     —— metrics.json 心跳/扫描耗时/数据延迟/错误数
  4. 端口可达     —— mootdx TCP 7709 / 腾讯实时接口 / 飞书 webhook
  5. 资源使用     —— CPU / 内存 / 磁盘 / monitor 进程内存
  6. 计划任务     —— tpoint_monitor / tpoint_alert_engine 注册状态

自检完成后：
  - 生成 Markdown 报告 → logs/selfcheck/YYYY-MM-DD_HHMMSS.md
  - 异常追加到 logs/selfcheck/anomalies.log
  - 发现异常时通过「全局任务状态」飞书 webhook 自动推送告警
  - 控制台输出彩色摘要

用法:
  python selfcheck_daily.py              # 完整自检
  python selfcheck_daily.py --no-push    # 完整自检但不推送告警
  python selfcheck_daily.py --quick      # 跳过端口探测（快速模式）
"""
import os
import sys
import json
import time
import socket
import shutil
import argparse
import subprocess
import urllib.request
import requests  # 与生产 monitor 推送同源：继承系统代理/DNS，避免 urllib 误报不可达
import re
from datetime import datetime, timezone, timedelta

# ========== 路径配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CST = timezone(timedelta(hours=8))

DATA_DIR = os.path.join(BASE_DIR, 'data')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
SELFCHK_DIR = os.path.join(LOG_DIR, 'selfcheck')
ANOMALY_LOG = os.path.join(SELFCHK_DIR, 'anomalies.log')

VERSION_FILE = os.path.join(BASE_DIR, 'VERSION')
METRICS_FILE = os.path.join(DATA_DIR, 'metrics.json')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')
SIGNAL_FILE = os.path.join(DATA_DIR, 'signal.txt')
LOCK_FILE = os.path.join(DATA_DIR, '.monitor.svc.lock')
PID_FILE = os.path.join(DATA_DIR, '.monitor.svc.pid')
PROMPT_FILE = os.path.join(BASE_DIR, '..', '数据', '股票池', 'prompt-common.md')
CONFIG_FILE = os.path.join(BASE_DIR, 'config', 'monitor_config.json')
WATCHLIST_FILE = os.path.join(DATA_DIR, 'watchlist.json')
ALERT_PID_FILE = os.path.join(DATA_DIR, '.alert_engine.pid')   # alert_engine 单实例 PID（与 monitor 同机制，写于 core/alert_engine.py）

# 解释器（与 run_monitor.bat 一致：managed python 3.13.12）
MANAGED_PY = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe'
VENV_PY = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')

# 盘前状态通知飞书 webhook（与 monitor B/S 信号同群）
GLOBAL_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/1d241455-447b-4017-b9a3-4ecb61912369"

# 阈值（与 monitor_config.json 对齐，独立硬编码避免读配置失败）
SERVICE_STALE_S = 120      # 心跳过期阈值
SCAN_DUR_WARN_S = 45       # 扫描耗时警告
DATA_LAG_WARN_S = 300      # 行情延迟警告
DISK_WARN_PCT = 90         # 磁盘使用率警告
MEM_WARN_PCT = 85          # 内存使用率警告
MONITOR_MEM_WARN_MB = 300  # monitor 进程内存警告

# 通达信服务器列表（与 datasource.py _TDX_SERVERS 对齐，取前几个做探测）
TDX_SERVERS = [
    ('119.97.185.59', 7709),
    ('124.70.133.119', 7709),
    ('116.205.183.150', 7709),
    ('123.60.73.44', 7709),
]

for d in (SELFCHK_DIR,):
    os.makedirs(d, exist_ok=True)


# ========== 工具函数 ==========

def now_ts():
    return datetime.now(CST)


def _c(code, text):
    """ANSI 颜色（Windows 10+ 支持）。"""
    colors = {'green': 32, 'red': 31, 'yellow': 33, 'cyan': 36, 'gray': 90, 'bold': 1}
    return f"\033[{colors.get(code, 0)}m{text}\033[0m"


def is_trading_today():
    """是否 A 股交易日（与 monitor.is_trading_today 一致）。"""
    now = datetime.now(CST)
    if now.weekday() >= 5:
        return False
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
    return now.strftime('%Y-%m-%d') not in holidays_2026


def in_trading_session():
    """当前是否在交易时段内（9:25-11:31 / 13:00-15:01）。"""
    t = datetime.now(CST).time()
    from datetime import time as dtime
    morning = dtime(9, 25) <= t <= dtime(11, 31)
    afternoon = dtime(13, 0) <= t <= dtime(15, 1)
    return morning or afternoon


class CheckResult:
    """单项检查结果。"""

    def __init__(self, category, name, status, detail='', value=None, threshold=None):
        # status: PASS / WARN / FAIL / SKIP
        self.category = category
        self.name = name
        self.status = status
        self.detail = detail
        self.value = value
        self.threshold = threshold

    def icon(self):
        return {'PASS': '✅', 'WARN': '⚠️', 'FAIL': '❌', 'SKIP': '⏭️'}.get(self.status, '?')

    def color(self):
        return {'PASS': 'green', 'WARN': 'yellow', 'FAIL': 'red', 'SKIP': 'gray'}.get(self.status, 'gray')


def _get_python_procs():
    """获取所有 python 进程的 [(pid, cmdline)] 列表。

    多方法互补（union，按 PID 去重），任一方法在计划任务/自检会话下漏抓都不会导致
    整体为空——这是此前 alert_engine「未检测到」+ 计划任务 FAIL 误报的根因。
    优先级: wmic（本机验证最稳，含完整 commandline）> PowerShell > tasklist PIDs 兜底。
    """
    procs = []
    seen = set()

    def _add(pid, cmd):
        try:
            pid = int(pid)
        except Exception:
            return
        if pid in seen:
            return
        seen.add(pid)
        procs.append((pid, (cmd or '').strip()))

    # 方法1: wmic（本机验证可用，含完整 commandline；解析与旧兜底一致）
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where', "name like 'python%'", 'get', 'processid,commandline'],
            timeout=15, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out.strip().splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            # wmic 输出格式: CommandLine  PID（右对齐数字）
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                _add(parts[1], parts[0])
    except Exception:
        pass

    # 方法2: PowerShell Get-CimInstance
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
             "ForEach-Object { Write-Output ($_.ProcessId.ToString() + '||' + $_.CommandLine) }"],
            timeout=15, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out.strip().splitlines():
            line = line.strip()
            if '||' in line:
                pid_s, cmd = line.split('||', 1)
                if pid_s.strip().isdigit():
                    _add(pid_s, cmd)
    except Exception:
        pass

    # 方法3: tasklist PIDs 兜底（无 commandline，仅保证 PID 不漏）
    try:
        out = subprocess.check_output(
            ['tasklist', '/fi', 'imagename eq python.exe', '/fo', 'csv', '/nh'],
            timeout=10, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                _add(parts[1], '')
    except Exception:
        pass

    return procs


def _tasklist_python_pids():
    """用 tasklist 获取 python 进程 PID 列表（兜底，不含命令行）。"""
    try:
        out = subprocess.check_output(
            ['tasklist', '/fi', 'imagename eq python.exe', '/fo', 'csv', '/nh'],
            timeout=10, stderr=subprocess.DEVNULL, text=True, errors='replace')
        pids = []
        for line in out.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
        # 也查 python3.x.exe
        out2 = subprocess.check_output(
            ['tasklist', '/fi', 'imagename eq python3*', '/fo', 'csv', '/nh'],
            timeout=10, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out2.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
        return pids
    except Exception:
        return []


def _proc_alive(pid):
    """进程是否存活（跨用户，Windows 用 ctypes 可靠判定）。"""
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            ec = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(ec))
            kernel32.CloseHandle(h)
            return ec.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_pid_file(path):
    """读取 PID 文件，返回 int PID；文件缺失/内容非数字返回 None。

    作为 monitor / alert_engine 进程存活判定的权威来源（与 watchdog 单实例机制一致），
    不依赖 cmdline 抓取——后者在计划任务/自检会话下 Get-CimInstance 常返回空导致误报。
    """
    try:
        if os.path.exists(path):
            c = open(path, encoding='utf-8').read().strip()
            if c.isdigit():
                return int(c)
    except Exception:
        pass
    return None


def _query_scheduled_tasks():
    """精确查询 tpoint 计划任务。返回 {name: {state, lastrun, lastresult, cmd}}。
    用精确任务名查询，避免模糊匹配 'monitor' 误命中系统任务(FamilySafetyMonitor 等)
    导致 Get-ScheduledTaskInfo 报错。"""
    result = {}
    names = ['tpoint_monitor', 'tpoint_alert_engine', 'tpoint_selfcheck']
    ps_names = ','.join([f'"{n}"' for n in names])
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "foreach ($n in @(%s)) { "
             "$t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
             "if ($t) { "
             "$i = Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue; "
             "$a = $t.Actions[0]; "
             "Write-Output ($n + '||' + $t.State + '||' + $i.LastRunTime + '||' + $i.LastTaskResult + '||' + $t.Principal.UserId + '||' + $a.Execute + ' ' + $a.Arguments) "
             "} else { Write-Output ($n + '||NOTFOUND') } }" % ps_names],
            timeout=20, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out.strip().splitlines():
            line = line.strip()
            if '||' not in line:
                continue
            parts = line.split('||')
            if len(parts) >= 2 and parts[1] == 'NOTFOUND':
                continue  # 未注册，不在 result 中体现（check 函数会报 FAIL）
            if len(parts) >= 6:
                result[parts[0]] = {
                    'state': parts[1],
                    'lastrun': parts[2],
                    'lastresult': parts[3],
                    'principal': parts[4],
                    'cmd': parts[5],
                }
    except Exception as e:
        result['_error'] = str(e)
    return result


def _system_resources():
    """获取系统资源使用率（用 PowerShell，不依赖 psutil）。"""
    res = {}
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "$os = Get-CimInstance Win32_OperatingSystem; "
             "$cpu = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average; "
             "$mem_total = $os.TotalVisibleMemorySize; "
             "$mem_free = $os.FreePhysicalMemory; "
             "$mem_pct = [math]::Round(($mem_total - $mem_free) / $mem_total * 100, 1); "
             "Write-Output ($cpu.ToString() + '||' + $mem_pct.ToString() + '||' + $mem_total.ToString()); "
             "$d = Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\"; "
             "$disk_pct = [math]::Round($d.Size / ($d.Size + $d.FreeSpace) * 100, 1); "
             "Write-Output ($disk_pct.ToString() + '||' + [math]::Round($d.FreeSpace/1GB,1).ToString() + '||' + [math]::Round($d.Size/1GB,1).ToString())"],
            timeout=15, stderr=subprocess.DEVNULL, text=True, errors='replace')
        lines = out.strip().splitlines()
        if len(lines) >= 1 and '||' in lines[0]:
            p = lines[0].split('||')
            res['cpu_pct'] = float(p[0]) if p[0] else None
            res['mem_pct'] = float(p[1])
            res['mem_total_kb'] = int(p[2])
        if len(lines) >= 2 and '||' in lines[1]:
            p = lines[1].split('||')
            res['disk_c_pct'] = float(p[0])
            res['disk_c_free_gb'] = float(p[1])
            res['disk_c_total_gb'] = float(p[2])
    except Exception:
        pass
    return res


def _proc_mem_mb(pid):
    """获取进程内存（MB）。"""
    try:
        out = subprocess.check_output(
            ['tasklist', '/fi', f'pid eq {pid}', '/fo', 'csv', '/nh'],
            timeout=8, stderr=subprocess.DEVNULL, text=True, errors='replace')
        for line in out.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 5:
                # "python.exe","20620","Console","1","83,852 K"
                mem_s = parts[4].replace(',', '').replace(' K', '').replace('K', '').strip()
                return int(mem_s) / 1024.0
    except Exception:
        pass
    return None


def _tcp_probe(ip, port, timeout=2.0):
    """TCP 握手探测。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _https_probe(host, path='/', timeout=5):
    """HTTPS 可达性探测。

    [2026-08-12 修复] 改用 requests（与生产 monitor 推送同源）。原 urllib.request 在
    Windows 下不继承系统代理/DNS，常对可达主机误报不可达，造成自检 ❌ FAIL 误报。
    判定口径：能建立连接并收到任何 HTTP 响应（含 4xx）即视为链路可达；仅连接超时/
    解析失败/拒绝连接才算不可达。
    """
    url = f'https://{host}{path}'
    try:
        requests.head(url, timeout=timeout, allow_redirects=True)
        return True
    except Exception:
        try:
            requests.get(url, timeout=timeout, allow_redirects=True)
            return True
        except Exception:
            return False


def _push_feishu(payload):
    """通过 webhook 推送。payload 可以是 str(纯文本告警) 或 dict(交互卡片)。

    [2026-08-12 修复] 改用 requests（与生产 monitor 推送同源），继承系统代理，
    避免 urllib 在代理环境下推送失败。
    """
    try:
        if isinstance(payload, str):
            data = {"msg_type": "text", "content": {"text": payload}}
        elif isinstance(payload, dict):
            data = payload
        else:
            return f"PUSH_FAIL: unsupported payload type {type(payload)}"
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        resp = requests.post(
            GLOBAL_WEBHOOK, data=body,
            headers={"Content-Type": "application/json"}, timeout=15)
        return resp.text
    except Exception as e:
        return f"PUSH_FAIL: {e}"


# ========== 检查项 ==========

def check_startup():
    """1. 启动状态检查。"""
    results = []

    # 1.1 VERSION 文件
    try:
        with open(VERSION_FILE, encoding='utf-8') as f:
            ver = f.read().strip()
        results.append(CheckResult('启动状态', 'VERSION 文件', 'PASS',
                                   f'版本 {ver}', value=ver))
    except Exception as e:
        results.append(CheckResult('启动状态', 'VERSION 文件', 'FAIL', str(e)))

    # 1.2 持仓文件
    if os.path.exists(PROMPT_FILE):
        sz = os.path.getsize(PROMPT_FILE)
        results.append(CheckResult('启动状态', '持仓文件 prompt-common.md', 'PASS',
                                   f'{sz} 字节', value=sz))
    else:
        results.append(CheckResult('启动状态', '持仓文件 prompt-common.md', 'FAIL',
                                   f'文件不存在: {PROMPT_FILE}（monitor 将回退硬编码标的）'))

    # 1.3 managed python 可用
    if os.path.exists(MANAGED_PY):
        try:
            out = subprocess.check_output([MANAGED_PY, '--version'],
                                          timeout=10, stderr=subprocess.STDOUT, text=True)
            results.append(CheckResult('启动状态', 'Managed Python 解释器', 'PASS',
                                       out.strip(), value=out.strip()))
        except Exception as e:
            results.append(CheckResult('启动状态', 'Managed Python 解释器', 'FAIL', str(e)))
    else:
        results.append(CheckResult('启动状态', 'Managed Python 解释器', 'FAIL',
                                   f'不存在: {MANAGED_PY}'))

    # 1.4 核心模块可导入
    mods_ok, mods_fail = [], []
    for mod in ('monitor', 'alert_engine', 'datasource', 'miji_alpha', 'exit_manager', 'indicators'):
        try:
            subprocess.check_output(
                [MANAGED_PY, '-c', f'import sys; sys.path.insert(0, r"{os.path.join(BASE_DIR, "core")}"); import {mod}'],
                timeout=20, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, 'PYTHONPATH': os.path.join(BASE_DIR, 'venv', 'Lib', 'site-packages') + ';' + os.path.join(BASE_DIR, 'venv', 'Lib') + ';' + BASE_DIR})
            mods_ok.append(mod)
        except Exception:
            mods_fail.append(mod)
    if not mods_fail:
        results.append(CheckResult('启动状态', '核心模块导入', 'PASS',
                                   f'{len(mods_ok)} 个模块全部可导入'))
    else:
        results.append(CheckResult('启动状态', '核心模块导入', 'FAIL',
                                   f'导入失败: {",".join(mods_fail)}；成功: {",".join(mods_ok)}'))

    # 1.5 关键目录
    for d in ('core', 'data', 'logs', 'config'):
        p = os.path.join(BASE_DIR, d)
        if os.path.isdir(p):
            results.append(CheckResult('启动状态', f'目录 {d}/', 'PASS', '存在'))
        else:
            results.append(CheckResult('启动状态', f'目录 {d}/', 'FAIL', f'不存在: {p}'))

    # 1.6 监控标的来源（统一为 watchlist.json，禁止硬编码）
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, encoding='utf-8') as f:
                wl = json.load(f)
            if isinstance(wl, dict) and len(wl) >= 1:
                names = ', '.join(list(wl.values())[:6])
                more = '' if len(wl) <= 6 else f' 等{len(wl)}个'
                results.append(CheckResult('启动状态', '监控标的来源', 'PASS',
                                           f'{len(wl)} 个 ({names}{more}) 来源 watchlist.json',
                                           value=len(wl)))
            else:
                results.append(CheckResult('启动状态', '监控标的来源', 'FAIL',
                                           f'watchlist.json 为空或格式错误（应为非空的 {{sym: name}} 字典）'))
        else:
            results.append(CheckResult('启动状态', '监控标的来源', 'FAIL',
                                       f'watchlist.json 不存在: {WATCHLIST_FILE}（monitor 无标的可监控）'))
    except Exception as e:
        results.append(CheckResult('启动状态', '监控标的来源', 'FAIL',
                                   f'读取 watchlist.json 失败: {e}'))

    return results


def check_services(trading_day):
    """2. 服务运行状态检查。"""
    results = []

    # 2.1 monitor 进程存活
    # 主判定：PID 文件 + 进程存活（与 watchdog/monitor 同机制，最可靠，不依赖 cmdline 抓取）
    procs = _get_python_procs()
    pid_in_file = _read_pid_file(PID_FILE)
    monitor_pids = [pid_in_file] if (pid_in_file and _proc_alive(pid_in_file)) else []
    # 兜底1: cmdline 扫描（Get-CimInstance/tasklist 可能漏抓）
    if not monitor_pids:
        monitor_pids = [pid for pid, cmd in procs if 'monitor.py' in cmd]
    # 兜底2: PID 文件存在但上面未抓到（cmdline 缺失但进程在）
    if not monitor_pids and pid_in_file and _proc_alive(pid_in_file):
        monitor_pids = [pid_in_file]

    if monitor_pids:
        pid = monitor_pids[0]
        mem = _proc_mem_mb(pid)
        mem_str = f'，内存 {mem:.0f}MB' if mem else ''
        results.append(CheckResult('服务运行', 'monitor 进程', 'PASS',
                                   f'PID {pid}{mem_str}', value=pid))
    else:
        # 非交易日 monitor 按设计退出，降级为 WARN
        status = 'WARN' if not trading_day else 'FAIL'
        msg = 'monitor 进程未检测到'
        if not trading_day:
            msg += '（非交易日，monitor 按设计退出）'
        results.append(CheckResult('服务运行', 'monitor 进程', status, msg))

    # 2.2 alert_engine 进程存活
    # 与 monitor 同机制：PID 文件 + 存活判定优先（此前无 PID 兜底 → cmdline 漏抓即误报 FAIL）
    ae_pid = _read_pid_file(ALERT_PID_FILE)
    engine_pids = [ae_pid] if (ae_pid and _proc_alive(ae_pid)) else []
    if not engine_pids:
        engine_pids = [pid for pid, cmd in procs if 'alert_engine' in cmd]
    if not engine_pids and ae_pid and _proc_alive(ae_pid):
        engine_pids = [ae_pid]

    if engine_pids:
        results.append(CheckResult('服务运行', 'alert_engine 进程', 'PASS',
                                   f'PID {engine_pids[0]}', value=engine_pids[0]))
    else:
        status = 'WARN' if not trading_day else 'FAIL'
        msg = 'alert_engine 进程未检测到'
        if not trading_day:
            msg += '（非交易日按设计退出）'
        results.append(CheckResult('服务运行', 'alert_engine 进程', status, msg))

    # 2.3 单实例锁一致性
    pid_in_file = None
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                c = f.read().strip()
            if c.isdigit():
                pid_in_file = int(c)
    except Exception:
        pass

    if pid_in_file is not None:
        alive = _proc_alive(pid_in_file)
        if alive:
            results.append(CheckResult('服务运行', '单实例锁一致性', 'PASS',
                                       f'PID 文件指向 {pid_in_file}，进程存活'))
        else:
            results.append(CheckResult('服务运行', '单实例锁一致性', 'WARN',
                                       f'PID 文件指向 {pid_in_file}，但进程已退出（stale lock，下次启动会自动清理）'))
    else:
        if trading_day:
            results.append(CheckResult('服务运行', '单实例锁一致性', 'WARN',
                                       'PID 文件缺失或无效（monitor 未启动或异常退出）'))
        else:
            results.append(CheckResult('服务运行', '单实例锁一致性', 'PASS',
                                       '非交易日无 PID 文件（正常）'))

    # 2.4 自检脚本进程去重检查（避免多个自检实例）
    self_pids = [pid for pid, cmd in procs if 'selfcheck_daily' in cmd and pid != os.getpid()]
    if self_pids:
        results.append(CheckResult('服务运行', '自检脚本实例', 'WARN',
                                   f'检测到其他自检进程运行中: {self_pids}'))
    else:
        results.append(CheckResult('服务运行', '自检脚本实例', 'PASS', '当前为唯一实例'))

    return results


def check_monitoring(trading_day):
    """3. 监控状态/指标检查。"""
    results = []

    # 3.1 metrics.json 可读
    metrics = None
    try:
        with open(METRICS_FILE, encoding='utf-8') as f:
            metrics = json.load(f)
        results.append(CheckResult('监控状态', 'metrics.json 可读', 'PASS',
                                   f'最近心跳 {datetime.fromtimestamp(metrics.get("ts", 0), CST).strftime("%H:%M:%S")}'))
    except Exception as e:
        results.append(CheckResult('监控状态', 'metrics.json 可读', 'FAIL',
                                   f'读取失败: {e}（monitor 未运行或未写入）'))
        return results  # 后续检查依赖 metrics

    now = time.time()
    ts = metrics.get('ts', 0)
    age = int(now - ts) if ts else None

    # 3.2 心跳新鲜度
    if age is not None:
        if age <= SERVICE_STALE_S:
            results.append(CheckResult('监控状态', '心跳新鲜度', 'PASS',
                                       f'心跳年龄 {age}s（≤{SERVICE_STALE_S}s）',
                                       value=age, threshold=SERVICE_STALE_S))
        else:
            # 非交易时段放宽（monitor keepalive 仍应每 15s 写，但容忍度提高）
            status = 'FAIL' if (trading_day and in_trading_session()) else 'WARN'
            results.append(CheckResult('监控状态', '心跳新鲜度', status,
                                       f'心跳停滞 {age}s（>{SERVICE_STALE_S}s），monitor 可能卡死或退出',
                                       value=age, threshold=SERVICE_STALE_S))

    # 3.3 扫描耗时合理性（仅交易时段强校验，堵"空转/未扫描"虚假健康）
    # 非交易时段 monitor 走 keepalive（scan_duration_s=0.0 合法），跳过校验。
    dur = metrics.get('scan_duration_s')
    session = in_trading_session()
    if dur is not None and session:
        if dur <= 0 or dur < 0.1:
            results.append(CheckResult('监控状态', '扫描耗时合理性', 'FAIL',
                f'{dur}s（<0.1s 或=0，疑似空转/未真正扫描/tf=None），正常实扫 3 标的约 0.4-0.6s',
                value=dur, threshold=0.1))
        elif dur <= 3:
            results.append(CheckResult('监控状态', '扫描耗时合理性', 'PASS',
                f'{dur}s（正常 1-3s）', value=dur, threshold=3))
        elif dur <= 10:
            results.append(CheckResult('监控状态', '扫描耗时合理性', 'WARN',
                f'{dur}s（偏慢，3-10s）', value=dur, threshold=10))
        else:
            results.append(CheckResult('监控状态', '扫描耗时合理性', 'WARN',
                f'{dur}s（过慢 >10s）', value=dur, threshold=10))
    elif dur is not None:
        results.append(CheckResult('监控状态', '扫描耗时合理性', 'SKIP',
            f'非交易时段（{dur}s，keepalive 合法），跳过合理性校验'))
    else:
        results.append(CheckResult('监控状态', '扫描耗时合理性', 'SKIP',
            'metrics 无 scan_duration_s 字段'))

    # 3.4 行情棒时效性 last_bar_ts（仅交易时段强校验，堵"陈旧/空数据"虚假健康）
    # <300s PASS / 300s~30min WARN / >30min FAIL；交易时段为 null → FAIL（未扫描）。
    last_bar = metrics.get('last_bar_ts')
    if last_bar and session:
        lag = int(now - last_bar)
        if lag < 300:
            results.append(CheckResult('监控状态', '行情棒时效性', 'PASS',
                f'行情新鲜（{lag}s < 300s）', value=lag, threshold=300))
        elif lag <= 1800:
            results.append(CheckResult('监控状态', '行情棒时效性', 'WARN',
                f'行情偏旧（{lag}s，300s~30min）', value=lag, threshold=1800))
        else:
            results.append(CheckResult('监控状态', '行情棒时效性', 'FAIL',
                f'行情陈旧 {lag}s（>30min），monitor 可能未扫描/数据源中断',
                value=lag, threshold=1800))
    elif last_bar is None and session:
        results.append(CheckResult('监控状态', '行情棒时效性', 'FAIL',
            'last_bar_ts 为 null（交易时段无行情棒，疑似 tf=None/未真正扫描）'))
    elif session:
        results.append(CheckResult('监控状态', '行情棒时效性', 'SKIP',
            '无 last_bar_ts 字段'))
    else:
        results.append(CheckResult('监控状态', '行情棒时效性', 'SKIP',
            '非交易时段，跳过行情时效校验'))

    # 3.5 本轮错误数（含 outer_err 闭环：扫描崩溃计入 errors）
    # 交易时段 errors>0 → FAIL（compute 抛异常 / 扫描崩溃均属真实故障）；
    # 非交易时段 keepalive 不扫描，无 errors 语义，跳过。
    errs = metrics.get('errors', 0)
    if session:
        if errs == 0:
            results.append(CheckResult('监控状态', '本轮扫描错误', 'PASS', '0 次'))
        else:
            results.append(CheckResult('监控状态', '本轮扫描错误', 'FAIL',
                f'{errs} 次（含可能的扫描崩溃 outer_err），检查标的/网络/数据源',
                value=errs, threshold=0))
    else:
        results.append(CheckResult('监控状态', '本轮扫描错误', 'SKIP',
            '非交易时段，跳过错误数校验'))

    # 3.6 state.json 今日刷新
    try:
        mtime = os.path.getmtime(STATE_FILE)
        mdate = datetime.fromtimestamp(mtime, CST).strftime('%Y-%m-%d')
        today = datetime.now(CST).strftime('%Y-%m-%d')
        if mdate == today:
            results.append(CheckResult('监控状态', 'state.json 今日刷新', 'PASS',
                                       f'最后修改 {datetime.fromtimestamp(mtime, CST).strftime("%H:%M:%S")}'))
        else:
            results.append(CheckResult('监控状态', 'state.json 今日刷新', 'WARN',
                                       f'最后修改日期 {mdate}（非今日），monitor 今日未运行或未写状态'))
    except Exception as e:
        results.append(CheckResult('监控状态', 'state.json 今日刷新', 'FAIL', str(e)))

    # 3.7 signal.txt 存在
    if os.path.exists(SIGNAL_FILE):
        results.append(CheckResult('监控状态', 'signal.txt', 'PASS', '存在'))
    else:
        results.append(CheckResult('监控状态', 'signal.txt', 'WARN',
                                   '不存在（monitor 启动后创建，盘前可能尚未创建）'))

    return results


def check_signal_output(trading_day):
    """3.8 当日信号产出计数（堵"零信号/静默漏推"盲区）。

    根因（2026-08-07 全天零信号事件）：selfcheck 仅在 09:00 盘前跑，3.3/3.4/3.5 数据检查全被
    in_trading_session() 门控 SKIP；且原 3.7 只查 signal.txt 是否存在，从不统计"今日已产出多少信号"。
    结果盘中 monitor 僵死/首扫误吞导致全天零信号，selfcheck 完全无感。

    本检查：交易时段内、开盘≥30min、行情数据新鲜（last_bar_ts<300s）却 0 信号 → FAIL，
    精准捕获"monitor 活着但不发信号 / 首扫抑制误吞 / 空转"类故障。
    """
    results = []
    session = in_trading_session()
    now = datetime.now(CST)
    today = now.strftime('%Y-%m-%d')

    if not session:
        results.append(CheckResult('信号产出', '当日信号计数', 'SKIP',
                                   '非交易时段，跳过（无信号属正常）'))
        return results

    # —— 统计 signal.txt 今日信号条数（按日期头切分 section，计时间格式 [HH:MM:SS] 头部行）——
    sig_count = 0
    try:
        with open(SIGNAL_FILE, encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        started = False
        for ln in lines:
            s = ln.strip()
            if re.match(r'^\[' + re.escape(today) + r'\]\s*$', s):
                started = True
                continue
            if re.match(r'^\[\d{4}-\d{2}-\d{2}\]\s*$', s):
                started = False
                continue
            if started and re.match(r'^\[\d{2}:\d{2}:\d{2}\]', s):
                sig_count += 1
    except Exception as e:
        results.append(CheckResult('信号产出', '当日信号计数', 'WARN',
                                   f'signal.txt 读取失败: {e}'))
        return results

    # —— 推送审计交叉验证 ——
    pa = None
    try:
        pa_path = os.path.join(DATA_DIR, 'push_audit.jsonl')
        if os.path.exists(pa_path):
            pa = 0
            with open(pa_path, encoding='utf-8', errors='ignore') as f:
                for ln in f:
                    if today in ln:
                        pa += 1
    except Exception:
        pa = None

    # —— 数据新鲜度（基于 metrics.last_bar_ts）——
    data_fresh = True
    try:
        with open(METRICS_FILE, encoding='utf-8') as f:
            m = json.load(f)
        last_bar = m.get('last_bar_ts')
        if last_bar:
            data_fresh = (time.time() - last_bar) < 300
    except Exception:
        data_fresh = True  # 读不到则假定新鲜，交给 3.4 判定

    # —— 开盘后分钟数 ——
    from datetime import time as dtime
    t = now.time()
    if dtime(9, 25) <= t <= dtime(11, 31):
        open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elif dtime(13, 0) <= t <= dtime(15, 1):
        open_dt = now.replace(hour=13, minute=0, second=0, microsecond=0)
    else:
        open_dt = now
    elapsed = int((now - open_dt).total_seconds() / 60)

    # —— 判定 ——
    # 注意：信号计数无法区分"行情清淡"与"静默故障"（后者已由 3.2/3.5/3.4 覆盖），
    # 故保守设阈值：<45min 不判（给市场产出早盘信号的时间）；45~90min 仅 WARN 观察；
    # ≥90min 且行情新鲜仍 0 信号才 FAIL（长时无信号 + 数据正常，疑似链路故障）。
    pa_str = f'，推送审计 {pa} 条' if pa is not None else ''
    if elapsed < 45:
        results.append(CheckResult('信号产出', '当日信号计数', 'SKIP',
            f'开盘仅 {elapsed} 分钟（<45 暂不判定）；signal.txt 今日 {sig_count} 条{pa_str}', value=sig_count))
    elif sig_count == 0 and not data_fresh:
        results.append(CheckResult('信号产出', '当日信号计数', 'WARN',
            f'开盘 {elapsed} 分钟零信号，但行情数据陈旧（last_bar_ts 滞后>300s）——信号数为0可能由数据源中断导致，详见 3.4',
            value=sig_count))
    elif sig_count == 0 and data_fresh and elapsed < 90:
        results.append(CheckResult('信号产出', '当日信号计数', 'WARN',
            f'开盘 {elapsed} 分钟、行情正常但 0 信号，暂观察（45~90min）；若持续至 90min 且仍 0 将升级 FAIL',
            value=sig_count))
    elif sig_count == 0 and data_fresh and elapsed >= 90:
        results.append(CheckResult('信号产出', '当日信号计数', 'FAIL',
            f'开盘 {elapsed} 分钟、行情数据正常（last_bar_ts 新鲜），但 signal.txt 今日 0 条信号——'
            f'疑似信号链路故障 / 首扫抑制误吞 / monitor 空转，需人工介入',
            value=sig_count, threshold=0))
    else:
        results.append(CheckResult('信号产出', '当日信号计数', 'PASS',
            f'开盘 {elapsed} 分钟，signal.txt 今日 {sig_count} 条信号{pa_str}', value=sig_count))
    return results


def check_ports(quick=False):
    """4. 核心服务端口/数据源可达性。"""
    results = []

    if quick:
        results.append(CheckResult('端口可达', '数据源探测', 'SKIP', '快速模式跳过'))
        return results

    # 4.1 mootdx TCP 7709（至少一个服务器可达）
    reachable = []
    for ip, port in TDX_SERVERS:
        if _tcp_probe(ip, port, timeout=2.0):
            reachable.append(ip)
    if reachable:
        results.append(CheckResult('端口可达', 'mootdx TCP 7709', 'PASS',
                                   f'{len(reachable)}/{len(TDX_SERVERS)} 服务器可达: {reachable[0]} 等'))
    else:
        results.append(CheckResult('端口可达', 'mootdx TCP 7709', 'FAIL',
                                   f'全部 {len(TDX_SERVERS)} 服务器不可达，行情数据源中断风险'))

    # 4.2 腾讯实时接口
    if _https_probe('qt.gtimg.cn', '/', timeout=5):
        results.append(CheckResult('端口可达', '腾讯实时接口 qt.gtimg.cn', 'PASS', 'HTTPS 可达'))
    else:
        results.append(CheckResult('端口可达', '腾讯实时接口 qt.gtimg.cn', 'WARN',
                                   'HTTPS 不可达（兜底数据源失效，主源 mootdx 仍可用则不影响）'))

    # 4.3 飞书 webhook
    if _https_probe('open.feishu.cn', '/', timeout=5):
        results.append(CheckResult('端口可达', '飞书 webhook open.feishu.cn', 'PASS', 'HTTPS 可达'))
    else:
        results.append(CheckResult('端口可达', '飞书 webhook open.feishu.cn', 'FAIL',
                                   'HTTPS 不可达，信号/告警推送将失败'))

    return results


def check_resources():
    """5. 关键资源使用率。"""
    results = []
    res = _system_resources()

    # 5.1 CPU
    cpu = res.get('cpu_pct')
    if cpu is not None:
        status = 'PASS' if cpu < 80 else ('WARN' if cpu < 95 else 'FAIL')
        results.append(CheckResult('资源使用', 'CPU 使用率', status,
                                   f'{cpu}%', value=cpu, threshold=80))
    else:
        results.append(CheckResult('资源使用', 'CPU 使用率', 'SKIP', '无法获取'))

    # 5.2 内存
    mem = res.get('mem_pct')
    if mem is not None:
        status = 'PASS' if mem < MEM_WARN_PCT else 'WARN'
        total_gb = res.get('mem_total_kb', 0) / 1024 / 1024
        results.append(CheckResult('资源使用', '内存使用率', status,
                                   f'{mem}%（总 {total_gb:.1f}GB）',
                                   value=mem, threshold=MEM_WARN_PCT))
    else:
        results.append(CheckResult('资源使用', '内存使用率', 'SKIP', '无法获取'))

    # 5.3 磁盘 C:
    disk = res.get('disk_c_pct')
    if disk is not None:
        status = 'PASS' if disk < DISK_WARN_PCT else ('WARN' if disk < 95 else 'FAIL')
        free = res.get('disk_c_free_gb', 0)
        results.append(CheckResult('资源使用', '磁盘 C: 使用率', status,
                                   f'{disk}%（剩余 {free}GB）',
                                   value=disk, threshold=DISK_WARN_PCT))
    else:
        results.append(CheckResult('资源使用', '磁盘 C: 使用率', 'SKIP', '无法获取'))

    # 5.4 monitor 进程内存
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        mem_mb = _proc_mem_mb(pid)
        if mem_mb is not None:
            status = 'PASS' if mem_mb < MONITOR_MEM_WARN_MB else 'WARN'
            results.append(CheckResult('资源使用', 'monitor 进程内存', status,
                                       f'{mem_mb:.0f}MB（PID {pid}）',
                                       value=round(mem_mb, 1), threshold=MONITOR_MEM_WARN_MB))
        else:
            results.append(CheckResult('资源使用', 'monitor 进程内存', 'SKIP',
                                       '无法获取（进程可能未运行）'))
    except Exception:
        results.append(CheckResult('资源使用', 'monitor 进程内存', 'SKIP', 'PID 文件缺失'))

    return results


def check_scheduled_tasks():
    """6. 计划任务注册状态。"""
    results = []
    tasks = _query_scheduled_tasks()

    if '_error' in tasks:
        results.append(CheckResult('计划任务', '任务查询', 'WARN',
                                   f'查询失败: {tasks["_error"]}'))
        return results

    if not tasks:
        results.append(CheckResult('计划任务', '任务查询', 'WARN',
                                   '未找到任何 tpoint 相关计划任务（可能未注册或查询权限不足）'))
        return results

    expected = {'tpoint_monitor', 'tpoint_alert_engine'}
    procs = _get_python_procs()
    # 进程存活判定：PID 文件 + 存活探测为权威源（与 watchdog 单实例机制一致），
    # cmdline 匹配仅作兜底——计划任务/自检会话下 Get-CimInstance 常返回空导致误报。
    _mon_pid = _read_pid_file(PID_FILE)
    _ae_pid = _read_pid_file(ALERT_PID_FILE)
    running = {
        'tpoint_monitor': (_mon_pid is not None and _proc_alive(_mon_pid))
                          or any('monitor.py' in c for _, c in procs),
        'tpoint_alert_engine': (_ae_pid is not None and _proc_alive(_ae_pid))
                               or any('alert_engine' in c for _, c in procs),
    }
    found = set(tasks.keys())
    for name in expected:
        if name in found:
            t = tasks[name]
            state = t.get('state', 'Unknown')
            cmd = t.get('cmd', '')
            status = 'PASS' if state == 'Ready' else ('WARN' if state in ('Running',) else 'FAIL')
            principal = t.get('principal', 'N/A')
            results.append(CheckResult('计划任务', name, status,
                                       f'状态={state}，账户={principal}，最后运行={t.get("lastrun","N/A")}，'
                                       f'结果={t.get("lastresult","N/A")}，命令={cmd[:60]}'))
        else:
            # 服务经 watchdog 守护进程（scripts/watchdog.py）保活，非计划任务，以进程存活为准，避免误报 FAIL
            if running.get(name):
                results.append(CheckResult('计划任务', name, 'PASS',
                                           '未注册为计划任务，但进程存活（由 watchdog 守护 scripts/watchdog.py 保活，属正常设计）'))
            else:
                results.append(CheckResult('计划任务', name, 'FAIL',
                                           '任务未注册且进程未检测到（检查 watchdog 是否在跑；或运行 scripts/launch_watchdog.py）'))

    # 自检任务本身
    if 'tpoint_selfcheck' in found:
        results.append(CheckResult('计划任务', 'tpoint_selfcheck', 'PASS', '已注册'))
    else:
        results.append(CheckResult('计划任务', 'tpoint_selfcheck', 'WARN',
                                   '自检任务未注册（运行 scripts/install_selfcheck.ps1 安装）'))

    return results


# ========== 报告与告警 ==========

def build_report(all_results, trading_day, quick):
    """生成 Markdown 报告。"""
    now = now_ts()
    lines = []
    lines.append(f"# tpoint 系统自检报告")
    lines.append(f"")
    lines.append(f"- **检查时间**: {now.strftime('%Y-%m-%d %H:%M:%S')} CST")
    lines.append(f"- **交易日**: {'是' if trading_day else '否'}")
    lines.append(f"- **交易时段**: {'是' if in_trading_session() else '否'}")
    lines.append(f"- **检查模式**: {'快速（跳过端口探测）' if quick else '完整'}")
    lines.append(f"")

    # 统计
    stats = {'PASS': 0, 'WARN': 0, 'FAIL': 0, 'SKIP': 0}
    for r in all_results:
        stats[r.status] = stats.get(r.status, 0) + 1
    total = len(all_results)
    overall = 'FAIL' if stats['FAIL'] > 0 else ('WARN' if stats['WARN'] > 0 else 'PASS')
    icon = {'PASS': '✅', 'WARN': '⚠️', 'FAIL': '❌'}[overall]

    lines.append(f"## 总体结果: {icon} {overall}")
    lines.append(f"")
    lines.append(f"| 状态 | 数量 |")
    lines.append(f"|------|------|")
    for s in ('PASS', 'WARN', 'FAIL', 'SKIP'):
        lines.append(f"| {s} | {stats[s]} |")
    lines.append(f"")

    # 按类别分组
    categories = []
    for r in all_results:
        if r.category not in categories:
            categories.append(r.category)

    for cat in categories:
        lines.append(f"## {cat}")
        lines.append(f"")
        lines.append(f"| 状态 | 检查项 | 详情 |")
        lines.append(f"|------|--------|------|")
        for r in all_results:
            if r.category != cat:
                continue
            detail = r.detail.replace('|', '\\|')
            lines.append(f"| {r.icon()} {r.status} | {r.name} | {detail} |")
        lines.append(f"")

    # 异常项汇总
    anomalies = [r for r in all_results if r.status in ('FAIL', 'WARN')]
    if anomalies:
        lines.append(f"## 异常项与建议处理措施")
        lines.append(f"")
        for r in anomalies:
            lines.append(f"### {r.icon()} [{r.status}] {r.category} / {r.name}")
            lines.append(f"- **详情**: {r.detail}")
            lines.append(f"- **建议**: {_suggestion(r)}")
            lines.append(f"")
    else:
        lines.append(f"## 无异常 ✨")
        lines.append(f"")

    return '\n'.join(lines), overall, stats


def _suggestion(r):
    """根据检查项给出处理建议。"""
    key = f"{r.category}/{r.name}"
    suggestions = {
        '服务运行/monitor 进程': '检查 run_monitor.bat 是否在运行；手动执行 `scripts/run_monitor.bat` 重启；查看 logs/monitor_fatal.log 和 monitor_lifecycle.log。',
        '服务运行/alert_engine 进程': '检查 run_engine.bat 是否在运行；手动执行 `scripts/run_engine.bat` 重启；查看 logs/engine_crash.log。',
        '服务运行/单实例锁一致性': 'monitor 单实例锁已切到 data/.monitor.svc.lock/.monitor.svc.pid（旧 .monitor.lock 可能被 Session0 僵尸占用）。若 stale，删 .monitor.svc.* 并重启 monitor。',
        '监控状态/metrics.json 可读': 'monitor 未启动或未写入心跳。先确认 monitor 进程存活，再检查 data/ 目录权限。',
        '监控状态/心跳新鲜度': 'monitor 可能卡在 mootdx 数据源连接。查看 monitor_console.log 最后输出；考虑重启 monitor。',
        '监控状态/扫描耗时合理性': '扫描耗时异常短（≤0.5s/0）说明 monitor 未真正扫描（可能 tf=None 或空转）。检查 monitor 进程是否存活、logs/monitor_console.log 是否每轮抛 compute exception；必要时清理锁文件并重启 monitor。',
        '监控状态/行情棒时效性': '行情棒陈旧/为空，说明 monitor 交易时段未拉到数据。检查 mootdx TCP 7709 连通性、腾讯兜底是否生效；查看 monitor_console.log 的 last_bar_ts 相关异常。',
        '监控状态/本轮扫描错误': '本轮扫描有错误（含扫描崩溃 outer_err 闭环计数）。查看 logs/monitor_console.log 中的 [warning]/💥 行定位异常标的或崩溃；常见为 tf=None（klines None）或个别标的无 intraday 数据。',
        '监控状态/state.json 今日刷新': 'monitor 今日未运行或未写状态。确认计划任务今日触发；手动启动 monitor。',
        '信号产出/当日信号计数': '交易时段开盘≥30min、行情正常却 0 信号，说明信号链路中断（monitor 僵死/首扫误吞/空转）。立即查 monitor_console.log 最后输出与 last_bar_ts；本例 08-07 因 mootdx 隔夜僵死 + 多次重启首扫抑制导致全天零信号。建议：① 杀 monitor 由 watchdog 重拉；② 加监控 monitor 午盘不重启；③ 部署首扫 last_emitted_bar 游标化修复（见 MEMORY.md 待办）。',
        '端口可达/mootdx TCP 7709': '通达信行情服务器全部不可达。检查网络/防火墙；或网络环境变更需更新 _TDX_SERVERS 列表。',
        '端口可达/飞书 webhook open.feishu.cn': '飞书推送通道中断。检查外网连通性；信号推送和告警都会受影响。',
        '资源使用/CPU 使用率': 'CPU 持续高负载。检查是否有异常进程占用；monitor 单轮扫描不应超过单核。',
        '资源使用/内存使用率': '内存紧张。检查 monitor 进程内存是否持续增长（内存泄漏）；重启 monitor。',
        '资源使用/磁盘 C: 使用率': '磁盘空间不足。清理 logs/ 下的旧日志和 backtest/ 下的旧 CSV。',
        '资源使用/monitor 进程内存': 'monitor 内存偏高（>300MB）。可能是 pandas DataFrame 累积；考虑定时重启。',
        '计划任务/tpoint_monitor': 'monitor 由 watchdog 守护进程（scripts/watchdog.py）保活，非计划任务，属正常设计。若进程也未存活，检查 watchdog 是否在跑（scripts/launch_watchdog.py）。',
        '计划任务/tpoint_alert_engine': 'alert_engine 由 watchdog 守护进程（scripts/watchdog.py）保活，非计划任务，属正常设计。若进程也未存活，检查 watchdog 是否在跑（scripts/launch_watchdog.py）。',
    }
    return suggestions.get(key, '查看对应日志排查；如持续异常请人工介入。')


def _console_summary(all_results, overall, stats):
    """控制台彩色摘要。"""
    now = now_ts()
    print()
    print(_c('bold', f"{'=' * 60}"))
    print(_c('bold', f" tpoint 系统自检  {now.strftime('%Y-%m-%d %H:%M:%S')}"))
    print(_c('bold', f"{'=' * 60}"))
    for r in all_results:
        color = r.color()
        print(f"  {_c(color, r.icon())} [{r.status:4}] {r.category}/{r.name}")
        if r.detail:
            print(f"          {r.detail}")
    print(_c('bold', f"{'-' * 60}"))
    icon = {'PASS': '✅', 'WARN': '⚠️', 'FAIL': '❌'}[overall]
    print(_c(r.color() if overall != 'PASS' else 'bold',
             f"  总体: {icon} {overall}  |  ✅{stats['PASS']}  ⚠️{stats['WARN']}  ❌{stats['FAIL']}  ⏭️{stats['SKIP']}"))
    print()


def _log_anomalies(all_results):
    """异常追加到 anomalies.log。"""
    anomalies = [r for r in all_results if r.status in ('FAIL', 'WARN')]
    if not anomalies:
        return
    now = now_ts()
    with open(ANOMALY_LOG, 'a', encoding='utf-8') as f:
        f.write(f"\n{'=' * 60}\n")
        f.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 自检异常记录\n")
        f.write(f"{'=' * 60}\n")
        for r in anomalies:
            f.write(f"\n[{r.status}] {r.category}/{r.name}\n")
            f.write(f"  详情: {r.detail}\n")
            f.write(f"  值: {r.value}  阈值: {r.threshold}\n")
            f.write(f"  建议: {_suggestion(r)}\n")


def _build_alert_text(all_results, overall, stats, trading_day):
    """构建飞书告警文本。"""
    now = now_ts()
    fails = [r for r in all_results if r.status == 'FAIL']
    warns = [r for r in all_results if r.status == 'WARN']
    lines = [
        f"🔴 tpoint 自检告警 [{overall}]",
        f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}  交易日:{'是' if trading_day else '否'}",
        f"统计: ✅{stats['PASS']} ⚠️{stats['WARN']} ❌{stats['FAIL']} ⏭️{stats['SKIP']}",
    ]
    if fails:
        lines.append(f"\n❌ 失败项 ({len(fails)}):")
        for r in fails[:10]:
            lines.append(f"  • {r.category}/{r.name}: {r.detail[:80]}")
    if warns:
        lines.append(f"\n⚠️ 警告项 ({len(warns)}):")
        for r in warns[:8]:
            lines.append(f"  • {r.category}/{r.name}: {r.detail[:80]}")
    lines.append(f"\n报告: logs/selfcheck/{now.strftime('%Y-%m-%d_%H%M%S')}.md")
    return '\n'.join(lines)


def _get_version():
    """读取 VERSION 文件。"""
    try:
        with open(VERSION_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return '?'

def _build_status_text(all_results, overall, stats, trading_day):
    """构建交易日盘前状态通知（交互卡片，与 monitor B/S 信号卡模板一致）。
    
    颜色标识：
      - green  (PASS 正常) / yellow (WARN 风险) / red (FAIL 异常)
    结构与 emit_card 对齐：header.template + div + hr + note。
    """
    now = now_ts()
    tpl_map = {'PASS': 'green', 'WARN': 'yellow', 'FAIL': 'red'}
    tpl = tpl_map.get(overall, 'blue')
    emoji_map = {'PASS': '✅', 'WARN': '⚠️', 'FAIL': '❌'}
    status_icon = emoji_map.get(overall, '?')
    state_label = {'PASS': '正常', 'WARN': '注意', 'FAIL': '异常'}.get(overall, overall)

    def find(cat, name):
        for r in all_results:
            if r.category == cat and r.name == name:
                return r
        return None

    md = lambda t: {"tag": "lark_md", "content": t}
    elements = []

    # 第1行：运行状态 + 统计概览
    elements.append({"tag": "div", "text": md(
        f"**运行状态：{status_icon} {state_label}**　　　"
        f"时间：{now.strftime('%m-%d %H:%M')}"
    )})
    elements.append({"tag": "div", "text": md(
        f"交易日：{'是' if trading_day else '否'}　　"
        f"✅{stats['PASS']} 项  ⚠️{stats['WARN']} 项  ❌{stats['FAIL']} 项  ⏭️{stats['SKIP']} 项"
    )})

    elements.append({"tag": "hr"})

    # 第2段：关键组件健康检查
    elements.append({"tag": "div", "text": md("**🔧 关键组件健康检查**")})
    comp_specs = [
        ('服务运行', 'monitor 进程'),
        ('服务运行', 'alert_engine 进程'),
        ('服务运行', '单实例锁一致性'),
        ('监控状态', '心跳新鲜度'),
        ('启动状态', '监控标的来源'),
        ('端口可达', 'mootdx TCP 7709'),
        ('端口可达', '飞书 webhook open.feishu.cn'),
        ('资源使用', '磁盘 C: 使用率'),
    ]
    for cat, name in comp_specs:
        r = find(cat, name)
        if r is None:
            continue
        mark = {'PASS': '🟢', 'WARN': '🟡', 'FAIL': '🔴', 'SKIP': '⏭️'}.get(r.status, '⚪')
        elements.append({"tag": "div", "text": md(f"{mark} {name}：{r.detail}")})

    # 第3段：潜在风险（仅 WARN 项）
    warns = [r for r in all_results if r.status == 'WARN']
    if warns:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": md("**⚠️ 潜在风险告警**")})
        for r in warns:
            d = r.detail[:120] if len(r.detail) > 120 else r.detail
            elements.append({"tag": "div", "text": md(f"🟡 {r.category}／{r.name}：{d}")})

    # 第4段：异常项 + 处理建议（仅 FAIL 项）
    fails = [r for r in all_results if r.status == 'FAIL']
    if fails:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": md("**❌ 异常项及处理建议**")})
        for r in fails:
            d = r.detail[:120] if len(r.detail) > 120 else r.detail
            elements.append({"tag": "div", "text": md(f"🔴 {r.category}／{r.name}：{d}")})
            sug = _suggestion(r)
            s = sug[:120] if len(sug) > 120 else sug
            elements.append({"tag": "div", "text": md(f"　↳ 建议：{s}")})

    # 底部备注（灰显 note，与 emit_card footer 一致）
    elements.append({"tag": "hr"})
    ver = _get_version()
    footer = f"完整报告 | logs/selfcheck/{now.strftime('%Y-%m-%d_%H%M%S')}.md  |  v{ver} 盘前自检 · 仅供参考"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": tpl,
                "title": {"tag": "plain_text", "content": f"TPoint 盘前自检 · {status_icon} {state_label}"},
            },
            "elements": elements,
        },
    }


# ========== 主流程 ==========

def main():
    ap = argparse.ArgumentParser(description='tpoint 系统每日自检')
    ap.add_argument('--no-push', action='store_true', help='不推送飞书告警')
    ap.add_argument('--quick', action='store_true', help='快速模式（跳过端口探测）')
    args = ap.parse_args()

    trading_day = is_trading_today()
    print(f"[{now_ts().strftime('%H:%M:%S')}] tpoint 自检启动 | 交易日={'是' if trading_day else '否'} | 模式={'快速' if args.quick else '完整'}")

    all_results = []
    all_results.extend(check_startup())
    all_results.extend(check_services(trading_day))
    all_results.extend(check_monitoring(trading_day))
    all_results.extend(check_signal_output(trading_day))
    all_results.extend(check_ports(quick=args.quick))
    all_results.extend(check_resources())
    all_results.extend(check_scheduled_tasks())

    # 生成报告
    report_md, overall, stats = build_report(all_results, trading_day, args.quick)
    now = now_ts()
    report_path = os.path.join(SELFCHK_DIR, now.strftime('%Y-%m-%d_%H%M%S') + '.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_md)

    # 控制台摘要
    _console_summary(all_results, overall, stats)

    # 异常日志
    _log_anomalies(all_results)

    # 飞书推送：交易日盘前状态通知（必发） + 非交易日仅异常告警
    if trading_day:
        # 交易日盘前状态通知：交互卡片（必发），开盘前送达
        card = _build_status_text(all_results, overall, stats, trading_day)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        if not args.no_push:
            resp = _push_feishu(card)
            print(f"  📡 盘前状态通知已推送: {resp[:60] if resp else 'N/A'}")
        else:
            print(f"  ⏭️  --no-push 模式，跳过推送（卡片预览见上方）")
    else:
        # 非交易日：维持原行为，仅异常时推送告警，正常不扰民
        if overall in ('FAIL', 'WARN') and not args.no_push:
            alert_text = _build_alert_text(all_results, overall, stats, trading_day)
            resp = _push_feishu(alert_text)
            print(f"  📡 飞书告警已推送: {resp[:60] if resp else 'N/A'}")
        elif overall == 'PASS':
            print(f"  ✅ 自检通过，非交易日不推送")
        else:
            print(f"  ⏭️  --no-push 模式，跳过推送")

    print(f"  📄 报告: {report_path}")
    print(f"  📋 异常日志: {ANOMALY_LOG}")

    # 退出码：FAIL→1，WARN→0（不阻断计划任务），PASS→0
    return 1 if overall == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
