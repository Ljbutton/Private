; Inno Setup script for The Edge.
;
; The reason this exists is Mark of the Web. Windows tags anything downloaded
; from the internet, Explorer copies that tag onto every file it extracts from
; a zip, and .NET refuses to load a tagged assembly -- so pywebview could not
; reach WebView2 through pythonnet, and the app fell back to a browser tab.
; Unblocking the files by hand fixes it, but asking someone to run a PowerShell
; command before their app will open is not shipping software.
;
; An installer moves the problem somewhere it does not exist. The tag lands on
; the downloaded setup.exe, and nothing reads that tag when it runs. The files
; the installer then writes are written by the installer process, so they carry
; no tag at all -- the same reason the old one-file build was never affected,
; except paid once at install rather than on every launch.
;
; Installs per user, into LocalAppData, deliberately: a machine-wide install
; needs administrator rights, and an unsigned installer asking for them is both
; a worse prompt and a worse idea.

#define AppName "The Edge"
#define AppExe "TheEdge.exe"
#define AppPublisher "The Edge"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\TheEdge"
#endif

[Setup]
AppId={{8E1F5A42-6C3B-4D9E-9A77-2B6F0C1D4E88}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install, so no UAC prompt and no administrator requirement.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\TheEdge
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; The program is one folder the user never needs to open, so offering to put it
; somewhere else is a question with no good answer for anybody.
DisableDirPage=yes
OutputBaseFilename=TheEdge-windows-setup
OutputDir=.
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The data directory lives in LocalAppData\TheEdge and is not ours to remove:
; it holds the picks, the database and the backups. Uninstalling removes the
; program only, which is the same promise the README makes about updating.
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\assets\icon.ico
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole one-folder build, including _internal. Recursive and verbatim --
; PyInstaller decides this layout and the exe will not start if it is altered.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; \
  Flags: nowait postinstall skipifsilent
