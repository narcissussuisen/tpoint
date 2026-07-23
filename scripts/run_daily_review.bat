@echo off
REM tpoint daily signal review task entry
REM called by Windows scheduled task tpoint_daily_review at 15:30 on trading days
REM 以 push_audit.jsonl 实盘推送为权威源，按标的归类，对比近5交易日实盘基线，推送飞书复盘群
chcp 65001 >nul
set "ROOT=C:\Users\YZP\WorkBuddy\Claw\tpoint"
set "PYTHONIOENCODING=utf-8"
"%ROOT%\venv\Scripts\python.exe" "%ROOT%\scripts\signal_review_daily.py" --push >> "%ROOT%\logs\daily_review.log" 2>&1
