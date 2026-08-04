@echo off
:: tpoint daily selfcheck launcher (called by scheduled task tpoint_selfcheck)
:: trading-day 09:00 trigger -> runs scripts/selfcheck_daily.py
:: weekend skip handled by Python script internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe

if not exist logs\selfcheck mkdir logs\selfcheck

echo [%date% %time%] selfcheck start >> "logs\selfcheck\selfcheck_lifecycle.log"

"%PY_EXE%" scripts\selfcheck_daily.py >> "logs\selfcheck\selfcheck_console.log" 2>&1

echo [%date% %time%] selfcheck done (exit=%errorlevel%) >> "logs\selfcheck\selfcheck_lifecycle.log"
