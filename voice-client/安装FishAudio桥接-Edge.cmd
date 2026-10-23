@echo off
setlocal
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "EXTENSION=%~dp0fish-audio-extension"

if not exist "%EDGE%" (
  set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)

if not exist "%EDGE%" (
  echo Microsoft Edge was not found.
  pause
  exit /b 1
)

if not exist "%EXTENSION%\manifest.json" (
  echo Fish Audio extension files are missing:
  echo %EXTENSION%
  pause
  exit /b 1
)

start "" explorer.exe "%EXTENSION%"
start "" "%EDGE%" "edge://extensions/"
echo.
echo 1. Turn on Developer mode in edge://extensions/
echo 2. Click Load unpacked
echo 3. Select this folder:
echo %EXTENSION%
echo.
exit /b 0
