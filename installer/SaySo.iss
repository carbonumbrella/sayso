; SaySo installer (Inno Setup 6). Per-user, no admin for the files; one UAC prompt only for the elevated
; hotkey task. The installer ships code only: bootstrap.ps1 installs Python + packages (uv) and downloads the
; Whisper model for this PC (NPU > Intel GPU > CPU), so the setup file stays small. Setup type: full (dictation +
; assistant) or minimal (dictation only, with a Whisper model choice).
; Build: build.cmd (stages the Python files from PYSRC, default C:\AI\openvino-npu).
; The tray exe is framework-dependent: a missing .NET 10 Desktop Runtime is downloaded and installed (own UAC).

#define AppName "SaySo"
#define RuntimeUrl "https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-x64.exe"
#ifndef AppVersion
  #define AppVersion "0.3.16"
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
DisableDirPage=auto
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
Name: "autostart"; Description: "Запускать SaySo вместе с Windows"
Name: "hotkeys"; Description: "Кнопки работают и поверх окон администратора (один запрос UAC)"
Name: "desktopicon"; Description: "Ярлык на рабочем столе"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; autostart is the per-user logon task "SaySo" (the Run key waits for the desktop in Xbox mode); old Run values go
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "SaySo"; Flags: deletevalue uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ClawVoice"; Flags: deletevalue

[Icons]
Name: "{userprograms}\SaySo"; Filename: "{app}\app\SaySo.exe"; Parameters: "--settings"
Name: "{userdesktop}\SaySo"; Filename: "{app}\app\SaySo.exe"; Parameters: "--settings"; Tasks: desktopicon

[Run]
Filename: "{app}\app\SaySo.exe"; Parameters: "--set-autostart on"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\app\SaySo.exe"; Parameters: "--set-autostart off"; Tasks: not autostart; Flags: runhidden waituntilterminated
Filename: "{app}\app\SaySo.exe"; Description: "Запустить SaySo"; Flags: nowait postinstall skipifsilent
Filename: "{app}\app\SaySo.exe"; Flags: nowait; Check: RestartAfterSilent

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
Type: filesandordirs; Name: "{app}\whisper-large-v3-turbo-fp16-ov"
Type: filesandordirs; Name: "{app}\gemma-3-4b-it-int4-cw-ov"
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
  SetupPage, ModelPage: TInputOptionWizardPage;
  WasRunning: Boolean;  // SaySo ran before this update: a silent update starts it again

function RestartAfterSilent: Boolean;
begin
  Result := WasRunning and WizardSilent;
end;

function ModelKey: String;
begin
  // the model page is shown for the minimal setup only; the full one picks the model by the hardware
  if SetupPage.Values[0] or ModelPage.Values[0] then Result := 'auto'
  else if ModelPage.Values[1] then Result := 'small'
  else Result := 'turbo';
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  if SetupPage.Values[1] then SetPreviousData(PreviousDataKey, 'SetupType', 'minimal')
  else SetPreviousData(PreviousDataKey, 'SetupType', 'full');
  SetPreviousData(PreviousDataKey, 'SttModel', ModelKey);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ModelPage.ID) and SetupPage.Values[0];
end;

function RuntimeInstalled: Boolean;
var
  R: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{commonpf64}\dotnet\shared\Microsoft.WindowsDesktop.App\10.*'), R);
  if Result then FindClose(R);
end;

procedure InitializeWizard;
var
  Prev: String;
begin
  WizardForm.SelectDirBrowseLabel.Caption := 'Сюда же установщик скачает Python и модель распознавания: от 1.5 до 3 ГБ, ' +
    'с языковой моделью Gemma ещё 3.3 ГБ. Папка должна быть доступна без прав администратора: Program Files не подойдёт. ' +
    'Настройки хранятся отдельно, в %APPDATA%\SaySo. Нажми «Далее», чтобы продолжить, или «Обзор», чтобы выбрать другую папку.';
  DownloadPage := CreateDownloadPage('Загрузка .NET 10', 'Для работы нужна среда .NET 10 Desktop Runtime от Microsoft.', nil);
  SetupPage := CreateInputOptionPage(wpSelectTasks, 'Тип установки', 'Что установить?',
    'Распознавание речи работает на этом компьютере в обоих вариантах. Помощника можно включить и позже, в настройках. ' +
    'Во время установки скачиваются Python и модель распознавания (от 250 МБ до 1.6 ГБ): возможно, для скачивания потребуется VPN.',
    True, False);
  SetupPage.Add('Полная: диктовка и голосовой помощник (команды, имя, ответы голосом, Телеграм, плагины)');
  SetupPage.Add('Минимальная: только диктовка, с выбором модели распознавания');
  // a silent install picks them on the command line: /TYPE=full|minimal /MODEL=auto|small|turbo
  Prev := ExpandConstant('{param:TYPE|}');
  if Prev = '' then Prev := GetPreviousData('SetupType', 'full');
  SetupPage.Values[Ord(Prev = 'minimal')] := True;
  ModelPage := CreateInputOptionPage(SetupPage.ID, 'Модель распознавания', 'Какую модель Whisper скачать?',
    'Обе модели работают на NPU, видеокарте и процессоре, SaySo сам выберет, где запускать: сначала NPU, ' +
    'потом видеокарта, потом процессор. Модель меняется и докачивается потом в настройках, вкладка «Распознавание».', True, False);
  ModelPage.Add('Авто: точная, если есть видеокарта без NPU; иначе быстрая');
  ModelPage.Add('Быстрая (Whisper small, 244 МБ, ~0.8 ГБ памяти): быстро везде, когда играет музыка, ошибается чаще');
  ModelPage.Add('Точная (Whisper turbo, 0.8 ГБ, для NPU своя сборка 1.6 ГБ; 1.3-2 ГБ памяти): почти без ошибок в тишине и точнее, когда играет негромкая музыка; при громкой ошибаются обе. ' +
    'На NPU в первый раз готовится около 6 минут, пока работает быстрая; на процессоре в 4 раза медленнее быстрой');
  Prev := ExpandConstant('{param:MODEL|}');
  if Prev = '' then Prev := GetPreviousData('SttModel', 'auto');
  if Prev = 'small' then ModelPage.Values[1] := True
  else if Prev = 'turbo' then ModelPage.Values[2] := True
  else ModelPage.Values[0] := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Code: Integer;
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    // per-user install (no admin): a protected folder fails later, when Python is being written
    Dir := Lowercase(AddBackslash(WizardDirValue));
    if (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf64}'))), Dir) = 1) or
       (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf32}'))), Dir) = 1) or
       (Pos(Lowercase(AddBackslash(ExpandConstant('{win}'))), Dir) = 1) then
    begin
      MsgBox('В эту папку нельзя установить без прав администратора. Выбери папку в своём профиле или на другом диске, ' +
             'например D:\SaySo.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
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
  // an update over a running SaySo: its exe and helpers are in use and the copy failed ("код 5", a silent install
  // then aborted or hung). Quit it, then end whatever still runs from the app folder (dictate, mic helper).
  if FileExists(ExpandConstant('{app}\app\SaySo.exe')) then
  begin
    WasRunning := CheckForMutexes('Local\SaySo') or CheckForMutexes('Local\ClawOverlay');
    Exec(ExpandConstant('{app}\app\SaySo.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoProfile -Command "Get-CimInstance Win32_Process | ' +
         'Where-Object { $_.ExecutablePath -like ''' + ExpandConstant('{app}') + '\*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(1500);
  end;
  Old := ExpandConstant('{app}\tray\SaySo.exe');
  if FileExists(Old) then
  begin
    Exec(Old, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(1500);
    DelTree(ExpandConstant('{app}\tray'), True, True, True);
  end;
end;

// A number after Key in download_progress.json ({"key": "small", "mb": 120, "total": 244}); -1 if absent.
function JsonInt(const S, Key: String): Integer;
var
  P: Integer;
  T: String;
begin
  Result := -1;
  P := Pos('"' + Key + '": ', S);
  if P = 0 then Exit;
  T := Copy(S, P + Length(Key) + 4, 12);
  P := 1;
  while (P <= Length(T)) and (T[P] >= '0') and (T[P] <= '9') do P := P + 1;
  Result := StrToIntDef(Copy(T, 1, P - 1), -1);
end;

// bootstrap.ps1 runs while the wizard shows what it is doing: its current step (setup_step.txt) and, for the
// model, the MB downloaded (download_progress.json). A full bar with a fixed text looked hung.
function RunBootstrap(const Args: String): Integer;
var
  Code, Mb, Total: Integer;
  App, Step, Prog: AnsiString;
  Page: TOutputProgressWizardPage;
begin
  App := ExpandConstant('{app}');
  DeleteFile(App + '\bootstrap_exit.txt');
  Result := -1;
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoProfile -ExecutionPolicy Bypass -File "' +
              App + '\launch_bootstrap.ps1" -AppDir "' + App + '"' + Args, '', SW_HIDE, ewNoWait, Code) then Exit;
  // a progress page: its SetProgress keeps the window responsive while this loop waits
  Page := CreateOutputProgressPage('Python и модель распознавания', 'Это займёт несколько минут, окно не зависло.');
  Page.Show;
  try
    while not FileExists(App + '\bootstrap_exit.txt') do
    begin
      Step := '';
      LoadStringFromFile(App + '\setup_step.txt', Step);
      Prog := '';
      Mb := -1;
      Total := 0;
      if LoadStringFromFile(App + '\download_progress.json', Prog) then
      begin
        Mb := JsonInt(Prog, 'mb');
        Total := JsonInt(Prog, 'total');
      end;
      if (Mb >= 0) and (Total > 0) then
      begin
        Page.ProgressBar.Style := npbstNormal;
        Page.SetText('Скачиваю модель распознавания: ' + IntToStr(Mb) + ' из ' + IntToStr(Total) + ' МБ', '');
        Page.SetProgress(Mb, Total);
      end
      else
      begin
        Page.ProgressBar.Style := npbstMarquee;
        if Pos('downloading uv', Step) > 0 then Page.SetText('Скачиваю установщик Python…', '')
        else if Pos('installing packages', Step) > 0 then Page.SetText('Устанавливаю пакеты Python, около 300 МБ…', '')
        else if Pos('downloading the speech model', Step) > 0 then Page.SetText('Подключаюсь к huggingface.co за моделью…', '')
        else Page.SetText('Устанавливаю Python…', '');
        Page.SetProgress(0, 0);
      end;
      Sleep(250);
    end;
  finally
    Page.Hide;
  end;
  Step := '';
  LoadStringFromFile(App + '\bootstrap_exit.txt', Step);
  Result := StrToIntDef(Trim(Step), 1);
end;

// After the files are copied: Python + packages + model (bootstrap.ps1), then the elevated hotkey task.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  PS, Setup: String;
begin
  if CurStep <> ssPostInstall then Exit;
  PS := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  WizardForm.StatusLabel.Caption := 'Устанавливаю Python и модель распознавания речи. Это займёт несколько минут…';
  Setup := ' -Model ' + ModelKey;
  if SetupPage.Values[1] then Setup := Setup + ' -Minimal';
  if RunBootstrap(Setup) <> 0 then
    SuppressibleMsgBox('Не получилось установить Python или скачать модель распознавания. Подробности: ' +
           ExpandConstant('{app}\bootstrap.log') + '. Модель скачивается с huggingface.co, возможно, для скачивания ' +
           'потребуется VPN. Потом запусти установку ещё раз, готовые шаги пропустятся.',
           mbError, MB_OK, IDOK);  // suppressible: a silent install (/SUPPRESSMSGBOXES) must not pop up a box
  // a silent update over a running SaySo keeps the elevated task from the first install: no UAC box each update
  if WizardIsTaskSelected('hotkeys') and not (WizardSilent and WasRunning) then
  begin
    WizardForm.StatusLabel.Caption := 'Регистрирую кнопки…';
    ShellExec('runas', PS, '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install_hotkey_task.ps1') +
              '" -AppDir "' + ExpandConstant('{app}') + '" -User "' + GetUserNameString + '"', '', SW_HIDE,
              ewWaitUntilTerminated, Code);
  end;
end;
