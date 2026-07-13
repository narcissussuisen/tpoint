@echo off
REM ============================================================
REM  v9 monitor persistent scheduled tasks installer
REM  Run as Administrator
REM  Registers two startup tasks: tpoint_monitor / tpoint_alert_engine
REM  Both run as SYSTEM (no login, no password needed),
REM  inner .bat loops auto-restart the process after 5s if it crashes.
REM ============================================================
set "TASKS_DIR=%~dp0"

schtasks /create /tn "tpoint_monitor" ^
  /tr "\"%TASKS_DIR%run_monitor.bat\"" ^
  /sc onstart /ru SYSTEM /rl highest /f

schtasks /create /tn "tpoint_alert_engine" ^
  /tr "\"%TASKS_DIR%run_engine.bat\"" ^
  /sc onstart /ru SYSTEM /rl highest /f

echo.
echo Tasks registered. Verify with:
echo   schtasks /query /tn tpoint_monitor
echo   schtasks /query /tn tpoint_alert_engine
echo.
echo To remove:
echo   schtasks /delete /tn tpoint_monitor /f
echo   schtasks /delete /tn tpoint_alert_engine /f
echo.
pause
