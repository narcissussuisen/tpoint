@echo off
REM tpoint 每日收盘后信号复盘 —— 定时任务入口（完整流水线：复算 + 行情图标注 + HTML + 推飞书）
REM 由 Windows 计划任务 tpoint_daily_review 于每个交易日 15:30 调用
REM 2026-07-30 修正：原仅 daily_signal_review.py --push（无行情图、推文本摘要），不满足"复盘必须画图标注信号"。
REM   现改为 3 步流水线：复算JSON -> review_charts 画信号标注图 -> build_review_html 汇编含图HTML -> push_feishu_html 推飞书云链接。
REM 编码修复：PYTHONUTF8=1 + chcp 65001 避免 monitor.py emoji 打印在 gbk 代码页下 UnicodeEncodeError。
REM --- 2026-09-03 T0/T1 失败语义透传（自迭代闭环硬化方案 v2，docs/self_iteration_loop_hardening_plan.md）---
REM   T0: 流水线头部 runtime_identity --begin 锚定 run_id（git commit/配置hash/成交口径版本 落盘 data/runtime_identity/）。
REM   T1: 每步前 pipeline_status running <step> 预写 RUNNING；每步后 record <step> <rc>（先 set RC=%ERRORLEVEL% 捕获真实 rc）；
REM       record 支持 --expected 分号分隔产物路径（rc=0 但产物缺失=DEGRADED；rc=77 约定=SKIPPED）；
REM       尾部 summarize --push-fail：关键步(live_review/reconcile/daily_report/daily_iterate/closed_loop/auto_tune)
REM       任一 FAILED/INTERRUPTED/NOT_RUN → 推 b4eba7a9 全局群 + exit /b 2（计划任务显示失败）。
chcp 65001 >nul
set "ROOT=C:\Users\YZP\WorkBuddy\Claw\tpoint"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONPATH=%ROOT%\venv\Lib\site-packages;%ROOT%\venv\Lib;%ROOT%
set PY_EXE=C:\Users\YZP\.workbuddy\binaries\python\versions\3.13.12\python.exe
set PUSH_PY=C:\Users\YZP\WorkBuddy\Claw\方法论与研究文档\研究报告\push_feishu_html.py
set WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/849577f5-6c79-498e-92bd-0721af6f9622
for /f "usebackq" %%i in (`%PY_EXE% %ROOT%\scripts\_today.py`) do set D=%%i
echo [%DATE% %TIME%] === tpoint daily review %D% === >> "%ROOT%\logs\daily_review.log"
REM --- T0 运行身份锚定（2026-09-03）：失败不阻断流水线（T1 record 会自动兜底重建 run） ---
"%PY_EXE%" "%ROOT%\scripts\runtime_identity.py" --begin >> "%ROOT%\logs\daily_review.log" 2>&1
if errorlevel 1 echo [%DATE% %TIME%] [WARN] runtime_identity non-zero >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-11 闭环迭代：前置自愈守卫（缺失关键脚本自动从 tpoint 外备份恢复；恢复失败推全局群；绝不阻断后续步骤） ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running preflight >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\pipeline_preflight.py" >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record preflight %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] preflight non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚 实盘化重构：live_roundtrip_review 产出实盘配对/波动段分析JSON（报告唯一信号源=实盘推送） ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running recompute >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\daily_signal_review.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record recompute %RC% --expected "%ROOT%\output\review_%D%.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] recompute non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running live_review >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\live_roundtrip_review.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record live_review %RC% --expected "%ROOT%\output\live_review_%D%.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] live_review non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running charts >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\review_charts.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record charts %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] charts non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running build_html >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\build_review_html.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record build_html %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] build_html non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
REM --- 推送：复盘报告仅推 a35d7f52 自迭代群 + 动态标题（08-04晚用户指定：不再推849577f5，标题带当日指标） ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running push_review >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\push_tpoint_review.py" %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record push_review %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] push_review non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (review html pushed) === >> "%ROOT%\logs\daily_review.log"
REM --- R0 自迭代基建（2026-08-03 新增）：步骤5 F盘增量 + 步骤6 生产vs回测对账 ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running fdisk_update >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\fdisk_daily_update.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record fdisk_update %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] fdisk_update non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running reconcile >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\prod_vs_bt_reconcile.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record reconcile %RC% --expected "%ROOT%\output\reconcile_%D%.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] reconcile non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (fdisk+reconcile) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚：对账转后台（用户拍板）——reconcile JSON 照跑留存档，不再生成/推送对账HTML；异常由 daily_report_push.py 文本告警 ---
REM --- 2026-08-04 新增：步骤8 自迭代日报（阶段2/3脚本化：roll20聚合+状态更新+推a35d7f52指定群+全局群摘要） ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running daily_report >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\daily_report_push.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record daily_report %RC% >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] daily_report_push non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (daily report pushed) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-04 晚新增：步骤9 每日自迭代（用户指令：寻优→回测验证→护栏热更→git小版本记录→摘要推a35d7f52） ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running daily_iterate >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\daily_iterate.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record daily_iterate %RC% --expected "%ROOT%\data\iteration_state.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] daily_iterate non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (daily iterate) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-05 new: step10 closed loop ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running closed_loop >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\daily_closed_loop.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record closed_loop %RC% --expected "%ROOT%\data\closed_loop_state.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] closed_loop non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (closed loop) === >> "%ROOT%\logs\daily_review.log"
REM --- 2026-08-11 Request4 补全：step11 报告驱动自动调参（每日闭环「实际出手」环节）---
REM 把 factor_opt 寻优报告转化为对 monitor_config.json 的真实改写；护栏 total_ret优先+wr不降+拒绝 wr 虚胖；
REM 仅改写监控内标的（不自动新增非监控项以免 live monitor 缺字段崩溃）；改动记 data/auto_tune_state.json 可回滚；
REM 推 a35d7f52。与 daily_iterate(仅白名单 atr_min_pct) 互补：本步落 trail 等锁定参数的自动寻优。
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" running auto_tune >> "%ROOT%\logs\daily_review.log" 2>&1
"%PY_EXE%" "%ROOT%\scripts\auto_tune.py" --date %D% >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" record auto_tune %RC% --expected "%ROOT%\data\auto_tune_state.json" >> "%ROOT%\logs\daily_review.log" 2>&1
if %RC% GEQ 1 echo [%DATE% %TIME%] [WARN] auto_tune non-zero rc=%RC% >> "%ROOT%\logs\daily_review.log"
echo [%DATE% %TIME%] === done (auto tune) === >> "%ROOT%\logs\daily_review.log"
REM --- T1 尾部汇总（2026-09-03）：只读当前 run_id 防旧状态污染；关键步失败 → 飞书告警 + exit /b 2 ---
"%PY_EXE%" "%ROOT%\scripts\pipeline_status.py" summarize --push-fail >> "%ROOT%\logs\daily_review.log" 2>&1
set RC=%ERRORLEVEL%
if %RC% GEQ 2 (
  echo [%DATE% %TIME%] [FAIL] pipeline summary exit %RC% - see data/step_status/ >> "%ROOT%\logs\daily_review.log"
  exit /b %RC%
)
echo [%DATE% %TIME%] === pipeline summary done - all steps recorded === >> "%ROOT%\logs\daily_review.log"
