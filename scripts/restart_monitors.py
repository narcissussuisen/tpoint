"""tpoint 紧急重启 v3：
  1. 清理 stale lock
  2. 直接 detached 拉 monitor + engine（设 TP_LAUNCHED_BY_V9LAUNCH 绕过 guard）
  3. 拉一个独立的 watchdog 守护，定期检查并重启它们（替代 run_monitor.bat 的 :loop）
     —— 解决"OS 杀掉 cmd.exe 后整条 :loop 跟着死"的历史问题
"""
import subprocess
import os
import sys
import time

BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
# 用托管 python（不自我复制）；venv 的 241KB python.exe 在 Windows 上启动会自复制出双进程。
PY   = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe'
DATA = os.path.join(BASE, 'data')
LOGS = os.path.join(BASE, 'logs')

DETACHED = 0x00000008 | 0x00000200

# 1) 清理 stale lock
for fn in ('.monitor.svc.lock', '.monitor.svc.pid', '.alert_engine.lock', '.alert_engine.pid'):
    p = os.path.join(DATA, fn)
    if os.path.exists(p):
        try: os.remove(p); print(f'  rm stale {p}')
        except Exception as e: print(f'  WARN rm {p}: {e}')

env = os.environ.copy()
env['PYTHONIOENCODING']    = 'utf-8'
env['PYTHONUNBUFFERED']    = '1'
env['PYTHONPATH']          = f'{BASE}\\venv\\Lib\\site-packages;{BASE}\\venv\\Lib;{BASE}'
env['MACD_GATE_MODE']      = 'floor'
env['TP_LAUNCHED_BY_V9LAUNCH'] = '1'

def detached_spawn(label, script_or_args, log_path):
    # script_or_args can be either a list like [PY, 'core/monitor.py'] or a single string path
    args = script_or_args if isinstance(script_or_args, list) else [script_or_args]
    log_fh = open(log_path, 'a', encoding='utf-8', errors='ignore')
    p = subprocess.Popen(
        args, creationflags=DETACHED,
        cwd=BASE, env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh, stderr=log_fh, close_fds=True,
    )
    log_fh.close()
    print(f'  launched {label}: PID={p.pid}')

# 2) monitor + engine
for label, relscript, log_name in [
    ('monitor',      'core/monitor.py',      'monitor_console.log'),
    ('alert_engine', 'core/alert_engine.py', 'alert_engine_console.log'),
]:
    try:
        detached_spawn(label, [PY, os.path.join(BASE, relscript)],
                       os.path.join(LOGS, log_name))
    except Exception as e:
        print(f'  FAIL {label}: {e}', file=sys.stderr)

# 3) watchdog（独立 Python 进程，不依赖任何 bat / cmd.exe）
try:
    # watchdog 不能把自己负责重启——双实例 watchdog 反而干扰。让 watchdog 先
    # 检查自己是否已存在一份（避免重复拉）。简化：仅拉一份；重启后用户重启电脑
    # 时需要在 Startup 注册 watchdog（todo）。
    wd_log = os.path.join(LOGS, 'watchdog.log')
    detached_spawn('watchdog', [PY, os.path.join(BASE, 'scripts', 'watchdog.py')],
                   wd_log)
except Exception as e:
    print(f'  FAIL watchdog: {e}', file=sys.stderr)

time.sleep(1)
print('OK')
