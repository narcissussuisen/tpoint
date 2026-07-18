@echo off
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"
:: 周末不启动 alert_engine，避免非交易日空转累积进程
powershell -NoProfile -Command "if ((Get-Date).DayOfWeek -in 'Saturday','Sunday') { exit 1 }"
if %errorlevel% equ 1 (
    echo [%date% %time%] 周末，alert_engine 不启动 >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
    exit /b 0
)

:: 使用 venv 真实 Python 解释器（避免 Scripts\python.exe 启动器退出被误判为 alert_engine 退出导致 :loop 累积实例）
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe

if not exist logs mkdir logs
:loop
"%PY_EXE%" core\alert_engine.py
echo [%date% %time%] tpoint_alert_engine exited, restart in 5s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
timeout /t 5 /nobreak >nul
goto loop
