@echo off
setlocal
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "EXTENSION=%~dp0fish-audio-extension"

if not exist "%CHROME%" (
  echo Google Chrome was not found:
  echo %CHROME%
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
start "" "%CHROME%" "chrome://extensions/"
echo.
echo 1. Turn on Developer mode in chrome://extensions/
echo 2. Click Load unpacked
echo 3. Select this folder:
echo %EXTENSION%
echo 4. For Edge or Firefox, use the browser-specific installer.
echo.
exit /b 0
