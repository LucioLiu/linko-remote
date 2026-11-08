@echo off
echo This allows Codex to control this Windows desktop. Close the window to stop.
choice /M "Allow control of this desktop now"
if errorlevel 2 exit /b
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Desktop.ps1" -AllowInput
pause
