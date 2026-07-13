@echo off
REM restart.bat - one-click restart v9 monitor and alert engine
REM Run as Administrator
REM 1) Stops scheduled tasks (if running)
REM 2) Force-kills any leftover python processes
REM 3) Starts fresh SYSTEM scheduled tasks

cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

echo [1/4] Stop scheduled tasks...
schtasks /end /tn tpoint_monitor 2>nul
schtasks /end /tn tpoint_alert_engine 2>nul

echo [2/4] Kill leftover python processes...
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM pythonw.exe /T 2>nul

echo [3/4] Wait 3 seconds...
timeout /t 3 /nobreak >nul

echo [4/4] Start scheduled tasks...
schtasks /run /tn tpoint_monitor
schtasks /run /tn tpoint_alert_engine

echo.
echo Done. Verify with:
echo   schtasks /query /tn tpoint_monitor
echo   schtasks /query /tn tpoint_alert_engine
echo   PowerShell: Get-Process python ^| Select-Object Id, SessionId, StartTime
pause
