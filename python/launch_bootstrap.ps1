# Runs bootstrap.ps1 OUTSIDE the installer's process tree and waits for it. Inno Setup starts its children with
# a redirection-trust mitigation, under which uv cannot use its managed Python (a junction): "os error 448,
# untrusted mount point". A process created through WMI (Win32_Process.Create) does not inherit it.
# Exit code 0 when bootstrap.log ends with "done". The same code goes to bootstrap_exit.txt, which the installer
# polls (it shows the progress meanwhile): it is written on every path, or the installer would wait forever.
param([Parameter(Mandatory = $true)][string]$AppDir, [switch]$Minimal, [string]$Model = 'auto')
$log = Join-Path $AppDir 'bootstrap.log'
$exitFile = Join-Path $AppDir 'bootstrap_exit.txt'
function Done([int]$code) { Set-Content $exitFile $code -Encoding ascii; exit $code }
try {
    foreach ($f in $log, $exitFile, (Join-Path $AppDir 'setup_step.txt')) { if (Test-Path $f) { Remove-Item $f -Force } }
    $cmd = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $AppDir 'bootstrap.ps1') +
           '" -AppDir "' + $AppDir + '" -Model ' + $Model + $(if ($Minimal) { ' -Minimal' } else { '' })
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $AppDir }
    if ($r.ReturnValue -ne 0) { Done 2 }
    while (Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }
    if ((Test-Path $log) -and ((Get-Content $log -Tail 1) -match ' done$')) { Done 0 }
    Done 1
} catch {
    Add-Content $log "launch_bootstrap: $($_.Exception.Message)" -Encoding utf8
    Done 3
}
