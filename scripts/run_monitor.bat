@echo off
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if not exist logs mkdir logs
:loop
"C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe" core\monitor_v9.py >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console.log" 2>&1
echo [%date% %time%] monitor_v9 exited, restart in 30s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log"
timeout /t 30 /nobreak >nul
goto loop
