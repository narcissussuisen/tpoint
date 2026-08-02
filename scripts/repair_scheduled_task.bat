@echo off
setlocal enabledelayedexpansion
title tpoint plan task repair

set VENV=C:\Users\YZP\WorkBuddy\Claw\tpoint\venv\Scripts\python.exe
set CORE=C:\Users\YZP\WorkBuddy\Claw\tpoint\core
set LOG=%CORE%\..\repair-log.txt

echo =============================================
echo  tpoint scheduled task repair
echo  %date% %time%
echo =============================================
echo.

echo [0] Check venv python ...
if exist "%VENV%" (
    echo   OK: %VENV%
    "%VENV%" -c "import mootdx; print(mootdx.__version__)" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo   OK: dependencies complete
    ) else (
        echo   FAIL: dependencies missing. Run: pip install -r config/requirements.txt
    )
) else (
    echo   FAIL: venv python not found at %VENV%
)
echo.

echo [1] Search for tpoint scheduled task ...
schtasks /query /tn "tpoint" >nul 2>&1
if !ERRORLEVEL! equ 0 (
    echo   FOUND: task named "tpoint"
    echo [2] Show current config ...
    schtasks /query /tn "tpoint" /v /fo list
    echo.
    echo [3] Change to venv python ...
    schtasks /change /tn "tpoint" /tr "\"%VENV%\" monitor.py" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo   OK: command path updated
        echo [4] Trigger task ...
        schtasks /run /tn "tpoint" >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            echo   OK: task triggered
        ) else (
            echo   WARN: trigger failed (may need admin rights)
        )
    ) else (
        echo   FAIL: could not change task (may need admin rights)
    )
) else (
    echo   WARN: task "tpoint" not found, or schtasks blocked by security policy
    echo.
    echo   Try searching with PowerShell:
    echo     Get-ScheduledTask ^| Where-Object {$_.TaskName -like '*tpoint*'}
)

echo.
echo =============================================
echo  MANUAL FIX (if automatic repair failed):
echo.
echo  1. Press Win+R, type: taskschd.msc
echo  2. Find the tpoint task
echo  3. Right-click - Properties - Actions - Edit
echo  4. Set Program: %VENV%
echo  5. Set Arguments: monitor.py
echo  6. Set "Start in": %CORE%
echo.
echo  OR just run tpoint manually:
echo    cd /d %CORE%
echo    "%VENV%" monitor.py
echo =============================================
echo.
pause
