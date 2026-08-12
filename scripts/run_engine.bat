@echo off
:: tpoint alert_engine launcher
:: weekend skip handled by Python alert_engine.py internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set MACD_GATE_MODE=floor
set TP_LAUNCHED_BY_V9LAUNCH=1
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe

if not exist logs mkdir logs

:: 2026-07-22 自愈：启动前先清理其它 alert_engine 实例与残留锁，避免多 run_engine
:: 循环竞争导致死锁/崩溃循环。仅按命令行匹配 alert_engine.py，不影响 monitor。
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*alert_engine.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>nul
del /Q "C:\Users\YZP\WorkBuddy\Claw\tpoint\data\.alert_engine.lock" 2>nul
del /Q "C:\Users\YZP\WorkBuddy\Claw\tpoint\data\.alert_engine.pid" 2>nul

:loop
:: 2026-07-31 护栏：若 watchdog 已拉起 alert_engine（data\.alert_engine.pid 指向存活进程），
:: 本 Session0 启动器让出，避免双 alert_engine 双跑（watchdog v3 已单一负责拉起 alert_engine）。
if exist "data\.alert_engine.pid" (
  for /f %%p in (data\.alert_engine.pid) do (
    tasklist /fi "pid eq %%p" 2>nul | find "%%p" >nul && (echo [%date% %time%] alert_engine %%p already running (watchdog), run_engine yields >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log" & goto :eof)
  )
)
"%PY_EXE%" core\alert_engine.py
echo [%date% %time%] tpoint_alert_engine exited, restart in 5s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
timeout /t 5 /nobreak >nul
goto loop
