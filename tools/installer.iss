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
Name: "cleanlegacy"; Description: "清理电脑上的旧版本（只删程序文件，索引和版本库不动）"; GroupDescription: "附加任务："

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

[Code]
{
  清理旧版本。

  能做到什么、做不到什么，先说清楚：
  v1.5.3 及更早**只发 zip**，用户把它解压到哪都有可能，注册表里没有任何记录。
  所以「找出电脑上所有旧版本」只能靠全盘搜索——慢、而且会误伤同名目录。这里
  改用「顺着用户自己留下的线索找」：开机自启快捷方式、桌面和开始菜单的快捷方式。
  这几处正好覆盖了真正会造成困扰的那些副本（尤其是自启那份：老版本每次开机把
  自启快捷方式改指向自己，用户于是一直在用旧版还不自知）。

  删除只动**能正面认出来的载荷**：
      <目录>\_internal\        （PyInstaller onedir 的运行时目录）
      <目录>\PPTutor.exe       （0.8~0.9.1 的老名字）
      <目录>\PPT Doctor.exe    （旧名）
      <目录>\PPT-Doctor.exe    （新名）
      <目录>\manifest.json
  认定条件是同时存在 _internal\base_library.zip 和上述某个 exe —— 这个签名基本
  不可能撞上别的东西。目录本身只用 RemoveDir（空了才删）。
  所以哪怕用户当初把 zip 直接解压在「下载」根目录，也只会删掉那几个程序文件，
  他自己的东西一件不动。

  绝不触碰 %LOCALAPPDATA%\pptx-finder —— 索引库和版本库在那里。
}

const
  { 历来发过的三个可执行文件名。最早叫 PPTutor（0.8~0.9.1），之后是带空格的
    PPT Doctor.exe，v1.5.2 起去掉空格。真正的「老版本」多半是第一个——
    漏了它，这个清理功能就正好对最该清的那批不起作用。 }
  EXE_PPTUTOR = 'PPTutor.exe';
  EXE_LEGACY = 'PPT Doctor.exe';
  EXE_CURRENT = 'PPT-Doctor.exe';
  STARTUP_LNK = 'pptx-finder.lnk';

var
  CleanedDirs: TStringList;

function ShortcutTarget(const LnkPath: string): string;
var
  Shell, Lnk: Variant;
begin
  Result := '';
  if not FileExists(LnkPath) then
    Exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Lnk := Shell.CreateShortcut(LnkPath);
    Result := Lnk.TargetPath;
  except
    Result := '';
  end;
end;

procedure PointShortcutAt(const LnkPath, ExePath: string);
var
  Shell, Lnk: Variant;
begin
  try
    Shell := CreateOleObject('WScript.Shell');
    Lnk := Shell.CreateShortcut(LnkPath);
    Lnk.TargetPath := ExePath;
    Lnk.WorkingDirectory := ExtractFileDir(ExePath);
    Lnk.Save;
  except
  end;
end;

{ 是不是一个 PPT Doctor 的解压/安装目录？要求签名同时成立，避免误伤 }
function LooksLikeInstallDir(const Dir: string): Boolean;
begin
  Result := (Dir <> '')
    and FileExists(AddBackslash(Dir) + '_internal\base_library.zip')
    and (FileExists(AddBackslash(Dir) + EXE_PPTUTOR)
      or FileExists(AddBackslash(Dir) + EXE_LEGACY)
      or FileExists(AddBackslash(Dir) + EXE_CURRENT));
end;

function SameDir(const A, B: string): Boolean;
begin
  Result := CompareText(RemoveBackslash(A), RemoveBackslash(B)) = 0;
end;

{ 只删能正面认出来的载荷；目录本身空了才删 }
procedure RemoveLegacyPayload(const Dir: string);
begin
  if not LooksLikeInstallDir(Dir) then
    Exit;
  if SameDir(Dir, ExpandConstant('{app}')) then
    Exit;
  if CleanedDirs.IndexOf(LowerCase(RemoveBackslash(Dir))) >= 0 then
    Exit;
  CleanedDirs.Add(LowerCase(RemoveBackslash(Dir)));

  Log('cleanup legacy install: ' + Dir);
  DelTree(AddBackslash(Dir) + '_internal', True, True, True);
  DeleteFile(AddBackslash(Dir) + EXE_PPTUTOR);
  DeleteFile(AddBackslash(Dir) + EXE_LEGACY);
  DeleteFile(AddBackslash(Dir) + EXE_CURRENT);
  DeleteFile(AddBackslash(Dir) + 'manifest.json');
  { 空了才删；用户若把 zip 解压在「下载」根目录，这里会失败，正是我们要的 }
  RemoveDir(Dir);
end;

procedure HandleShortcut(const LnkPath: string; RepointIfStale: Boolean);
var
  Target, NewExe: string;
begin
  Target := ShortcutTarget(LnkPath);
  if Target = '' then
    Exit;
  NewExe := AddBackslash(ExpandConstant('{app}')) + EXE_CURRENT;
  if CompareText(Target, NewExe) = 0 then
    Exit;
  if not LooksLikeInstallDir(ExtractFileDir(Target)) then
    Exit;

  if RepointIfStale then
  begin
    { 开机自启：改指向新版，别删。删了的话用户不手动开一次应用，自启就没了 }
    Log('repoint startup shortcut: ' + LnkPath);
    PointShortcutAt(LnkPath, NewExe);
  end
  else
  begin
    Log('drop stale shortcut: ' + LnkPath);
    DeleteFile(LnkPath);
  end;
  RemoveLegacyPayload(ExtractFileDir(Target));
end;

procedure SweepShortcutFolder(const Folder: string);
var
  FR: TFindRec;
begin
  if FindFirst(AddBackslash(Folder) + '*.lnk', FR) then
  try
    repeat
      HandleShortcut(AddBackslash(Folder) + FR.Name, False);
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

procedure CleanupLegacyInstalls;
begin
  CleanedDirs := TStringList.Create;
  try
    { 1) 开机自启：最要紧的一处。老版本每次开机会把它改指向自己，
      用户于是一直在用旧版还不自知。 }
    HandleShortcut(AddBackslash(ExpandConstant('{userstartup}')) + STARTUP_LNK, True);
    { 2) 桌面 / 开始菜单里指向旧副本的快捷方式 }
    SweepShortcutFolder(ExpandConstant('{userdesktop}'));
    SweepShortcutFolder(ExpandConstant('{userprograms}'));
    SweepShortcutFolder(AddBackslash(ExpandConstant('{userprograms}')) + 'PPT Doctor');
  finally
    CleanedDirs.Free;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { 必须在新文件装完之后跑：要拿安装目录当「不许删」的排除项，
    也要把自启快捷方式指向新装好的 exe。
    注意：Inno 的注释是花括号，正文里不能出现右花括号（写 app 常量会提前闭合注释）。 }
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('cleanlegacy') then
    CleanupLegacyInstalls;
end;
