@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
rem 一键运行全部测试。测试只使用临时目录，不会改动生产数据。
rem 双击运行；命令行加 --no-pause 则结束时不暂停。
cd /d "%~dp0"
set "FAILED="

set "WB_PY=%~dp0workbench\.venv\Scripts\python.exe"
set "RX_PY=%LOCALAPPDATA%\TikHubOPS\venv_cuda\Scripts\python.exe"
set "VS_PY=%~dp0video-stitcher\.venv\Scripts\python.exe"
set "NODE="
if exist "%~dp0voice-client\runtime\node.exe" set "NODE=%~dp0voice-client\runtime\node.exe"
if not defined NODE where node >nul 2>nul && set "NODE=node"

echo ===== [1/4] 工作台 workbench =====
if exist "%WB_PY%" (
  pushd workbench
  "%WB_PY%" -m unittest discover -p "test_*.py"
  if errorlevel 1 set "FAILED=!FAILED! 工作台"
  popd
) else (
  echo 未找到运行环境：%WB_PY%
  set "FAILED=!FAILED! 工作台[缺环境]"
)

echo.
echo ===== [2/4] 任务接收器 task_receiver =====
if exist "%RX_PY%" (
  pushd workbench
  "%RX_PY%" -m unittest test_task_receiver
  if errorlevel 1 set "FAILED=!FAILED! 任务接收器"
  popd
) else (
  echo 未找到 OPS 运行环境：%RX_PY%
  set "FAILED=!FAILED! 任务接收器[缺环境]"
)

echo.
echo ===== [3/4] 拼接工具 video-stitcher =====
if exist "%VS_PY%" (
  pushd video-stitcher
  "%VS_PY%" -m unittest discover -p "test_*.py"
  if errorlevel 1 set "FAILED=!FAILED! 拼接工具"
  popd
) else (
  echo 未找到运行环境：%VS_PY%
  set "FAILED=!FAILED! 拼接工具[缺环境]"
)

echo.
echo ===== [4/4] 语音客户端 voice-client =====
if defined NODE (
  pushd voice-client
  "%NODE%" --test
  if errorlevel 1 set "FAILED=!FAILED! 语音客户端"
  popd
) else (
  echo 未找到 Node.js
  set "FAILED=!FAILED! 语音客户端[缺环境]"
)

echo.
echo ==========================================
if defined FAILED (
  echo 未通过：!FAILED!
  set "CODE=1"
) else (
  echo 全部测试通过。
  set "CODE=0"
)
echo ==========================================
if /i not "%~1"=="--no-pause" pause
exit /b %CODE%
