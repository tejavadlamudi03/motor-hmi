; Inno Setup 6 script - per-machine install into Program Files (write-protected
; for standard users, so operators cannot alter the executable).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6C0B5E0E-5A57-4F5E-9C1E-3B2B7F0D9A11}
AppName=Motor HMI
AppVersion={#AppVersion}
AppPublisher=Your Company
DefaultDirName={autopf}\Motor HMI
DefaultGroupName=Motor HMI
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=MotorHMI-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\dist\MotorHMI\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Motor HMI"; Filename: "{app}\MotorHMI.exe"
Name: "{autodesktop}\Motor HMI"; Filename: "{app}\MotorHMI.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\MotorHMI.exe"; Description: "Start Motor HMI"; Flags: nowait postinstall skipifsilent
