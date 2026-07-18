@echo off
:: 周末不启动 monitor，避免非交易日空转累积进程
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"
powershell -NoProfile -Command "if ((Get-Date).DayOfWeek -in 'Saturday','Sunday') { exit 1 }"
if %errorlevel% equ 1 (
    echo [%date% %time%] 周末，monitor 不启动 >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log"
    exit /b 0
)

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
:: 使用 venv 真实 Python 解释器（避免 Scripts\python.exe 启动器退出被误判为 monitor 退出）
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe

if not exist logs mkdir logs
:loop
"%PY_EXE%" core\monitor.py >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console.log" 2>&1
echo [%date% %time%] tpoint_monitor exited, restart in 30s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log"
timeout /t 30 /nobreak >nul
goto loop
