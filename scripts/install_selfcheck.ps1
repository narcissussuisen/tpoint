# ============================================================
#  tpoint 自检计划任务注册脚本
#  注册 tpoint_selfcheck 任务：每周一至五 09:00 执行自检
#  同时检查 tpoint_monitor / tpoint_alert_engine 是否已注册
#
#  使用方式（普通用户即可，无需管理员）:
#    PowerShell -ExecutionPolicy Bypass -File scripts\install_selfcheck.ps1
#
#  如需以管理员注册 SYSTEM 任务(monitor/engine)，请另运行:
#    以管理员身份运行 scripts\install_tasks.bat
# ============================================================

$ErrorActionPreference = "Continue"
$BASE = "C:\Users\YZP\WorkBuddy\Claw\tpoint"
$BAT = "$BASE\scripts\run_selfcheck.bat"
$USER = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "============================================="
Write-Host " tpoint 自检计划任务注册"
Write-Host " $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host " 用户: $USER"
Write-Host "============================================="
Write-Host ""

# ---------- 1. 注册 tpoint_selfcheck (周一至五 09:00) ----------
Write-Host "[1] 注册 tpoint_selfcheck (每周一至五 09:00)..."

if (-not (Test-Path $BAT)) {
    Write-Host "  FAIL: 启动脚本不存在: $BAT"
} else {
    $action = New-ScheduledTaskAction -Execute $BAT -WorkingDirectory $BASE
    # 每周一至五 09:00
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00am
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)
    # 以当前用户运行（Interactive，需用户已登录；9:00 交易日通常已登录）
    $principal = New-ScheduledTaskPrincipal -UserId $USER -LogonType Interactive -RunLevel Limited

    try {
        Register-ScheduledTask -TaskName "tpoint_selfcheck" `
            -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -Description "tpoint 系统每日自检（交易日 09:00）：启动状态/服务运行/监控/端口/资源/计划任务" `
            -Force -ErrorAction Stop | Out-Null
        Write-Host "  OK: tpoint_selfcheck 已注册"
        # 立即触发一次验证
        Write-Host "  触发一次验证..."
        Start-ScheduledTask -TaskName "tpoint_selfcheck" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        $info = Get-ScheduledTaskInfo -TaskName "tpoint_selfcheck" -ErrorAction SilentlyContinue
        Write-Host "  最后运行: $($info.LastRunTime)  结果: $($info.LastTaskResult)"
    } catch {
        Write-Host "  FAIL: $($_.Exception.Message)"
        Write-Host "  提示: 如需以 SYSTEM 注册（无需登录），请以管理员身份运行："
        Write-Host "    PowerShell -ExecutionPolicy Bypass -File scripts\install_selfcheck.ps1 -AsAdmin"
    }
}
Write-Host ""

# ---------- 2. 检查 tpoint_monitor / tpoint_alert_engine ----------
Write-Host "[2] 检查 tpoint_monitor / tpoint_alert_engine 计划任务..."

$expected = @("tpoint_monitor", "tpoint_alert_engine")
foreach ($name in $expected) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
        $a = $t.Actions[0]
        Write-Host "  OK: [$name] state=$($t.State) last=$($info.LastRunTime) result=$($info.LastTaskResult)"
        Write-Host "       cmd=$($a.Execute) $($a.Arguments)"
    } else {
        Write-Host "  WARN: [$name] 未注册（当前用户可能无法看到 SYSTEM 任务）"
        Write-Host "       如 monitor/engine 进程已在运行，可忽略此警告"
        Write-Host "       如需注册，以管理员运行 scripts\install_tasks.bat"
    }
}
Write-Host ""

# ---------- 3. 列出所有 tpoint 相关任务 ----------
Write-Host "[3] 所有 tpoint 相关计划任务:"
$all = Get-ScheduledTask | Where-Object { $_.TaskName -match 'tpoint|monitor|alert_engine|selfcheck|miji' }
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
Write-Host "   Get-ScheduledTask -TaskName 'tpoint_selfcheck' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host " 手动触发自检:"
Write-Host "   Start-ScheduledTask -TaskName 'tpoint_selfcheck'"
Write-Host " 或直接运行:"
Write-Host "   $BAT"
Write-Host "============================================="
