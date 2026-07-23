@echo off
REM tpoint 每日收盘后信号复盘 —— 定时任务入口
REM 由 Windows 计划任务 tpoint_daily_review 于每个交易日 15:30 调用
set "ROOT=C:\Users\YZP\WorkBuddy\Claw\tpoint"
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\scripts\daily_signal_review.py" --push >> "%ROOT%\logs\daily_review.log" 2>&1
