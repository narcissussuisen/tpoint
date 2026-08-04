# ============================================================
#  tpoint 每日复盘计划任务注册脚本
#  注册 tpoint_daily_review 任务：每周一至五 15:30 执行收盘后复盘
#  并推送复盘报告到飞书复盘群（849577f5）
#
#  使用方式（普通用户即可，无需管理员）:
#    PowerShell -ExecutionPolicy Bypass -File scripts\install_daily_review.ps1
#
#  说明：
#   - 复盘脚本 scripts/daily_signal_review.py 内部已做非交易日静默退出
#     （weekday>=5 或 HOLIDAYS 直接 return），节假日不会空跑。
#   - 入口 scripts/run_daily_review.bat 已调用 daily_signal_review.py --push
#     并输出到 logs\daily_review.log。
# ============================================================

$ErrorActionPreference = "Continue"
$BASE = "C:\Users\YZP\WorkBuddy\Claw\tpoint"
$BAT = "$BASE\scripts\run_daily_review.bat"
$USER = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "============================================="
Write-Host " tpoint 每日复盘计划任务注册"
Write-Host " $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host " 用户: $USER"
Write-Host "============================================="
Write-Host ""

# ---------- 注册 tpoint_daily_review (周一至五 15:30) ----------
Write-Host "[1] 注册 tpoint_daily_review (每周一至五 15:30)..."

if (-not (Test-Path $BAT)) {
    Write-Host "  FAIL: 入口脚本不存在: $BAT"
} else {
    $action = New-ScheduledTaskAction -Execute $BAT -WorkingDirectory $BASE
    # 每周一至五 15:30（收盘后约 30 分钟，确保当日 1m 行情完整落地）
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 3:30pm
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2)
    # 以当前用户运行（Interactive，需用户已登录；15:30 交易日通常已登录）
    # 与 tpoint_selfcheck 一致，确保能读取用户环境并推送飞书。
    $principal = New-ScheduledTaskPrincipal -UserId $USER -LogonType Interactive -RunLevel Limited

    try {
        Register-ScheduledTask -TaskName "tpoint_daily_review" `
            -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -Description "tpoint 每日收盘后信号复盘（交易日 15:30）：生成 HTML+JSON 并推送飞书复盘群" `
            -Force -ErrorAction Stop | Out-Null
        Write-Host "  OK: tpoint_daily_review 已注册"
        # 立即触发一次验证（非交易日会静默退出，安全）
        Write-Host "  触发一次验证..."
        Start-ScheduledTask -TaskName "tpoint_daily_review" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5
        $info = Get-ScheduledTaskInfo -TaskName "tpoint_daily_review" -ErrorAction SilentlyContinue
        Write-Host "  最后运行: $($info.LastRunTime)  结果: $($info.LastTaskResult)"
        Write-Host "  查看日志: $BASE\logs\daily_review.log"
    } catch {
        Write-Host "  FAIL: $($_.Exception.Message)"
    }
}
Write-Host ""

# ---------- 列出所有 tpoint 相关任务 ----------
Write-Host "[2] 所有 tpoint 相关计划任务:"
$all = Get-ScheduledTask | Where-Object { $_.TaskName -match 'tpoint|monitor|alert_engine|selfcheck|miji|daily_review' }
if ($all) {
    $all | ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -ErrorAction SilentlyContinue
        Write-Host "  [$($_.TaskName)] state=$($_.State) last=$($info.LastRunTime) next=$($info.NextRunTime)"
    }
} else {
    Write-Host "  (无)"
}
Write-Host ""

Write-Host "============================================="
Write-Host " 完成。验证命令:"
Write-Host "   Get-ScheduledTask -TaskName 'tpoint_daily_review' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host " 手动触发复盘:"
Write-Host "   Start-ScheduledTask -TaskName 'tpoint_daily_review'"
Write-Host " 或直接运行:"
Write-Host "   $BAT"
Write-Host "============================================="
