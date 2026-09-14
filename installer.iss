#define MyAppName "AirPrint Bridge"
#define MyAppVersion "1.3.1"
#define MyAppPublisher "Salman Asmat"
#define MyAppExeName "AirPrintBridge.exe"
#define MyAppDir "C:\Program Files\AirPrintBridge"

[Setup]
AppId={{D370A1A2-5678-4321-ABCD-B1C2A3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyAppDir}
OutputDir=.\Release
OutputBaseFilename=AirPrintBridge_Setup_v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
CloseApplicationsFilter=*AirPrintBridge*

[Files]
Source: "dist\AirPrintBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--startup auto install"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "start"; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "stop"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "remove"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: files; Name: "{app}\airprint_bridge.log"

[Code]
procedure StopAndRemoveExistingService();
var
  ResultCode: Integer;
begin
  // Stop existing Windows service if running
  Exec('sc.exe', 'stop AirPrintBridge', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Forcefully terminate any running AirPrintBridge.exe processes
  Exec('taskkill.exe', '/F /IM AirPrintBridge.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Remove existing service registration
  Exec('sc.exe', 'delete AirPrintBridge', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function GetUninstallString(): String;
var
  sUninstPath: String;
  sAppGuid: String;
begin
  sUninstPath := '';
  sAppGuid := '{D370A1A2-5678-4321-ABCD-B1C2A3D4E5F6}_is1';
  
  if not RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + sAppGuid, 'UninstallString', sUninstPath) then
    if not RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + sAppGuid, 'UninstallString', sUninstPath) then
      RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + sAppGuid, 'UninstallString', sUninstPath);
      
  Result := sUninstPath;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  sUninstPath: String;
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // Forcefully stop running processes and services first
    StopAndRemoveExistingService();
    
    // Check if a previously installed version exists and uninstall it silently
    sUninstPath := GetUninstallString();
    if sUninstPath <> '' then
    begin
      sUninstPath := RemoveQuotes(sUninstPath);
      if FileExists(sUninstPath) then
      begin
        Exec(sUninstPath, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
    
    // Final check: make sure service & process are completely cleaned up
    StopAndRemoveExistingService();
  end;
end;
