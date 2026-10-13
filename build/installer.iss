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
UninstallDisplayIcon={app}\Playout.exe
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
Source: "cgcomposer.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; shortcuts from before the name changed to Endora Playout
Type: files; Name: "{group}\Playout.lnk"
Type: files; Name: "{group}\Playout media folder.lnk"
Type: files; Name: "{group}\Uninstall Playout.lnk"
Type: files; Name: "{autodesktop}\Playout.lnk"
Type: files; Name: "{commonstartup}\Playout.lnk"

[Dirs]
Name: "{userdocs}\Playout\media"

[Icons]
Name: "{group}\Endora Playout"; Filename: "{app}\Playout.exe"
Name: "{group}\CG Composer"; Filename: "{app}\Playout.exe"; Parameters: "--composer"; IconFilename: "{app}\cgcomposer.ico"
Name: "{group}\Endora Playout media folder"; Filename: "{userdocs}\Playout\media"
Name: "{group}\Uninstall Endora Playout"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Endora Playout"; Filename: "{app}\Playout.exe"; Tasks: desktopicon
Name: "{autodesktop}\CG Composer"; Filename: "{app}\Playout.exe"; Parameters: "--composer"; IconFilename: "{app}\cgcomposer.ico"; Tasks: desktopicon
Name: "{commonstartup}\Endora Playout"; Filename: "{app}\Playout.exe"; Tasks: autostart

[Run]
; let NDI and the remote control page through Windows Firewall without a prompt
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Playout"" dir=in action=allow program=""{app}\Playout.exe"" enable=yes"; Flags: runhidden
Filename: "{app}\Playout.exe"; Description: "Start Endora Playout now"; Flags: nowait postinstall skipifsilent
; after an update from inside Playout (setup started with /AUTOSTART): start Playout again
Filename: "{app}\Playout.exe"; Flags: nowait runasoriginaluser; Check: AutoStart

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden; RunOnceId: "DelFirewallRule"

[Code]
function AutoStart: Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/AUTOSTART') = 0 then
      Result := True;
end;
