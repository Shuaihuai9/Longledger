; 长衡 LongLedger · Inno Setup 6 安装脚本
; 输入：..\dist\LongLedger（由 build_nuitka.bat 生成）
; 输出：installer\Output\LongLedger-Setup-1.0.0.exe
;
; 版本号与 config\settings.py、build_nuitka.bat、README.md 保持一致。

#define MyAppName "长衡"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "LongLedger"
#define MyAppExeName "LongLedger.exe"
#define MyAppId "{{8D3B2C41-6F7A-4C25-9E58-3B7A1D9C0F62}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\LongLedger
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=LongLedger-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\brand\changheng.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=admin
; 安装目录只放程序，用户数据在安装向导里单独选择

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\LongLedger\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

; 两个数据目录由 [Code] 在安装结束时写入 paths.ini（UTF-8，兼容中文路径），
; 该文件不随卸载删除，重装时 InitializeWizard 会预填上次选择

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  AccountDirPage: TInputDirWizardPage;
  QuoteDirPage: TInputDirWizardPage;

function GetAccountDir(Param: String): String;
begin
  Result := AccountDirPage.Values[0];
end;

function GetQuoteDir(Param: String): String;
begin
  Result := QuoteDirPage.Values[0];
end;

{ 自己解析 paths.ini：GetIniString 按 ANSI 读，中文路径会乱码 }
function ReadIniValue(const FileName, Key, Default: String): String;
var
  Lines: TArrayOfString;
  i, p: Integer;
  Line: String;
begin
  Result := Default;
  if not FileExists(FileName) then Exit;
  if not LoadStringsFromFile(FileName, Lines) then Exit;
  for i := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[i];
    p := Pos('=', Line);
    if (p > 0) and (CompareText(Trim(Copy(Line, 1, p - 1)), Key) = 0) then
      Result := Trim(Copy(Line, p + 1, Length(Line) - p));
  end;
end;

procedure InitializeWizard;
var
  IniPath, AccountDefault, QuoteDefault: String;
begin
  IniPath := ExpandConstant('{commonappdata}\LongLedger\paths.ini');
  AccountDefault := ReadIniValue(IniPath, 'AccountDataDir',
    ExpandConstant('{userappdata}\LongLedger\data'));
  QuoteDefault := ReadIniValue(IniPath, 'QuoteDataDir',
    ExpandConstant('{localappdata}\LongLedger\market'));

  AccountDirPage := CreateInputDirPage(wpSelectDir,
    '账户数据目录',
    '账户、成交、红利与净值数据保存在哪里？',
    '账户库 longledger.db 会写入该目录；升级或重装时保持原目录即可继续使用历史账本。',
    False, '');
  AccountDirPage.Add('账户数据目录：');
  AccountDirPage.Values[0] := AccountDefault;

  QuoteDirPage := CreateInputDirPage(AccountDirPage.ID,
    '行情数据目录',
    '下载的日线行情保存在哪里？',
    '行情库 market.db 与账户库分离，可单独放置、单独备份或清理。',
    False, '');
  QuoteDirPage.Add('行情数据目录：');
  QuoteDirPage.Values[0] := QuoteDefault;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = AccountDirPage.ID) and (Trim(AccountDirPage.Values[0]) = '') then
  begin
    MsgBox('请选择账户数据目录。', mbError, MB_OK);
    Result := False;
  end
  else if (CurPageID = QuoteDirPage.ID) and (Trim(QuoteDirPage.Values[0]) = '') then
  begin
    MsgBox('请选择行情数据目录。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines: TArrayOfString;
  IniDir: String;
begin
  if CurStep = ssPostInstall then
  begin
    IniDir := ExpandConstant('{commonappdata}\LongLedger');
    ForceDirectories(IniDir);
    SetArrayLength(Lines, 3);
    Lines[0] := '[Paths]';
    Lines[1] := 'AccountDataDir=' + AccountDirPage.Values[0];
    Lines[2] := 'QuoteDataDir=' + QuoteDirPage.Values[0];
    SaveStringsToUTF8File(IniDir + '\paths.ini', Lines, False);
    ForceDirectories(AccountDirPage.Values[0]);
    ForceDirectories(QuoteDirPage.Values[0]);
  end;
end;
