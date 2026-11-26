#ifndef MyAppVersion
  #define MyAppVersion "3.1.0"
#endif

[Setup]
AppId={{D9C6E4A2-8F3B-4A9E-B1F2-9B83C745D109}
AppName=GD Quiet
AppVersion={#MyAppVersion}
AppVerName=GD Quiet {#MyAppVersion}
AppPublisher=GD Quiet
DefaultDirName={autopf}\GD Quiet
DefaultGroupName=GD Quiet
PrivilegesRequired=lowest
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
LicenseFile=TERMS.txt
SetupIconFile=..\app\assets\gdquiet.ico
UninstallDisplayIcon={app}\GDQuiet.exe
UninstallDisplayName=GD Quiet
WizardStyle=modern
WizardImageFile=..\app\assets\wizard_side.bmp
WizardSmallImageFile=..\app\assets\wizard_small.bmp
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=GDQuiet-Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\GDQuiet.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\GD Quiet"; Filename: "{app}\GDQuiet.exe"; IconFilename: "{app}\GDQuiet.exe"
Name: "{group}\Uninstall GD Quiet"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GD Quiet"; Filename: "{app}\GDQuiet.exe"; IconFilename: "{app}\GDQuiet.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\GDQuiet.exe"; Description: "{cm:LaunchProgram,GD Quiet}"; Flags: nowait postinstall skipifsilent
