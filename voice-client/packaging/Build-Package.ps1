$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot 'release'
$zipFileName = 'BatchVoiceClient-FishAudio-CustomFolders-Final-20260715.zip'
$zipPath = Join-Path $releaseDir $zipFileName
$rootName = 'BatchVoiceClient-Portable'

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
if (Test-Path -LiteralPath $zipPath) {
  Remove-Item -LiteralPath $zipPath -Force
}

$stream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew)
$archive = New-Object System.IO.Compression.ZipArchive($stream, [System.IO.Compression.ZipArchiveMode]::Create)
$fileCount = 0
$totalBytes = [int64]0

function Add-PackageFile {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$RelativePath
  )

  $entryName = ($rootName + '/' + $RelativePath).Replace('\', '/')
  $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
  $sourceStream = [System.IO.File]::OpenRead($Source)
  $entryStream = $entry.Open()
  try {
    $sourceStream.CopyTo($entryStream)
  } finally {
    $entryStream.Dispose()
    $sourceStream.Dispose()
  }
  $script:fileCount++
  $script:totalBytes += (Get-Item -LiteralPath $Source).Length

  if (($script:fileCount % 500) -eq 0) {
    Write-Output "已加入 $script:fileCount 个文件..."
  }
}

function Add-PackageDirectory {
  param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$TargetDirectory
  )

  $base = (Get-Item -LiteralPath $SourceDirectory).FullName
  Get-ChildItem -LiteralPath $base -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($base.Length).TrimStart('\')
    $excluded = ($TargetDirectory -eq 'data' -and $relative -like 'chrome-profile\*') -or
      ($TargetDirectory -eq 'data' -and $relative -eq 'project_output_dirs.csv') -or
      ($relative -like '.selftest*\*')
    if (-not $excluded) {
      Add-PackageFile -Source $_.FullName -RelativePath (Join-Path $TargetDirectory $relative)
    }
  }
}

try {
  $rootFiles = @(
    '.env.example',
    '.gitignore',
    'BatchVoiceClient.exe',
    'package.json',
    'package-lock.json'
  )
  foreach ($file in $rootFiles) {
    Add-PackageFile -Source (Join-Path $projectRoot $file) -RelativePath $file
  }
  Get-ChildItem -LiteralPath $projectRoot -File -Filter '*.md' | ForEach-Object {
    Add-PackageFile -Source $_.FullName -RelativePath $_.Name
  }

  foreach ($directory in @('src', 'public', 'data', 'desktop', 'packaging', 'test', 'fish-audio-extension', 'node_modules', '.browsers')) {
    Write-Output "正在加入 $directory ..."
    Add-PackageDirectory -SourceDirectory (Join-Path $projectRoot $directory) -TargetDirectory $directory
  }
  Add-PackageFile -Source (Join-Path $projectRoot 'data\project_output_dirs.example.csv') -RelativePath 'data\project_output_dirs.csv'

  Add-PackageFile -Source (Join-Path $projectRoot 'runtime\node.exe') -RelativePath 'runtime\node.exe'
  Get-ChildItem -LiteralPath $PSScriptRoot -File | Where-Object { $_.Name -ne 'Build-Package.ps1' } | ForEach-Object {
    Add-PackageFile -Source $_.FullName -RelativePath $_.Name
  }
  Get-ChildItem -LiteralPath $projectRoot -File -Filter '*.cmd' | ForEach-Object {
    Add-PackageFile -Source $_.FullName -RelativePath $_.Name
  }
  Get-ChildItem -LiteralPath $projectRoot -File -Filter '*.ps1' | ForEach-Object {
    Add-PackageFile -Source $_.FullName -RelativePath $_.Name
  }

  $archive.CreateEntry($rootName + '/downloads/') | Out-Null
  $archive.CreateEntry($rootName + '/logs/') | Out-Null
} finally {
  $archive.Dispose()
  $stream.Dispose()
}

$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
$hashFile = Join-Path $releaseDir 'BatchVoiceClient-FishAudio-CustomFolders-Final-20260715.sha256.txt'
Set-Content -LiteralPath $hashFile -Value "$hash  $zipFileName" -Encoding ASCII

$zip = Get-Item -LiteralPath $zipPath
Write-Output "打包完成：$zipPath"
Write-Output "文件数量：$fileCount"
Write-Output ("源文件总大小：{0:N1} MB" -f ($totalBytes / 1MB))
Write-Output ("ZIP 大小：{0:N1} MB" -f ($zip.Length / 1MB))
Write-Output "SHA256：$hash"
