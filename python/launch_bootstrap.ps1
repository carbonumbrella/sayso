# Runs bootstrap.ps1 OUTSIDE the installer's process tree and waits for it. Inno Setup starts its children with
# a redirection-trust mitigation, under which uv cannot use its managed Python (a junction): "os error 448,
# untrusted mount point". A process created through WMI (Win32_Process.Create) does not inherit it.
# Exit code 0 when bootstrap.log ends with "done".
param([Parameter(Mandatory = $true)][string]$AppDir)
$log = Join-Path $AppDir 'bootstrap.log'
if (Test-Path $log) { Remove-Item $log -Force }
$cmd = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $AppDir 'bootstrap.ps1') +
       '" -AppDir "' + $AppDir + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $AppDir }
if ($r.ReturnValue -ne 0) { exit 2 }
while (Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }
if ((Test-Path $log) -and ((Get-Content $log -Tail 1) -match ' done$')) { exit 0 }
exit 1
