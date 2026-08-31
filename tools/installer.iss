; PPT Doctor 安装器（Inno Setup 6）
; 构建：uv run python tools/build_installer.py      ← 别手工跑 ISCC，见下面第一条
; 产物：artifacts\PPT-Doctor-Setup-v<版本>.exe
;
; 设计约束：
;   - 版本号**只能从命令行传入**（/DAppVersion=x.y.z），文件里不写死。此前写死成
;     1.3.2，一直漂到 1.5.3 都没人发现——因为从没真正构建过。构建脚本从
;     pptx_finder.__version__ 取值，漂不了。
;   - 装到 {localappdata}\Programs —— 免 UAC，且增量更新 helper 需要就地换文件的
;     写权限。装到 Program Files 会让自动更新永久失效。
;   - 目录名不含空格（v1.5.2 起的一致约定），理由见 CHANGELOG。
;   - 自启完全交给应用内设置（versioning/autostart.py），安装器不写 Run 项，
;     避免双重注册。
;   - 用户数据（index.db / vault / ocr 组件）在 %LOCALAPPDATA%\pptx-finder，
;     不在 {app}，卸载不触碰。

#ifndef AppVersion
  #error 必须用 /DAppVersion=x.y.z 传版本号；请走 tools/build_installer.py
#endif

[Setup]
AppId={{B7E2A8F3-5C4D-4E1F-9A2B-3D4C5E6F7A08}
AppName=PPT Doctor
AppVersion={#AppVersion}
AppVerName=PPT Doctor {#AppVersion}
AppPublisher=TianLin
AppPublisherURL=https://github.com/TianLin0509/PPTutor
AppSupportURL=https://github.com/TianLin0509/PPTutor/issues
AppUpdatesURL=https://github.com/TianLin0509/PPTutor/releases
DefaultDirName={localappdata}\Programs\PPT-Doctor
DefaultGroupName=PPT Doctor
DisableProgramGroupPage=yes
OutputDir=..\artifacts
OutputBaseFilename=PPT-Doctor-Setup-v{#AppVersion}
SetupIconFile=..\assets\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName=PPT Doctor
UninstallDisplayIcon={app}\PPT-Doctor.exe
; 托盘常驻，覆盖前必须先请它退出，否则 exe/dll 被占用
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=TianLin
VersionInfoDescription=PPT Doctor 安装程序

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[InstallDelete]
; v1.5.2 入口改名（去掉空格）。Inno 不会动它不认识的旧文件，不清掉的话 {app} 里
; 会留一个旧的 PPT Doctor.exe —— 用户桌面上若有旧快捷方式，点开的就是那个旧壳。
Type: files; Name: "{app}\PPT Doctor.exe"

[Files]
Source: "..\dist\PPT-Doctor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\PPT Doctor"; Filename: "{app}\PPT-Doctor.exe"
Name: "{group}\卸载 PPT Doctor"; Filename: "{uninstallexe}"
Name: "{userdesktop}\PPT Doctor"; Filename: "{app}\PPT-Doctor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PPT-Doctor.exe"; Description: "立即启动 PPT Doctor（托盘常驻）"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; 自动更新会往 {app} 里写 Inno 不认识的新文件（新版新增的 dll、刷新的 manifest），
; 不清掉会留一个半空目录。只点名我们自己的载荷，**不能**对 {app} 用
; filesandordirs —— 用户若把安装目录选在一个已有内容的文件夹，那会连带删掉别人的东西。
;
; 实测（塞进两个「更新新增」文件再卸载）：_internal 下的会被清掉，**根目录下的
; 不会**。这是有意为之的取舍：宁可留一个孤儿文件，也不冒删掉用户自己东西的风险。
; PyInstaller onedir 的根目录只有 exe + manifest.json，两者都已点名，所以这个
; 边界只有在未来版本新增根级文件、且用户走过自动更新之后卸载时才会碰到。
Type: files; Name: "{app}\manifest.json"
Type: filesandordirs; Name: "{app}\_internal"
