@echo off
set "SCRIPT_DIR=%~dp0"
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%launch.ps1"
