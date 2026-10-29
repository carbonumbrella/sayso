# SaySo installer step (run elevated, once): registers the SaySoHotkeys task, the elevated hotkey listener
# (hotkey_helper.py) that keeps the dictation/command keys working over admin windows (UIPI), and starts it.
# It only forwards key events to the non-elevated dictate.py; nothing else runs elevated.
# Undo: Unregister-ScheduledTask -TaskName SaySoHotkeys -Confirm:$false
param([Parameter(Mandatory = $true)][string]$AppDir, [string]$User = "$env:USERDOMAIN\$env:USERNAME")
$ErrorActionPreference = 'Stop'
$log = Join-Path $AppDir 'install_hotkey_task.log'
try {
    $action = New-ScheduledTaskAction -Execute (Join-Path $AppDir '.venv\Scripts\pythonw.exe') `
        -Argument ('"' + (Join-Path $AppDir 'hotkey_helper.py') + '"') -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName 'SaySoHotkeys' -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "SaySo hotkey listener ($AppDir)" -Force | Out-Null
    Start-ScheduledTask -TaskName 'SaySoHotkeys'
    "OK $(Get-Date -Format s)" | Set-Content $log
} catch {
    "FAIL $($_.Exception.Message)" | Set-Content $log
    exit 1
}
