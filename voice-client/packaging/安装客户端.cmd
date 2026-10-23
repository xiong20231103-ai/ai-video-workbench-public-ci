@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"
if errorlevel 1 (
  echo.
  echo 安装失败，请把上面的错误信息交给 Codex。
)
pause
