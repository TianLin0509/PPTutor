; PPT Doctor 安装器（Inno Setup 6/7）
; 构建：scratchpad\innosetup\ISCC.exe tools\installer.iss（仓库根目录下执行）
; 产物：artifacts\PPT-Doctor-Setup-v1.2.4.exe
; 设计约束：
;   - 装到 {localappdata}\Programs —— 免 UAC，且增量更新 helper 需要就地换文件的写权限
;   - 自启完全交给应用内设置（versioning/autostart.py），安装器不写 Run 项，避免双重注册
;   - 用户数据（index.db / vault）在 %LOCALAPPDATA%\pptx-finder，不在 {app}，卸载不触碰

#define AppVersion "1.2.4"

[Setup]
AppId={{B7E2A8F3-5C4D-4E1F-9A2B-3D4C5E6F7A08}
AppName=PPT Doctor
AppVersion={#AppVersion}
AppPublisher=TianLin
AppPublisherURL=https://github.com/TianLin0509/PPTutor
DefaultDirName={localappdata}\Programs\PPT Doctor
DefaultGroupName=PPT Doctor
OutputDir=..\artifacts
OutputBaseFilename=PPT-Doctor-Setup-v{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\PPT Doctor.exe
CloseApplications=yes
VersionInfoVersion={#AppVersion}.0

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\PPT Doctor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\PPT Doctor"; Filename: "{app}\PPT Doctor.exe"
Name: "{group}\卸载 PPT Doctor"; Filename: "{uninstallexe}"
Name: "{userdesktop}\PPT Doctor"; Filename: "{app}\PPT Doctor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PPT Doctor.exe"; Description: "启动 PPT Doctor（托盘常驻）"; Flags: postinstall nowait skipifsilent unchecked
