@echo off
REM ============================================================
REM  v9 monitor persistent scheduled tasks installer
REM  Run as Administrator
REM  Registers two startup tasks: v9_monitor / v9_alert_engine
REM  Both run as SYSTEM (no login, no password needed),
REM  inner .bat loops auto-restart the process after 5s if it crashes.
REM ============================================================
set "TASKS_DIR=%~dp0"

schtasks /create /tn "v9_monitor" ^
  /tr "\"%TASKS_DIR%run_monitor.bat\"" ^
  /sc onstart /ru SYSTEM /rl highest /f

schtasks /create /tn "v9_alert_engine" ^
  /tr "\"%TASKS_DIR%run_engine.bat\"" ^
  /sc onstart /ru SYSTEM /rl highest /f

echo.
echo Tasks registered. Verify with:
echo   schtasks /query /tn v9_monitor
echo   schtasks /query /tn v9_alert_engine
echo.
echo To remove:
echo   schtasks /delete /tn v9_monitor /f
echo   schtasks /delete /tn v9_alert_engine /f
echo.
pause
