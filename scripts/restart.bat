@echo off
REM restart.bat - one-click restart v9 monitor + alert engine (precise, no full-python kill)
REM Run as Administrator
REM Fix1.3 (2026-07-22): 改为按 PID 精确杀，读 data/.monitor.pid / data/.alert_engine.pid，
REM 不再 taskkill /F /IM python.exe 误杀全部 python（此前会误伤/产生重复实例共用 webhook → 11232）。

cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

echo [1/4] Stop scheduled tasks (if any)...
schtasks /end /tn tpoint_monitor 2>nul
schtasks /end /tn tpoint_alert_engine 2>nul

echo [2/4] Kill monitor/alert_engine by PID (precise)...
setlocal enabledelayedexpansion
for %%f in (.monitor.svc.pid .alert_engine.pid) do (
  if exist "data\%%f" (
    set /p pid=<"data\%%f"
    if defined pid (
      echo   kill %%~nf pid=!pid!
      taskkill /PID !pid! /F 2>nul
    ) else (
      echo   %%~nf empty, skip
    )
  ) else (
    echo   data\%%f missing, skip
  )
)
endlocal

echo [3/4] Wait 3 seconds...
timeout /t 3 /nobreak >nul

echo [4/4] Relaunch monitor + alert engine (with V9Launch marker)...
set TP_LAUNCHED_BY_V9LAUNCH=1
start "v9_monitor" /MIN cmd /c "C:\Users\YZP\WorkBuddy\Claw\tpoint\scripts\run_monitor.bat"
timeout /t 3 /nobreak >nul
start "v9_engine"  /MIN cmd /c "C:\Users\YZP\WorkBuddy\Claw\tpoint\scripts\run_engine.bat"

echo.
echo Done. Verify with:
echo   Get-Process python ^| Select-Object Id, SessionId, StartTime
echo   type data\.monitor.svc.pid
pause
