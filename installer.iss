; DC 發電機量測系統 — Inno Setup 安裝腳本
; 編譯：ISCC.exe installer.iss  → 產出 installer\DCGenTester_Setup_x.x.x.exe
; 前置：先用 DCGenTester.spec 打包出 dist\DCGenTester.exe

#define MyAppName "DC發電機量測系統"
#define MyAppExeName "DCGenTester.exe"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "捷耀"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 預設裝到使用者可寫入的位置 (lowest 權限 → %LocalAppData%\Programs)，
; 這樣 config.yaml 在程式旁可直接編輯，且不需系統管理員/UAC。
DefaultDirName={autopf}\DCGenTester
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=DCGenTester_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#MyAppName}
SetupIconFile=ui\assets\app.ico

[Files]
Source: "dist\DCGenTester.exe"; DestDir: "{app}"; Flags: ignoreversion
; config.yaml 只在「不存在時」安裝 → 重新安裝/升級不會蓋掉現場改過的設定
Source: "config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; GroupDescription: "附加捷徑："

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\編輯設定 (config.yaml)"; Filename: "notepad.exe"; Parameters: """{app}\config.yaml"""
Name: "{group}\解除安裝 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即執行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
