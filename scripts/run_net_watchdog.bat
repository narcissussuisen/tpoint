@echo off
REM net_health_watchdog 运行器：每 5 分钟由 Windows 计划任务调用
setlocal
set PY=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe
set BASE=C:\Users\YZP\WorkBuddy\Claw\tpoint
"%PY%" "%BASE%\scripts\net_health_watchdog.py"
exit /b %errorlevel%
