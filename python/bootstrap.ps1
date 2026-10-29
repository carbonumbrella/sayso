# SaySo installer step: Python runtime + packages + the speech model for this PC's hardware.
# Run by the installer (per-user, no admin). Re-runnable: finished steps are skipped.
#   -AppDir      install folder (holds the *.py files)
#   -WithNpu     also the packages of the optional Gemma NPU server
#   -SkipModel   do not download the Whisper model (tests)
# Log: <AppDir>\bootstrap.log. Exit code 0 = ready.
param([Parameter(Mandatory = $true)][string]$AppDir, [switch]$WithNpu, [switch]$SkipModel)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$log = Join-Path $AppDir 'bootstrap.log'
function Step($msg) { "$(Get-Date -Format s) $msg" | Add-Content $log -Encoding utf8 }
# Native tools (uv, python) write progress to stderr; in Windows PowerShell 5.1 with 'Stop' that would abort
# the script. Run them with 'Continue', log everything, and judge only by the exit code.
function Run([string]$exe, [string[]]$argv) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $exe @argv 2>&1 | ForEach-Object { "$_" } | Add-Content $log -Encoding utf8 } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}

try {
    Step "start: $AppDir"
    # 1. uv (Python manager): the user's own if present, else a private copy in <app>\tools
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $tools = Join-Path $AppDir 'tools'
        $uv = Join-Path $tools 'uv.exe'
        if (-not (Test-Path $uv)) {
            New-Item -ItemType Directory -Force $tools | Out-Null
            $zip = Join-Path $env:TEMP "uv-$([guid]::NewGuid()).zip"
            Step 'downloading uv'
            Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
            Expand-Archive $zip -DestinationPath $tools -Force
            Remove-Item $zip -Force
        }
    }
    Step "uv: $uv"

    # 2. Python 3.12 + venv (uv keeps its own Python; nothing is installed system-wide)
    $py = Join-Path $AppDir '.venv\Scripts\python.exe'
    # healthy = it really starts: a venv with python.exe but no pyvenv.cfg (half-deleted on the Claw A8) passed
    # a plain Test-Path, and every later start popped the launcher's "No pyvenv.cfg file" box
    $healthy = (Test-Path $py) -and (Test-Path (Join-Path $AppDir '.venv\pyvenv.cfg')) -and ((Run $py @('-c', 'import sys')) -eq 0)
    if (-not $healthy) {
        # leftovers of a half-done uninstall (files that were in use) make uv think Python is there and then fail
        # ("failed to write ... Lib\EXTERNALLY-MANAGED: path not found"): start both from scratch
        foreach ($d in (Join-Path $AppDir 'tools\python'), (Join-Path $AppDir '.venv')) {
            if (Test-Path $d) { Step "removing the incomplete $d"; Remove-Item $d -Recurse -Force }
        }
        # A private Python inside the app (tools\python), no registry entries, no shims. The venv is created from
        # the exact versioned folder: uv's "cpython-3.12-..." minor-version link is a junction, and processes
        # started by the installer may not follow junctions ("os error 448, untrusted mount point").
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $AppDir 'tools\python'
        Run $uv @('python', 'install', '3.12', '--no-bin', '--no-registry') | Out-Null  # a failed link is not fatal
        $real = Get-ChildItem $env:UV_PYTHON_INSTALL_DIR -Directory -Filter 'cpython-3.12*' -ErrorAction SilentlyContinue |
                Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
                ForEach-Object { Join-Path $_.FullName 'python.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $real) { throw 'python 3.12 was not installed' }
        Step "python: $real"
        # the standard library venv (with pip): uv's venv puts a "trampoline" python.exe there that fails to start
        # Python inside the installer; venv's launcher simply reads the home path below
        if ((Run $real @('-m', 'venv', (Join-Path $AppDir '.venv'))) -ne 0 -or -not (Test-Path $py)) { throw 'venv was not created' }
        # uv writes the minor-version link (a junction) as the venv home even when given the real folder: point
        # it at the real folder, or every python started by the installer fails the same way
        $cfg = Join-Path $AppDir '.venv\pyvenv.cfg'
        (Get-Content $cfg) -replace '^home = .*$', ('home = ' + (Split-Path $real)) | Set-Content $cfg -Encoding ascii
    }

    # 3. packages
    Step 'installing packages'
    # the venv's own pip (seeded above), not "uv pip": uv inspects the interpreter through its junction-based
    # cache and fails the same way inside the installer
    if ((Run $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $AppDir 'requirements.txt'))) -ne 0) { throw 'pip install failed' }
    if ($WithNpu -and (Run $py @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $AppDir 'requirements-npu.txt'))) -ne 0) {
        throw 'pip install (npu) failed'
    }
    if ((Run $py @('-c', 'import openvino_genai, sounddevice, onnxruntime, numpy')) -ne 0) { throw 'packages do not import' }

    # 4. the Whisper model for the recommended device (NPU > Intel GPU > CPU)
    if (-not $SkipModel) {
        # Hugging Face sometimes drops a file mid-download ("The read operation timed out"): retry, the
        # files already fetched are kept, so each attempt only gets the rest
        for ($try = 1; ; $try++) {
            Step "downloading the speech model (attempt $try)"
            $rc = Run $py @((Join-Path $AppDir 'sayso_cli.py'), 'download')
            if ($rc -eq 0 -and (Get-Content $log -Tail 1) -notmatch '"error"') { break }
            if ($try -ge 4) { throw 'model download failed' }
            Start-Sleep -Seconds (10 * $try)
        }
    }
    Step 'done'
    exit 0
} catch {
    Step "FAIL $($_.Exception.Message)"
    exit 1
}
