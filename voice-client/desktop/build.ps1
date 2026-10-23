$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

if (-not (Test-Path -LiteralPath $compiler)) {
  $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}

& $compiler /nologo /target:winexe /optimize+ /utf8output /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /out:"$projectRoot\BatchVoiceClient.exe" "$PSScriptRoot\BatchVoiceClient.cs"

if ($LASTEXITCODE -ne 0) {
  throw "Desktop client build failed with exit code $LASTEXITCODE"
}

Write-Output "Built: $projectRoot\BatchVoiceClient.exe"
