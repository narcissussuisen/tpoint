@echo off
REM ============================================================
REM  v9 monitor 自启安装器（无需管理员）
REM  通过 HKCU\Run 注册 watchdog，当前用户登录后自动拉起 monitor + alert_engine。
REM  watchdog 用托管 python(pythonw, 无窗口) + 单实例守护，杜绝双进程与 cmd 弹窗。
REM  注意：自启为"登录后"生效（非 SYSTEM 计划任务，不需要管理员）。
REM        若需"不登录也运行"(服务器/无头场景)，请用管理员执行 scripts\install_tasks_system.bat。
REM ============================================================
set "SCRIPTS=%~dp0"
set "PYW=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
set "LAUNCH=%SCRIPTS%launch_watchdog.py"

REM 写入 HKCU\Run（当前用户登录自启，免管理员）
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "tpoint_watchdog" /t REG_SZ /d "\"%PYW%\" \"%LAUNCH%\"" /f

echo.
echo tpoint watchdog 已注册为登录自启（HKCU\Run\tpoint_watchdog）。
echo 下次登录后 watchdog 会自动拉起 monitor + alert_engine（无窗口）。
echo 立即生效（无需重登）：手动运行下面任一行
echo   "%PYW%" "%LAUNCH%"
echo.
echo 如需移除自启：
echo   reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "tpoint_watchdog" /f
echo.
pause
