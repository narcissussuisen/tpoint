@echo off
:: tpoint alert_engine launcher
:: weekend skip handled by Python alert_engine.py internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set MACD_GATE_MODE=floor
set PY_EXE=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe

if not exist logs mkdir logs

:: 2026-07-22 自愈：启动前先清理其它 alert_engine 实例与残留锁，避免多 run_engine
:: 循环竞争导致死锁/崩溃循环。仅按命令行匹配 alert_engine.py，不影响 monitor。
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*alert_engine.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>nul
del /Q "C:\Users\YZP\WorkBuddy\Claw\tpoint\data\.alert_engine.lock" 2>nul
del /Q "C:\Users\YZP\WorkBuddy\Claw\tpoint\data\.alert_engine.pid" 2>nul

:loop
"%PY_EXE%" core\alert_engine.py
echo [%date% %time%] tpoint_alert_engine exited, restart in 5s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
timeout /t 5 /nobreak >nul
goto loop
