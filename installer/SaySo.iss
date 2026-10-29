; SaySo installer (Inno Setup 6). Per-user, no admin for the files; one UAC prompt only for the elevated
; hotkey task. The installer ships code only: bootstrap.ps1 installs Python + packages (uv) and downloads the
; Whisper model for this PC (NPU > Intel GPU > CPU), so the setup file stays small.
; Build: build.cmd (stages the Python files from PYSRC, default C:\AI\openvino-npu).
; The tray exe is framework-dependent: a missing .NET 10 Desktop Runtime is downloaded and installed (own UAC).

#define AppName "SaySo"
#define RuntimeUrl "https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-x64.exe"
#ifndef AppVersion
  #define AppVersion "0.3.5"
#endif
#ifndef Stage
  #define Stage "..\publish\stage"
#endif

[Setup]
AppId={{B7E5F0C2-3A41-4C7B-9E1D-5A2F8C6D9E01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SaySo
DefaultDirName={localappdata}\Programs\SaySo
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\publish\installer
OutputBaseFilename=SaySo-Setup-{#AppVersion}
SetupIconFile=..\src\app.ico
UninstallDisplayIcon={app}\app\SaySo.exe
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Запускать SaySo вместе с Windows"; Flags: checkedonce
Name: "hotkeys"; Description: "Кнопки работают и поверх окон администратора (один запрос UAC)"; Flags: checkedonce

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; autostart is the per-user logon task "SaySo" (the Run key waits for the desktop in Xbox mode); old Run values go
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "SaySo"; Flags: deletevalue uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ClawVoice"; Flags: deletevalue

[Icons]
Name: "{userprograms}\SaySo"; Filename: "{app}\app\SaySo.exe"; Parameters: "--settings"

[Run]
Filename: "{app}\app\SaySo.exe"; Parameters: "--set-autostart on"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\app\SaySo.exe"; Parameters: "--set-autostart off"; Tasks: not autostart; Flags: runhidden waituntilterminated
Filename: "{app}\app\SaySo.exe"; Description: "Запустить SaySo"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Every process of the app must be gone before [UninstallDelete]: files in use stayed behind, and the next
; install then failed on a half-deleted tools\python. The elevated hotkey helper can only be ended elevated.
Filename: "{app}\app\SaySo.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "quit"
Filename: "{app}\app\SaySo.exe"; Parameters: "--set-autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "autostart"
Filename: "{cmd}"; Parameters: "/c schtasks /End /TN SaySoHotkeys & schtasks /Delete /TN SaySoHotkeys /F"; Flags: runhidden shellexec waituntilterminated; Verb: "runas"; RunOnceId: "task"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{app}\*' } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }; Start-Sleep 1"""; Flags: runhidden waituntilterminated; RunOnceId: "kill"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\npu_cache"
Type: filesandordirs; Name: "{app}\whisper-small-int8-ov"
Type: filesandordirs; Name: "{app}\whisper-large-v3-turbo-int8-ov"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\plugins\__pycache__"
Type: files; Name: "{app}\*.log"
Type: files; Name: "{app}\*.txt"
Type: files; Name: "{app}\*.json"
Type: files; Name: "{app}\*.wav"
Type: dirifempty; Name: "{app}"

[Code]
var
  DownloadPage: TDownloadWizardPage;

function RuntimeInstalled: Boolean;
var
  R: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{commonpf64}\dotnet\shared\Microsoft.WindowsDesktop.App\10.*'), R);
  if Result then FindClose(R);
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Загрузка .NET 10', 'Для работы нужна среда .NET 10 Desktop Runtime от Microsoft.', nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Code: Integer;
begin
  Result := True;
  if (CurPageID = wpReady) and not RuntimeInstalled then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('{#RuntimeUrl}', 'windowsdesktop-runtime-win-x64.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        SuppressibleMsgBox('Не удалось скачать .NET 10: ' + AddPeriod(GetExceptionMessage) + #13#10 +
          'Установите .NET 10 Desktop Runtime вручную с dotnet.microsoft.com.', mbError, MB_OK, IDOK);
        Result := False;
        Exit;
      end;
    finally
      DownloadPage.Hide;
    end;
    // ShellExec (not Exec) so the runtime installer can raise its own UAC prompt
    if not ShellExec('', ExpandConstant('{tmp}\windowsdesktop-runtime-win-x64.exe'), '/install /quiet /norestart', '',
                     SW_SHOW, ewWaitUntilTerminated, Code) or not RuntimeInstalled then
    begin
      SuppressibleMsgBox('.NET 10 не установился (код ' + IntToStr(Code) + ').', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

// Up to 0.2.0 the exe lived in {app}\tray\. ClawTweaks garbles "\t" in the tray-app paths it sends to its
// Center (the whole quick-menu list fails to parse), so it moved to {app}\app\: stop the old copy, drop the folder.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Old: String;
begin
  Result := '';
  Old := ExpandConstant('{app}\tray\SaySo.exe');
  if FileExists(Old) then
  begin
    Exec(Old, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(1500);
    DelTree(ExpandConstant('{app}\tray'), True, True, True);
  end;
end;

// After the files are copied: Python + packages + model (bootstrap.ps1), then the elevated hotkey task.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  PS: String;
begin
  if CurStep <> ssPostInstall then Exit;
  PS := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  WizardForm.StatusLabel.Caption := 'Устанавливаю Python и модель распознавания речи. Это займёт несколько минут…';
  if not Exec(PS, '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\launch_bootstrap.ps1') + '" -AppDir "' +
              ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    SuppressibleMsgBox('Не получилось установить Python или скачать модель распознавания. Подробности: ' +
           ExpandConstant('{app}\bootstrap.log') + '. Модель скачивается с huggingface.co: если этот сайт у тебя ' +
           'не открывается, включи VPN. Потом запусти установку ещё раз, готовые шаги пропустятся.',
           mbError, MB_OK, IDOK);  // suppressible: a silent install (/SUPPRESSMSGBOXES) must not pop up a box
  if WizardIsTaskSelected('hotkeys') then
  begin
    WizardForm.StatusLabel.Caption := 'Регистрирую кнопки…';
    ShellExec('runas', PS, '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install_hotkey_task.ps1') +
              '" -AppDir "' + ExpandConstant('{app}') + '" -User "' + GetUserNameString + '"', '', SW_HIDE,
              ewWaitUntilTerminated, Code);
  end;
end;
