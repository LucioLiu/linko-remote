@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Pair-Device.ps1" -PublicKeyPath "%~dp0LanPilot-key.xml"
if errorlevel 1 pause
