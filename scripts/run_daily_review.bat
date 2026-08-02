@echo off
REM tpoint 每日收盘后信号复盘 —— 定时任务入口（完整流水线：复算 + 行情图标注 + HTML + 推飞书）
REM 由 Windows 计划任务 tpoint_daily_review 于每个交易日 15:30 调用
REM 2026-07-30 修正：原仅 daily_signal_review.py --push（无行情图、推文本摘要），不满足"复盘必须画图标注信号"。
REM   现改为 3 步流水线：复算JSON -> review_charts 画信号标注图 -> build_review_html 汇编含图HTML -> push_feishu_html 推飞书云链接。
REM 编码修复：PYTHONUTF8=1 + chcp 65001 避免 monitor.py emoji 打印在 gbk 代码页下 UnicodeEncodeError。
chcp 65001 >nul
set "ROOT=C:\Users\YZP\WorkBuddy\Claw\tpoint"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=%ROOT%\venv\Lib\site-packages;%ROOT%\venv\Lib;%ROOT%
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe
set PUSH_PY=C:\Users\YZP\WorkBuddy\Claw\research\push_feishu_html.py
set WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622
for /f "usebackq" %%i in (`"%PY_EXE%" -c "import datetime;print(datetime.date.today().strftime('%%Y-%%m-%%d'))"`) do set D=%%i
echo [%DATE% %TIME%] === tpoint daily review %D% === >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\daily_signal_review.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] recompute non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\review_charts.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] charts non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\build_review_html.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] build_html non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%PUSH_PY%" "%ROOT%\output\review_%D%.html" "%WEBHOOK%" "tpoint 每日复盘 %D%" >> "%ROOT%\logs\daily_review.log" 2>&1
echo [%DATE% %TIME%] === done (html+charts pushed) === >> "%ROOT%\logs\daily_review.log"
