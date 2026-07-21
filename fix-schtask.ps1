# tpoint scheduled task repair (PowerShell)
# Right-click -> "Run with PowerShell"
$ErrorActionPreference = "Continue"
$VENV = "C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe"
$CORE = "C:\Users\YZP\WorkBuddy\Claw\tpoint\core"

Write-Host "============================================="
Write-Host " tpoint scheduled task repair (PowerShell)"
Write-Host " $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "============================================="
Write-Host ""

Write-Host "[0] Check venv python ..."
if (Test-Path $VENV) {
    Write-Host "  OK: $VENV"
    try {
        $ver = & $VENV -c "import mootdx; print(mootdx.__version__)" 2>&1
        Write-Host "  OK: mootdx $ver"
    } catch {
        Write-Host "  FAIL: $ver"
    }
} else {
    Write-Host "  FAIL: $VENV not found"
}
Write-Host ""

Write-Host "[1] Search for tpoint scheduled tasks ..."
$tasks = Get-ScheduledTask | Where-Object {
    $_.TaskName -like "*tpoint*" -or
    $_.TaskName -like "*monitor*" -or
    $_.TaskName -like "*TP*" -or
    $_.TaskName -like "*T*" -and $_.TaskName -match "[Tt][Pp]"
}
if ($tasks) {
    Write-Host "  Found $($tasks.Count) task(s):"
    foreach ($t in $tasks) {
        $a = $t.Actions[0]
        Write-Host "    [$($t.TaskName)] state=$($t.State) cmd=$($a.Execute) $($a.Arguments)"
    }
    Write-Host ""
    Write-Host "[2] Attempt to repair ..."
    foreach ($t in $tasks) {
        try {
            $newAction = New-ScheduledTaskAction -Execute $VENV -Argument "monitor.py" -WorkingDirectory $CORE
            Set-ScheduledTask -TaskName $t.TaskName -Action $newAction -ErrorAction Stop
            Write-Host "  OK: [$($t.TaskName)] updated"
            Start-ScheduledTask -TaskName $t.TaskName -ErrorAction Stop
            Write-Host "  OK: [$($t.TaskName)] triggered"
        } catch {
            Write-Host "  FAIL: [$($t.TaskName)] $($_.Exception.Message)"
        }
    }
} else {
    Write-Host "  No tpoint tasks found."
    Write-Host "  (May not exist, or may be registered under SYSTEM account - invisible to current user)"
}
Write-Host ""

Write-Host "============================================="
Write-Host " MANUAL FIX (if automatic repair failed):"
Write-Host ""
Write-Host "  1. Win+R -> taskschd.msc"
Write-Host "  2. Find the tpoint task"
Write-Host "  3. Right-click -> Properties -> Actions -> Edit"
Write-Host "  4. Program: $VENV"
Write-Host "  5. Arguments: monitor.py"
Write-Host "  6. Start in: $CORE"
Write-Host ""
Write-Host "  OR manual start:"
Write-Host "    cd $CORE"
Write-Host "    $VENV monitor.py"
Write-Host "============================================="
Write-Host ""

Read-Host "Press Enter to exit"
