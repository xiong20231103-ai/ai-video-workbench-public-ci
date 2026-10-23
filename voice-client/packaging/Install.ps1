$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$required = @(
  'BatchVoiceClient.exe',
  'runtime\node.exe',
  '.browsers\chrome-win64\chrome.exe',
  'node_modules\playwright\package.json',
  'src\runBatch.js',
  'data\tasks.csv'
)

$missing = @()
foreach ($relativePath in $required) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $relativePath))) {
    $missing += $relativePath
  }
}

if ($missing.Count -gt 0) {
  Write-Host '安装包不完整，缺少以下文件：' -ForegroundColor Red
  $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
  throw '请重新完整解压安装包，不要只复制 EXE。'
}

if (-not (Test-Path -LiteralPath (Join-Path $root '.env'))) {
  Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination (Join-Path $root '.env')
}

New-Item -ItemType Directory -Path (Join-Path $root 'downloads') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $root 'logs') -Force | Out-Null

Get-Item -LiteralPath (Join-Path $root 'BatchVoiceClient.exe'), (Join-Path $root 'runtime\node.exe') | Unblock-File -ErrorAction SilentlyContinue

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop '批量语音客户端.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $root 'BatchVoiceClient.exe'
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Playwright 批量语音生成客户端'
$shortcut.Save()

Write-Host ''
Write-Host '批量语音客户端安装完成。' -ForegroundColor Green
Write-Host "安装目录：$root"
Write-Host "桌面快捷方式：$shortcutPath"
Write-Host ''
Write-Host '正在启动客户端...'
Start-Process -FilePath (Join-Path $root 'BatchVoiceClient.exe') -WorkingDirectory $root
