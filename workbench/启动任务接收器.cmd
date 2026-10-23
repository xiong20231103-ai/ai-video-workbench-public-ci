@echo off
chcp 65001 >nul
set "PY=%LOCALAPPDATA%\TikHubOPS\venv_cuda\Scripts\python.exe"
if not exist "%PY%" (
  echo 未找到 OPS 运行环境。请先运行 AI视频生产线-OPS 的“安装Dashboard.cmd”。
  pause
  exit /b 1
)
cd /d "%~dp0"
"%PY%" task_receiver.py
