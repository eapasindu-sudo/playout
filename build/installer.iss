; Inno Setup script: builds Playout-Setup.exe from build\dist\Playout
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "1.0.0"
#endif

[Setup]
AppId={{7B1F3C52-4E2A-4C1B-9A0E-5D3F6A1C2B90}
AppName=Endora Playout
AppVersion={#AppVer}
AppPublisher=Endora
DefaultDirName={autopf}\Playout
DefaultGroupName=Endora Playout
OutputDir=output
OutputBaseFilename=Playout-Setup
SetupIconFile=playout.ico
WizardImageFile=wizard.bmp
WizardSmallImageFile=wizard-small.bmp
UninstallDisplayIcon={app}\endora-playout.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Endora Playout when Windows starts (recommended for a playout PC)"; GroupDescription: "Start:"; Flags: unchecked

[Files]
Source: "dist\Playout\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "endora-playout.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "endora-cgcomposer.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "update.ps1"; DestDir: "{app}"; Flags: ignoreversion
; Built-in browser for web overlays (cefcg.exe + Chromium Embedded; CI builds it into build\cef)
Source: "cef\*"; DestDir: "{app}\cef"; Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist
; ATEM Switcher + Sony camera control (CI puts it in build\atem from the atem-web-switcher release)
Source: "atem\*"; DestDir: "{app}\atem"; Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist

[InstallDelete]
Type: files; Name: "{app}\cgcomposer.ico"
; shortcuts from before the name changed to Endora Playout
Type: files; Name: "{group}\Playout.lnk"
Type: files; Name: "{group}\Playout media folder.lnk"
Type: files; Name: "{group}\Uninstall Playout.lnk"
Type: files; Name: "{autodesktop}\Playout.lnk"
Type: files; Name: "{commonstartup}\Playout.lnk"

[Dirs]
Name: "{userdocs}\Playout\media"

[Icons]
Name: "{group}\Endora Playout"; Filename: "{app}\Playout.exe"; IconFilename: "{app}\endora-playout.ico"
Name: "{group}\CG Composer"; Filename: "{app}\Playout.exe"; Parameters: "--composer"; IconFilename: "{app}\endora-cgcomposer.ico"
Name: "{group}\ATEM & Cameras"; Filename: "{app}\Playout.exe"; Parameters: "--atem"; IconFilename: "{app}\endora-playout.ico"
Name: "{group}\Endora Playout media folder"; Filename: "{userdocs}\Playout\media"
Name: "{group}\Uninstall Endora Playout"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Endora Playout"; Filename: "{app}\Playout.exe"; IconFilename: "{app}\endora-playout.ico"; Tasks: desktopicon
Name: "{autodesktop}\CG Composer"; Filename: "{app}\Playout.exe"; Parameters: "--composer"; IconFilename: "{app}\endora-cgcomposer.ico"; Tasks: desktopicon
Name: "{commonstartup}\Endora Playout"; Filename: "{app}\Playout.exe"; IconFilename: "{app}\endora-playout.ico"; Tasks: autostart

[Run]
; let NDI and the remote control page through Windows Firewall without a prompt
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Playout"" dir=in action=allow program=""{app}\Playout.exe"" enable=yes"; Flags: runhidden
; phones / tablets reach the ATEM switcher page (port 3000); Sony cameras answer the camera search
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout ATEM"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Playout ATEM"" dir=in action=allow program=""{app}\atem\ATEM-Switcher.exe"" enable=yes profile=any"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout Sony"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Playout Sony"" dir=in action=allow program=""{app}\atem\sony\sony-bridge.exe"" enable=yes profile=any"; Flags: runhidden
; the Sony SDK needs the Microsoft Visual C++ runtime: fetched once if this PC does not have it
Filename: "{sys}\curl.exe"; Parameters: "-sSfL -o ""{tmp}\vc_redist.x64.exe"" https://aka.ms/vs/17/release/vc_redist.x64.exe"; StatusMsg: "Downloading the Visual C++ runtime for Sony camera control..."; Flags: runhidden; Check: NeedVCRuntime
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Visual C++ runtime..."; Flags: skipifdoesntexist; Check: NeedVCRuntime
Filename: "{app}\Playout.exe"; Description: "Start Endora Playout now"; Flags: nowait postinstall skipifsilent
; after an update from inside Playout (setup started with /AUTOSTART): start Playout again
Filename: "{app}\Playout.exe"; Flags: nowait runasoriginaluser; Check: AutoStart

; Windows keeps old icons in a cache: refresh it so the new Endora logos show at once
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden skipifdoesntexist

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout ATEM"""; Flags: runhidden; RunOnceId: "DelFirewallAtem"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout Sony"""; Flags: runhidden; RunOnceId: "DelFirewallSony"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""Get-Process ATEM-Switcher,sony-bridge -EA 0 | ? {{ $_.Path -like '{app}\atem\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "StopAtem"

[Code]
function NeedVCRuntime: Boolean;
var
  installed: Cardinal;
begin
  Result := FileExists(ExpandConstant('{app}\atem\sony\sony-bridge.exe')) and
    not (RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64', 'Installed', installed) and (installed = 1));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  code: Integer;
begin
  { the ATEM Switcher that came with Playout is running: stop it so its files can be replaced
    (a separately installed ATEM Switcher keeps running) }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "Get-Process ATEM-Switcher,sony-bridge -EA 0 | ? { $_.Path -like ''' + ExpandConstant('{app}') + '\atem\*'' } | Stop-Process -Force"',
    '', SW_HIDE, ewWaitUntilTerminated, code);
  { an update started from inside Playout: Playout closes itself a moment after starting setup.
    Wait for it (up to 20 s), then stop whatever still runs from the Playout folder (its browser
    overlays, ffmpeg), so the files-in-use check finds nothing and setup does not cancel itself }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "$d = ''' + ExpandConstant('{app}') + '\''; ' +
    '$m = { Get-Process | ? { $_.Path -and $_.Path.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) } }; ' +
    'for ($i = 0; $i -lt 40 -and (Get-Process Playout -EA 0 | ? { $_.Path -like ($d + ''*'') }); $i++) { Start-Sleep -Milliseconds 500 }; ' +
    '& $m | Stop-Process -Force; Start-Sleep -Seconds 1"',
    '', SW_HIDE, ewWaitUntilTerminated, code);
  Result := '';
end;

function AutoStart: Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/AUTOSTART') = 0 then
      Result := True;
end;
