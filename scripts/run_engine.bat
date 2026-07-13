@echo off
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"
:loop
"C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe" core\alert_engine.py
echo [%date% %time%] tpoint_alert_engine exited, restart in 5s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\engine_crash.log"
timeout /t 5 /nobreak >nul
goto loop
