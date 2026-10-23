@echo off
setlocal

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "FISH_URL=https://fish.audio/zh-CN/app/text-to-speech/"

if not exist "%CHROME%" (
  echo Google Chrome was not found:
  echo %CHROME%
  pause
  exit /b 1
)

start "" "%CHROME%" "%FISH_URL%"
exit /b 0
