#define AppName "QuotaCrew"
#define AppVersion "0.2.3"
#define AppExe "QuotaCrew.exe"
#define AppPublisher "QuotaCrew contributors"
#ifndef BundleRoot
  #define BundleRoot "..\dist"
#endif

[Setup]
SetupIconFile=assets\app.ico
AppId={{7C2E5F3A-1D2B-4E6A-9C11-590DBC049B83}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={userpf}\QuotaCrew
DefaultGroupName={#AppName}
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=QuotaCrew-Setup-{#AppVersion}
OutputDir=..\installer\Output
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
english.StartOnLogin=Start QuotaCrew when I log in
turkish.StartOnLogin=Windows oturumu açıldığında QuotaCrew başlasın
english.DesktopShortcut=Create a desktop shortcut
turkish.DesktopShortcut=Masaüstü kısayolu oluştur
english.LaunchApp=Launch QuotaCrew
turkish.LaunchApp=QuotaCrew uygulamasını aç
english.UninstallApp=Uninstall QuotaCrew
turkish.UninstallApp=QuotaCrew uygulamasını kaldır

[Tasks]
Name: "startupicon"; Description: "{cm:StartOnLogin}"; Flags: unchecked
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; Flags: unchecked

[Files]
Source: "{#BundleRoot}\QuotaCrew\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundleRoot}\cx\*"; DestDir: "{app}\cli"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "WINDOWS-README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\cli\_internal"
Type: files; Name: "{app}\CodexAccountManager.exe"
Type: files; Name: "{userdesktop}\Codex Account Manager.lnk"
Type: files; Name: "{userprograms}\Codex Account Manager.lnk"
Type: files; Name: "{userprograms}\Codex Account Manager\Codex Account Manager.lnk"
Type: files; Name: "{userprograms}\Codex Account Manager\Uninstall Codex Account Manager.lnk"
Type: files; Name: "{userprograms}\Codex Account Manager\Codex Hesap Yöneticisi uygulamasını kaldır.lnk"
Type: dirifempty; Name: "{userprograms}\Codex Account Manager"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "CodexAccountManager.Desktop"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "CodexAccountManager.Desktop"; Check: DesktopShortcutRequested
Name: "{group}\{cm:UninstallApp}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: ReopenAfterUpdate

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CodexAccountManager"; ValueData: """{app}\{#AppExe}"""; Check: StartOnLoginRequested; Flags: uninsdeletevalue

[Code]
var
  HadStartOnLogin: Boolean;
  HadDesktopShortcut: Boolean;

function DesktopShortcutRequested(): Boolean;
begin
  Result := HadDesktopShortcut or WizardIsTaskSelected('desktopicon');
end;

function StartOnLoginRequested(): Boolean;
begin
  Result := HadStartOnLogin or WizardIsTaskSelected('startupicon');
end;

function OpenProcess(Access: LongWord; Inherit: Boolean; ProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function InitializeSetup(): Boolean;
var
  ProcessId: Integer;
  Handle: THandle;
begin
  Result := True;
  HadStartOnLogin := RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CodexAccountManager');
  HadDesktopShortcut := FileExists(ExpandConstant('{userdesktop}\Codex Account Manager.lnk')) or FileExists(ExpandConstant('{userdesktop}\QuotaCrew.lnk'));
  ProcessId := StrToIntDef(ExpandConstant('{param:WAITFORPID|0}'), 0);
  if ProcessId <= 0 then Exit;
  Handle := OpenProcess($00100000, False, ProcessId);
  if Handle = 0 then Exit;
  try
    Result := WaitForSingleObject(Handle, 60000) = 0;
  finally
    CloseHandle(Handle);
  end;
end;

function ReopenAfterUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:REOPENAPP|0}') = '1';
end;
