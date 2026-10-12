#ifndef AppSourceDir
  #define AppSourceDir "..\dist\app"
#endif

[Setup]
AppId=DatabaseOfflineLineage
AppName=Database & Offline Lineage
AppVersion=1.0.1
DefaultDirName={localappdata}\Programs\DatabaseOfflineLineage
DefaultGroupName=Database & Offline Lineage
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=DatabaseOfflineLineage-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\DatabaseOfflineLineage.exe

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"

[Files]
Source: "{#AppSourceDir}\DatabaseOfflineLineage.exe"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\uninstall.ps1"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\DatabaseOfflineLineage"; Flags: deletekey

[Icons]
Name: "{group}\Database & Offline Lineage"; Filename: "{app}\DatabaseOfflineLineage.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall Database & Offline Lineage"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Database & Offline Lineage"; Filename: "{app}\DatabaseOfflineLineage.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\DatabaseOfflineLineage.exe"; Description: "Open Database & Offline Lineage"; Flags: nowait postinstall skipifsilent