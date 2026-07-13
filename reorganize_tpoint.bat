@echo off
REM reorganize_tpoint.bat - one-click restructure tpoint folder
REM Run as Administrator

cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

echo [1/4] Stop scheduled tasks...
schtasks /end /tn v9_monitor 2>nul
schtasks /end /tn v9_alert_engine 2>nul

echo [2/4] Kill leftover python processes...
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM pythonw.exe /T 2>nul

echo [3/4] Reorganize folder structure...
"venv\Scripts\python.exe" "scripts\reorganize_tpoint.py"
if errorlevel 1 (
    echo ERROR: reorganization failed.
    pause
    exit /b 1
)

echo [4/4] Re-register and start scheduled tasks...
schtasks /delete /tn v9_monitor /f 2>nul
schtasks /delete /tn v9_alert_engine /f 2>nul
"scripts\install_tasks.bat"
"scripts\restart_v9.bat"

echo.
echo Done.
pause
