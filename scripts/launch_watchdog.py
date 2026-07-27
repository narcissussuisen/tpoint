import subprocess, os
BASE = r'C:\Users\YZP\WorkBuddy\Claw\tpoint'
# 用托管 python（不自我复制）；venv 的 241KB python.exe 在 Windows 上启动会自复制出双进程。
PY = r'C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe'
SP = os.path.join(BASE, 'venv', 'Lib', 'site-packages')
env = os.environ.copy()
env['PYTHONIOENCODING'] = 'utf-8'
env['PYTHONUNBUFFERED'] = '1'
env['PYTHONPATH'] = f'{SP};{os.path.join(BASE, "venv", "Lib")};{BASE}'
fl = open(os.path.join(BASE, 'logs', 'watchdog.log'), 'a', encoding='utf-8')
p = subprocess.Popen(
    [PY, os.path.join(BASE, 'scripts', 'watchdog.py')],
    creationflags=0x8 | 0x200,
    cwd=BASE, env=env,
    stdin=subprocess.DEVNULL, stdout=fl, stderr=fl, close_fds=True,
)
fl.close()
print('watchdog launched PID=', p.pid)
