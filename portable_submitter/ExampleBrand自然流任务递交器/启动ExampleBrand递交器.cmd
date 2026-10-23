@echo off
setlocal
set "ROOT=%~dp0"
set "APP="
for /r "%ROOT%" %%F in (ExampleBrand-Natural-Flow-Task-Submitter.exe) do if exist "%%~fF" if not defined APP set "APP=%%~fF"
if not defined APP goto :missing
start "" "%APP%"
exit /b 0
:missing
echo Package incomplete: ExampleBrand submitter EXE was not found.
echo Extract the complete ZIP again.
pause
exit /b 2
