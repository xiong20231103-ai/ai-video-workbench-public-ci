@echo off
chcp 65001 >nul
if exist "%~dp0AI视频任务投递器.exe" (
  start "" "%~dp0AI视频任务投递器.exe"
) else (
  start "" pyw.exe "%~dp0company_task_submitter.pyw"
)
