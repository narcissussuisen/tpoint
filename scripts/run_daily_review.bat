@echo off
REM tpoint daily signal review task entry
REM called by Windows scheduled task tpoint_daily_review at 15:30 on trading days
chcp 65001 >nul
set "ROOT=C:\Users\YZP\WorkBuddy\Claw\tpoint"
set "PYTHONIOENCODING=utf-8"
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\scripts\daily_signal_review.py" --push >> "%ROOT%\logs\daily_review.log" 2>&1
