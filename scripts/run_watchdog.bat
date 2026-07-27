@echo off
:: tpoint watchdog launcher (called by scheduled task tpoint_watchdog at logon)
:: 拉起 scripts/watchdog.py（独立守护进程，负责保活 monitor + alert_engine）
:: 用托管 python（不自我复制）；venv 的 241KB python.exe 在 Windows 上启动会自复制出双进程。
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist logs mkdir logs
"%PY_EXE%" scripts\launch_watchdog.py
