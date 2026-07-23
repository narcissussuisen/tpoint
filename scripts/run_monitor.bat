@echo off
:: tpoint monitor launcher (called by scheduled task / V9Launch.bat)
:: weekend skip handled by Python monitor.py internally (no PS needed here)
cd /d "C:\Users\YZP\WorkBuddy\Claw\tpoint"

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib\site-packages;C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Lib;C:\Users\YZP\WorkBuddy\Claw\tpoint
set MACD_GATE_MODE=floor
set TP_LAUNCHED_BY_V9LAUNCH=1
set PY_EXE=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe

if not exist logs mkdir logs
:loop
:: 改进 A (07-23)：拉起前先读 data/.monitor.pid，确认对应进程仍存活（且确为 monitor.py）则
:: 直接 sleep 跳过本次拉起，消除"每 ~30s 重 import 重模块抢锁失败"的空转 churn。
:: 进程已死 / pid 文件缺失 / 读取或校验失败 → 一律放行，由 monitor.py _clear_stale_lock 自愈。
:: 注：用 Get-CimInstance 校验 CommandLine 含 monitor.py，避免 PID 复用导致误判"存活"而真空。
set "MON_ALIVE=0"
set "MON_PID="
if exist "data\.monitor.pid" (
  set /p MON_PID=<"data\.monitor.pid"
  if defined MON_PID (
    powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%MON_PID%' -ErrorAction SilentlyContinue; if ($p -and $p.CommandLine -like '*monitor.py*') { exit 0 } else { exit 1 }" >nul 2>&1
    if not errorlevel 1 set "MON_ALIVE=1"
  )
)
if "%MON_ALIVE%"=="1" (
  timeout /t 30 /nobreak >nul
  goto loop
)

"%PY_EXE%" core\monitor.py >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_console.log" 2>&1
echo [%date% %time%] tpoint_monitor exited, restart in 30s >> "C:\Users\YZP\WorkBuddy\Claw\tpoint\logs\monitor_crash.log"
:: 崩溃即通知（对齐全局规则：任务异常须推送飞书）。后台执行，不阻塞重启循环。
start "" /B "%PY_EXE%" "C:\Users\YZP\.workbuddy\notify.py" "[tpoint] monitor 进程崩溃退出，30s 后自动重启 (pid=%MON_PID%)" >nul 2>&1
timeout /t 30 /nobreak >nul
goto loop
