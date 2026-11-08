@echo off
setlocal
set "desktopPairPath=%LOCALAPPDATA%\CodexRemoteDesktop\relay-pairing.bin"
if not exist "%desktopPairPath%" (
  echo Pairing file missing. Complete the documented pairing step first.
  exit /b 2
)
if /I "%~1"=="control" goto control
if not "%~1"=="" if /I not "%~1"=="readonly" (
  echo Usage: Start-Relay.cmd [readonly^|control]
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Desktop.ps1" -RelayConfig "%desktopPairPath%"
exit /b %errorlevel%
:control
echo Control mode: this Windows desktop accepts mouse and keyboard input.
echo Close this window to stop. Do not operate the desktop concurrently.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Desktop.ps1" -RelayConfig "%desktopPairPath%" -AllowInput -TargetGuard
exit /b %errorlevel%
