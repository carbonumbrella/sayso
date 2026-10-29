@echo off
rem Builds publish\SaySo.exe, stages the app (tray exe in app\ + Python code from PYSRC) and compiles the installer
rem publish\installer\SaySo-Setup-<version>.exe. Needs the .NET 10 SDK (DOTNET=...) and Inno Setup 6.
setlocal
cd /d "%~dp0"
if not defined DOTNET set "DOTNET=C:\AI\dotnet\dotnet.exe"
rem Python code: python\ next to this file (the public repo), else the dev folder
if not defined PYSRC if exist "%~dp0python\dictate.py" set "PYSRC=%~dp0python"
if not defined PYSRC set "PYSRC=C:\AI\openvino-npu"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
set DOTNET_CLI_TELEMETRY_OPTOUT=1
set DOTNET_NOLOGO=1

"%DOTNET%" publish src -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true ^
  -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=none -o publish || exit /b 1

rem the app as installed: code at the root, tray exe in app\ (not tray\: ClawTweaks garbles a \t in paths).
rem Plugins: english + the MSI Claw ones (each stays idle when its app, ClawTweaks / ClawAmbilight, is missing).
set "STAGE=publish\stage"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%\app" "%STAGE%\plugins"
copy /y publish\SaySo.exe "%STAGE%\app\" >nul
for %%F in (dictate.py gemma_agent.py net_skills.py wake.py hotkey_helper.py voice_settings.py plugins.py commands.py ^
            tg.py sayso_cli.py npu_server.py ps_host.ps1 confirm.ps1 install_hotkey_task.ps1 bootstrap.ps1 launch_bootstrap.ps1 ^
            requirements.txt requirements-npu.txt silero_vad.onnx) do copy /y "%PYSRC%\%%F" "%STAGE%\" >nul || exit /b 1
for %%F in (english.py claw_tweaks.py _claw_ipc.py ambilight.py) do copy /y "%PYSRC%\plugins\%%F" "%STAGE%\plugins\" >nul || exit /b 1

for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "(Get-Item publish\SaySo.exe).VersionInfo.ProductVersion.Split('+')[0]"`) do set VER=%%V
if not defined ISCC (
  echo Inno Setup 6 not found: staged in %STAGE% only.
  exit /b 0
)
"%ISCC%" /Q /DAppVersion=%VER% /DStage="..\%STAGE%" installer\SaySo.iss || exit /b 1
echo Done: publish\installer\SaySo-Setup-%VER%.exe
