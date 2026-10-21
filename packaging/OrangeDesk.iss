#define AppVersion "1.3.3"
[Setup]
AppId={{D6C0C715-6EB7-4D5B-B481-B4C7CCDD50B2}
AppName=快橙伴航
AppVersion={#AppVersion}
AppPublisher=OrangeDesk
DefaultDirName={localappdata}\Programs\OrangeDesk
DefaultGroupName=快橙伴航
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
OutputDir=..\发布
OutputBaseFilename=快橙伴航-{#AppVersion}-Setup
SetupIconFile=..\.build\brand\icon.ico
UninstallDisplayIcon={app}\OrangeDesk.exe
WizardStyle=modern
WizardImageFile=..\.build\brand\wizard.bmp
WizardImageStretch=yes
Compression=lzma2/normal
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
ChangesEnvironment=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified-6.3.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "vendor"; Description: "尚未安装快橙时，打开官方安装程序"; GroupDescription: "快橙："

[Files]
Source: "..\dist\OrangeDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\发布\安装与使用.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\发布\网络监测使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\发布\Claude出口保护使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\发布\微信直连使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\快橙伴航"; Filename: "{app}\OrangeDesk.exe"; AppUserModelID: "OrangeDesk.Companion"
Name: "{group}\使用说明"; Filename: "{app}\安装与使用.txt"
Name: "{autodesktop}\快橙伴航"; Filename: "{app}\OrangeDesk.exe"; Tasks: desktopicon; AppUserModelID: "OrangeDesk.Companion"

[Run]
Filename: "{app}\orange.exe"; Parameters: "--install-path"; Flags: runhidden waituntilterminated
Filename: "{app}\_internal\resources\vendor\FastOrangeSetup.exe"; Tasks: vendor; Check: NeedsVendor; Description: "安装快橙"; Flags: nowait skipifsilent
Filename: "{app}\OrangeDesk.exe"; Description: "打开快橙伴航"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\orange.exe"; Parameters: "--remove-path"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveUserPath"

[Registry]
Root: HKCU; Subkey: "Software\OrangeDesk\Installer"; Flags: uninsdeletekey

[Code]
function NeedsVendor: Boolean;
begin
  Result := not FileExists(ExpandConstant('{userprograms}\FastOrange\FastOrange.appref-ms'));
end;

function StopService(Directory: String): Boolean;
var ExitCode: Integer;
begin
  Result := True;
  if FileExists(Directory + '\orange.exe') then begin
    Result := Exec(Directory + '\orange.exe', 'shutdown --close-ui', Directory, SW_HIDE, ewWaitUntilTerminated, ExitCode);
    if Result then Result := ExitCode = 0;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopService(ExpandConstant('{app}')) then
    Result := '伴航尚未完成代理恢复。请先打开伴航，停止运行或执行 orange restore，再重试安装。';
end;

function InitializeUninstall(): Boolean;
var ExitCode: Integer;
begin
  Result := False;
  if not FileExists(ExpandConstant('{app}\OrangeDesk.exe')) then begin
    MsgBox('主程序缺失，无法核验并关闭独立微信分流和 Claude 出口保护，已停止卸载。请先修复安装，或使用完整更新包关闭这两项功能后再重试。', mbError, MB_OK);
    Exit;
  end;
  Result := StopService(ExpandConstant('{app}'));
  if not Result then begin
    MsgBox('代理恢复未完成，已保留程序。请先在伴航中停止运行，或执行 orange restore，然后重试卸载。', mbError, MB_OK);
    Exit;
  end;
  Result := Exec(ExpandConstant('{app}\OrangeDesk.exe'), 'wechat-direct disable --if-present', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if Result then Result := ExitCode = 0;
  if not Result then begin
    MsgBox('微信直连尚未确认关闭，已保留程序。请先在伴航中关闭“微信直连”，再重试卸载。', mbError, MB_OK);
    Exit;
  end;
  Result := Exec(ExpandConstant('{app}\OrangeDesk.exe'), 'claude-protection disable --if-present', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if Result then Result := ExitCode = 0;
  if not Result then
    MsgBox('Claude 出口保护尚未确认关闭，已保留程序。请先在伴航中关闭“Claude 出口保护”并完成管理员授权，再重试卸载。', mbError, MB_OK);
end;
