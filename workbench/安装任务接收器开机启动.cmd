@echo off
chcp 65001 >nul
setlocal
set "RECEIVER_VBS=%~dp0启动任务接收器_后台.vbs"
if not exist "%RECEIVER_VBS%" (
  echo 找不到后台启动文件：%RECEIVER_VBS%
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:RECEIVER_VBS; $startup=[Environment]::GetFolderPath('Startup'); $shell=New-Object -ComObject WScript.Shell; $shortcut=$shell.CreateShortcut((Join-Path $startup 'AI视频生产线任务接收器.lnk')); $shortcut.TargetPath=($env:WINDIR + '\System32\wscript.exe'); $shortcut.Arguments=('"' + $target + '" --login-startup'); $shortcut.WorkingDirectory=(Split-Path -Parent $target); $shortcut.Description='AI视频生产线任务接收器（登录后后台启动）'; $shortcut.Save()"
if errorlevel 1 (
  echo 创建开机启动失败。
  pause
  exit /b 1
)
echo 已创建：登录 Windows 后后台启动“AI视频任务接收器”。
echo 同时自动打开工作台；无任务时待命，有任务按现有恢复规则继续，人工暂停仍有效。
pause
