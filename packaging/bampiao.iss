; 半瓢 安装程序（Inno Setup 6）
;
; 设计取舍：
; - 装到 %LOCALAPPDATA%\Bampiao，PrivilegesRequired=lowest —— 普通用户双击就能装，
;   不会弹 UAC，也不会往 Program Files 里塞东西。
; - 自动把 {app} 加进用户 PATH，所以装完直接能用 `bampiao` 命令，
;   不需要文档里那套「复制路径 → 粘 $b」的仪式。
; - 卸载时把 PATH 里那一项删掉，不留垃圾。

#define MyAppName "半瓢"
#define MyAppVersion "0.2.65"
#define MyAppPublisher "鹤竹子"
#define MyAppExeName "bampiao-desktop.exe"
#define MyAppCli "bampiao.exe"

[Setup]
AppId={{6B2F4C1E-9D3A-4B77-8E51-2A7F0C4D9B33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Bampiao
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 不需要管理员：装到当前用户目录，PATH 也只改 HKCU
; 只做仅为我安装：这个工具的数据（~/.bampiao）和 PATH 都在 HKCU，
; 装到 Program Files 反而不一致，还会多一次没必要的选择。
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=半瓢-{#MyAppVersion}-安装程序
SetupIconFile=bampiao.ico
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=yes
; 在线更新：安装器由 launcher 用 /VERYSILENT + CREATE_NO_WINDOW + SW_HIDE 拉起（update.py）。
; 这里再关掉 Inno 自己的目录页/完成页/日志，双保险。
; 注意：WindowVisible / WindowShowCaption 在 Inno 6.7 已废弃（编译会警告且被忽略），
; 真正管用的是 /VERYSILENT + SW_HIDE，所以不写这两条。
DisableDirPage=yes
DisableFinishedPage=yes
SetupLogging=no
; 安装前先给一段"这是什么、有什么风险"，比装完再解释有意义
InfoBeforeFile=before-install.txt
; PATH 是环境变量，要通知系统刷新
ChangesEnvironment=yes
MinVersion=10.0
; 在线更新走 /SILENT：先关掉正在运行的半瓢，否则 exe 被占着覆盖不了
CloseApplications=force
RestartApplications=no

[Languages]
; 官方仓库里的简体中文（本机装的 6.7.3 没带，随包放在 packaging/ 下）。
; 文件必须是 UTF-8 带 BOM，否则 Inno 会按 ANSI 读成乱码。
Name: "zh"; MessagesFile: "ChineseSimplified.isl"

[Messages]
; 欢迎页被关掉了（信息都放在"安装前须知"那一页），这里只定制完成页
FinishedHeadingLabel=安装完成
; 2026-11-04 审计 P0：原来写"登录你的小号，就能出图了" —— 半瓢面板里没有输入框，
; 用户装完会盯着一个出不了图的窗口。改成真实的下一步。
FinishedLabel=装好了，还剩三步：%n%n  1. 双击「半瓢」→「账号」页点「＋」，在弹出的窗口里登录 ChatGPT%n  2. 回「上手」页点「交给所有 AI」，让 Claude / Codex / WorkBuddy 认识半瓢%n  3. 打开你平时用的 AI，说一句「用半瓢画一张水墨小猫」，图会出现在「任务」页%n%n半瓢自己不出图：你照常跟 AI 说话，它替你把话和账号交过来。%n%n弱水三千，我只取半瓢饮。
ClickFinish=完成(&F)

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动（挂到右下角托盘）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 源目录可以用 /DPyiDist="..\dist-024\bampiao" 覆盖。默认仍是 ..\dist\bampiao。
; 好处是能在不删旧 dist 的前提下打新包（旧的那份留着还能救急回滚）。
#ifndef PyiDist
#define PyiDist "..\dist\bampiao"
#endif
; ★ data 子目录绝不进安装包：开发机上跑隔离测试，会在打包源目录里留下假配置。
; 一旦被打包，用户升级后面板里找得到 config.toml，迁移就判定“已迁过”直接跳过，
; 账号和任务全变空（2026-11-04 用户真机实测；0.2.30 已发布才发现）。
; Excludes 里的 data 通配实测能连子目录一起排掉（2026-11-04 用最小 iss 验过）。
; 真正的保证在 tools/build_win.py：打包前发现源目录里有 data 就直接失败。
Source: "{#PyiDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "\data\*,\data\*\*,*.bampiao-shared-dir"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} 命令行"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoExit -Command ""Set-Location '{app}'; Write-Host '半瓢命令行就绪：输入 bampiao --help 看用法' -ForegroundColor Green"""; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\{#MyAppExeName}"

[Registry]
; 用户 PATH 加一项（只在还没有的时候加）
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))
; 勾了"开机自启"才写 Run 项
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Bampiao"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 装完刷一次图标缓存：反复重装时资源管理器会抱着旧图标不放，
; 用户看到的就是"换了图标还是老样子"（这次实测碰到）。
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden nowait; StatusMsg: "刷新图标缓存…"
Filename: "{app}\{#MyAppExeName}"; Description: "现在打开{#MyAppName}"; Flags: nowait postinstall skipifsilent
; 在线更新（静默安装）装完直接把半瓢重新打开，用户不用自己去找图标
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
; 半瓢开着时卸载，exe 和 _internal 被占着删不掉，卸载器还不报错（约 190MB 残留，评审实测）。
; 先让它自己退出（会顺带收掉账号浏览器）。
Filename: "{app}\{#MyAppCli}"; Parameters: "quit"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopBampiao"

[UninstallDelete]
; 2026-11-04：这里原来有一条 `Type: filesandordirs; Name: "{app}"`，把安装目录整棵删掉。
; 危险在于用户可以把安装路径改成 D:\软件 —— 卸载时会把那个目录里的别的东西一起带走。
; 现在改成在 [Code] 的 usPostUninstall 里判断：
;   目录里有我们的 exe → 是半瓢自己的目录，整棵清掉；
;   没有 → 说明是用户自选的目录，只删 {app}\data，别的不碰。
; 用户数据（账号、登录态、出的图、日志）就在 {app}\data 里，跟程序同一个文件夹。

[Code]
const
  EnvironmentKey = 'Environment';

var
  OwnInstallDir: Boolean;
  SharedFlag: String;

function InitializeUninstall(): Boolean;
var
  DataDir: String;
begin
  Result := True;
  // 先记住这个目录是不是半瓢自己的：{app} 里有我们的 exe 才算。
  // 用户可以把安装路径改成 D:\软件，那种目录卸载时不能整棵删。
  OwnInstallDir := FileExists(ExpandConstant('{app}\{#MyAppExeName}'))
                or FileExists(ExpandConstant('{app}\{#MyAppCli}'));
  DataDir := ExpandConstant('{app}\data');
  // 只在真有用户数据时才问一句。刚装完就卸的人不该被拦。
  // 静默卸载（/SILENT 或控制面板批量）不弹框，直接按"删"处理。
  if DirExists(DataDir) and not UninstallSilent() then
  begin
    if MsgBox('卸载会把账号登录信息和出过的图一起删掉：' + #13#10 +
              DataDir + #13#10#13#10 +
              '想留着的话，先把这个文件夹复制到别处，再回来点「是」。',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  R: Integer;
begin
  Result := '';
  // 覆盖升级时半瓢可能正在跑，尤其是用户在批量 bampiao gen 的时候：
  // 那种进程没有窗口、不响应 Restart Manager，CloseApplications=force 对它无效，
  // 于是在替换 bampiao.exe 时报「DeleteFile 失败；错误代码 5。拒绝访问。」
  // （2026-10-30 用户在真机上撞到）。先让它自己退：quit 会走 socket，
  // socket 找不到人时还会按安装目录把残留进程收掉。
  // 注意：这里只能用 // 注释。Pascal 的 { } 注释遇到 {app} 会提前闭合，编译报语法错。
  if FileExists(ExpandConstant('{app}\{#MyAppCli}')) then
  begin
    // 有单正在跑就别装：装上会 quit 掉正在出图的进程，那批图直接没了
    // （2026-10-30 实测：我在用户机器上升级，掐掉了他正在跑的 R101）。
    // 排队的单不算——队列是持久的，装完会自动继续。要强装就加 /FORCEUPGRADE。
    if Pos('/FORCEUPGRADE', Uppercase(GetCmdTail)) = 0 then
    begin
      Exec(ExpandConstant('{app}\{#MyAppCli}'), 'upgrade-guard', '', SW_HIDE,
           ewWaitUntilTerminated, R);
      // **只认退出码 3**（= 正在出图）。旧版本没有这个子命令，会返回 2，
      // 若把"非 0 都当阻塞"，0.2.6 的用户就永远升不上来了。
      if R = 3 then
      begin
        Result := '半瓢现在正在出图，为了不打断它，这次先不装了。' + #13#10 +
                  '等这几张跑完再点一次安装就行（不会丢任何东西）。' + #13#10 +
                  '确实要现在强装：给安装包加参数 /FORCEUPGRADE。';
        exit;
      end;
    end;
    Exec(ExpandConstant('{app}\{#MyAppCli}'), 'quit', '', SW_HIDE,
         ewWaitUntilTerminated, R);
    Sleep(1200);
  end;
end;

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  { 前后加分隔符，避免 "C:\Foo" 命中 "C:\Foobar" }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure RemoveFromPath(Param: string);
var
  OrigPath, Item: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
    exit;
  Item := ';' + Uppercase(Param) + ';';
  P := Pos(Item, ';' + Uppercase(OrigPath) + ';');
  if P = 0 then
    exit;
  { 从原串里精确摘掉这一段 }
  P := Pos(Uppercase(Param), Uppercase(OrigPath));
  if P > 0 then
  begin
    Delete(OrigPath, P, Length(Param));
    StringChangeEx(OrigPath, ';;', ';', True);
    if (Length(OrigPath) > 0) and (OrigPath[1] = ';') then
      Delete(OrigPath, 1, 1);
    if (Length(OrigPath) > 0) and (OrigPath[Length(OrigPath)] = ';') then
      Delete(OrigPath, Length(OrigPath), 1);
    RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath);
  end;
end;

{ WebView2 是 pywebview 的运行时。Win10 1803+ 一般自带，但没有的话窗口开不出来，
  所以装前提醒一句，别让用户装完打不开还以为装坏了。 }
function HasWebView2(): Boolean;
var
  v: String;
begin
  { 只看 Program Files (x86) 会漏掉用户级安装（Edge 自带的那个在 LOCALAPPDATA），
    于是用户明明有、向导一进来却弹框说没有。和程序内 platform/win.py 查同一组位置。 }
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v);
  if not Result then
    Result := DirExists(ExpandConstant('{localappdata}\Microsoft\EdgeWebView\Application'))
           or DirExists('C:\Program Files (x86)\Microsoft\EdgeWebView\Application')
           or DirExists('C:\Program Files\Microsoft\EdgeWebView\Application');
end;

procedure InitializeWizard();
begin
  if not HasWebView2() then
    MsgBox('没检测到 WebView2（Edge 的内核组件），半瓢的窗口需要它。' + #13#10 +
           '可以继续安装，装完先装上它再打开半瓢：' + #13#10 +
           'https://go.microsoft.com/fwlink/p/?LinkId=2124703', mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  App: String;
begin
  if CurStep = ssInstall then
  begin
    // 安装**之前**这个目录里已经有东西、而且没有我们的 exe → 是用户自己的目录
    // （典型场景：他把安装路径改成 D:\软件）。记个标记，卸载时据此只清自己的，
    // 绝不整棵删。2026-11-04 实测过：不加这个，卸载会把用户整个目录删光。
    App := ExpandConstant('{app}');
    if DirExists(App) and not FileExists(App + '\{#MyAppExeName}') then
    begin
      SharedFlag := App + '\.bampiao-shared-dir';
      SaveStringToFile(SharedFlag, '1', False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    App := ExpandConstant('{app}');
    // 分两种目录：
    //  - 装到空目录/新目录（绝大多数人）：整棵清，免得覆盖安装后剩下 13 个空壳；
    //  - 装进用户已有的目录（D:\软件 那种）：**只删 data 和我们自己的标记**，
    //    别的文件一个都不许碰。判据是安装时写的标记文件，不是"这里有没有我们的 exe"
    //    —— exe 卸载时当然还在，那个判据永远为真，等于没判。
    SharedFlag := App + '\.bampiao-shared-dir';
    if FileExists(SharedFlag) then
    begin
      DelTree(App + '\data', True, True, True);
      // 运行时生成的两个文件 Inno 不认识，卸载后会在用户目录里留垃圾
      DeleteFile(App + '\instance.lock');
      DeleteFile(App + '\instance.json');
      DeleteFile(SharedFlag);
    end
    else
      DelTree(App, True, True, True);
    RemoveFromPath(App);
  end;
end;
