@echo off
:: tpoint monitor launcher (called by scheduled task / V9Launch.bat)
:: weekend skip handled by Python monitor.py internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set MACD_GATE_MODE=floor
set TP_MHD_THRESHOLD=0.15
set TP_LAUNCHED_BY_V9LAUNCH=1
set TP_LAUNCHER=run_monitor
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe

if not exist logs mkdir logs
:loop
REM 若 watchdog 已托管 monitor（.watchdog.pid 存活），本脚本不作为第二启动器抢锁，直接退出避免双 monitor
if exist "data\.watchdog.pid" (
  set /p WD=<"data\.watchdog.pid"
  tasklist /fi "pid eq !WD!" 2>nul | find "!WD!" >nul && (echo [%date% %time%] run_monitor: watchdog(!WD!) alive, defer to watchdog >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log" & goto :eof)
)
"%PY_EXE%" core\monitor.py >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console.log" 2>&1
echo [%date% %time%] tpoint_monitor exited, restart in 30s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log"
timeout /t 30 /nobreak >nul
goto loop
