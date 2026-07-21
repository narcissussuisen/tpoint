@echo off
:: tpoint alert_engine launcher
:: weekend skip handled by Python alert_engine.py internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set MACD_GATE_MODE=floor
set PY_EXE=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe

if not exist logs mkdir logs
:loop
"%PY_EXE%" core\alert_engine.py
echo [%date% %time%] tpoint_alert_engine exited, restart in 5s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
timeout /t 5 /nobreak >nul
goto loop
