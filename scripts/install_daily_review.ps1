# tpoint daily review scheduled task installer
# Register tpoint_daily_review: Mon-Fri 15:30
# Usage: PowerShell -ExecutionPolicy Bypass -File scripts\install_daily_review.ps1

$ErrorActionPreference = "Continue"
$BASE = "C:\Users\YZP\WorkBuddy\Claw\tpoint"
$BAT = "$BASE\scripts\run_daily_review.bat"
$USER = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "--- tpoint daily review task installer ---"
Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Write-Host "user: $USER"
Write-Host ""

Write-Host "[1] register tpoint_daily_review (Mon-Fri 15:30)..."

if (-not (Test-Path $BAT)) {
    Write-Host "  FAIL: entry script not found: $BAT"
} else {
    $action = New-ScheduledTaskAction -Execute $BAT -WorkingDirectory $BASE
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 3:30pm
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2)
    $principal = New-ScheduledTaskPrincipal -UserId $USER -LogonType Interactive -RunLevel Limited

    try {
        Register-ScheduledTask -TaskName "tpoint_daily_review" `
            -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -Description "tpoint daily signal review at 15:30 on trading days" `
            -Force -ErrorAction Stop | Out-Null
        Write-Host "  OK: tpoint_daily_review registered"
        Write-Host "  triggering a test run..."
        Start-ScheduledTask -TaskName "tpoint_daily_review" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5
        $info = Get-ScheduledTaskInfo -TaskName "tpoint_daily_review" -ErrorAction SilentlyContinue
        Write-Host "  last run: $($info.LastRunTime)  result: $($info.LastTaskResult)"
        Write-Host "  log: $BASE\logs\daily_review.log"
    } catch {
        Write-Host "  FAIL: $($_.Exception.Message)"
    }
}
Write-Host ""

Write-Host "[2] tpoint related scheduled tasks:"
$all = Get-ScheduledTask | Where-Object { $_.TaskName -match 'tpoint|monitor|alert_engine|selfcheck|miji|daily_review' }
if ($all) {
    $all | ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -ErrorAction SilentlyContinue
        Write-Host "  [$($_.TaskName)] state=$($_.State) last=$($info.LastRunTime) next=$($info.NextRunTime)"
    }
} else {
    Write-Host "  (none)"
}
Write-Host ""

Write-Host "--- done ---"
Write-Host "verify: Get-ScheduledTask -TaskName 'tpoint_daily_review' | Get-ScheduledTaskInfo"
Write-Host "manual: Start-ScheduledTask -TaskName 'tpoint_daily_review'"
Write-Host "direct: $BAT"
