@echo off
setlocal
set "FIREFOX=C:\Program Files\Mozilla Firefox\firefox.exe"
set "EXTENSION=%~dp0fish-audio-extension"

if not exist "%FIREFOX%" (
  set "FIREFOX=C:\Program Files (x86)\Mozilla Firefox\firefox.exe"
)

if not exist "%FIREFOX%" (
  echo Mozilla Firefox was not found.
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
start "" "%FIREFOX%" "about:debugging#/runtime/this-firefox"
echo.
echo 1. Click Load Temporary Add-on
echo 2. Select manifest.json in this folder:
echo %EXTENSION%
echo 3. Firefox removes temporary add-ons after restart.
echo.
exit /b 0
