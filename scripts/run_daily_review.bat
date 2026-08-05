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
set PUSH_PY=F:\Users\YZP\WorkBuddy\Claw\research\push_feishu_html.py
set WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622
for /f "usebackq" %%i in (`%PY_EXE% %ROOT%\scripts\_today.py`) do set D=%%i
echo [%DATE% %TIME%] === tpoint daily review %D% === >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\daily_signal_review.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] recompute non-zero >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚 实盘化重构：live_roundtrip_review 产出实盘配对/波动段分析JSON（报告唯一信号源=实盘推送） ---
"%PY_EXE%" "%ROOT%\scripts\live_roundtrip_review.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] live_review non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\review_charts.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] charts non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\build_review_html.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] build_html non-zero >> "%ROOT%\logs\daily_review.log"
REM --- 推送：复盘报告仅推 a35d7f52 自迭代群 + 动态标题（08-04晚用户指定：不再推849577f5，标题带当日指标） ---
"%PY_EXE%" "%ROOT%\scripts\push_tpoint_review.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] push_review non-zero >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (review html pushed) === >> "%ROOT%\logs\daily_review.log"
REM --- R0 自迭代基建（2026-08-03 新增）：步骤5 F盘增量 + 步骤6 生产vs回测对账 ---
"%PY_EXE%" "%ROOT%\scripts\fdisk_daily_update.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] fdisk_update non-zero >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\prod_vs_bt_reconcile.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] reconcile non-zero >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (fdisk+reconcile) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚：对账转后台（用户拍板）——reconcile JSON 照跑留存档，不再生成/推送对账HTML；异常由 daily_report_push.py 文本告警 ---
REM --- 2026-08-04 新增：步骤8 自迭代日报（阶段2/3脚本化：roll20聚合+状态更新+推a35d7f52指定群+全局群摘要） ---
"%PY_EXE%" "%ROOT%\scripts\daily_report_push.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] daily_report_push non-zero >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (daily report pushed) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚新增：步骤9 每日自迭代（用户指令：寻优→回测验证→护栏热更→git小版本记录→摘要推a35d7f52） ---
"%PY_EXE%" "%ROOT%\scripts\daily_iterate.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] daily_iterate non-zero >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (daily iterate) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-05 new: step10 closed loop ---
"%PY_EXE%" "%ROOT%\scripts\daily_closed_loop.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] closed_loop non-zero >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (closed loop) === >> "%ROOT%\logs\daily_review.log"
