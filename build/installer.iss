; Inno Setup script: builds Playout-Setup.exe from build\dist\Playout
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "1.0.0"
#endif

[Setup]
AppId={{7B1F3C52-4E2A-4C1B-9A0E-5D3F6A1C2B90}
AppName=Playout
AppVersion={#AppVer}
AppPublisher=Playout
DefaultDirName={autopf}\Playout
DefaultGroupName=Playout
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

[Files]
Source: "dist\Playout\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
Name: "{userdocs}\Playout\media"

[Icons]
Name: "{group}\Playout"; Filename: "{app}\Playout.exe"
Name: "{group}\Playout media folder"; Filename: "{userdocs}\Playout\media"
Name: "{group}\Uninstall Playout"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Playout"; Filename: "{app}\Playout.exe"; Tasks: desktopicon

[Run]
; let NDI and the remote control page through Windows Firewall without a prompt
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Playout"" dir=in action=allow program=""{app}\Playout.exe"" enable=yes"; Flags: runhidden
Filename: "{app}\Playout.exe"; Description: "Start Playout now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Playout"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
