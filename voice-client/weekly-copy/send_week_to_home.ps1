# Send finished weekly copy to the home PC over Tailscale (Windows side of the Thursday job).
# The OpenClaw gateway runs in WSL without Tailscale or Windows interop, so run_weekly_copy.py only
# writes READY into the week's folder; this script, run by a hidden Windows scheduled task,
# keeps a local copy, sends the merged CSV and report, and writes SENT so a week is sent once.
# It never submits anything into home production.
param(
    [string]$RunsDir = '\\wsl.localhost\OpenClawGateway\home\openclaw\.openclaw\workspace\weekly-copy\runs',
    [string]$Peer = 'example-pc',
    [string]$LocalDir = (Join-Path $env:LOCALAPPDATA 'WeeklyCopy'),
    [string]$Tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $LocalDir | Out-Null
$log = Join-Path $LocalDir 'send.log'
function Write-Log([string]$message) {
    Add-Content -Path $log -Encoding UTF8 -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $message)
}

if (-not (Test-Path $RunsDir)) { Write-Log "runs dir not reachable: $RunsDir"; exit 1 }
$failed = $false
foreach ($dir in Get-ChildItem $RunsDir -Directory | Where-Object Name -match '^\d{4}-W\d{2}$') {
    $ready = Join-Path $dir.FullName 'READY'
    if (-not (Test-Path $ready) -or (Test-Path (Join-Path $dir.FullName 'SENT'))) { continue }
    $week = $dir.Name
    try {
        $report = Get-Content (Join-Path $dir.FullName "report-$week.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $report.accepted) { throw "report-$week.json is not accepted" }
        $keep = Join-Path $LocalDir $week
        New-Item -ItemType Directory -Force $keep | Out-Null
        $files = foreach ($name in "weekly-$week.csv", "report-$week.json") {
            Copy-Item (Join-Path $dir.FullName $name) (Join-Path $keep $name) -Force
            Join-Path $keep $name
        }
        & $Tailscale file cp @files "${Peer}:"
        if ($LASTEXITCODE -ne 0) { throw "tailscale file cp exit code $LASTEXITCODE" }
        $hashes = $files | ForEach-Object { "{0}  {1}" -f (Get-FileHash $_ -Algorithm SHA256).Hash, (Split-Path $_ -Leaf) }
        Set-Content -Path (Join-Path $dir.FullName 'SENT') -Encoding UTF8 -Value (@("sent to $Peer at $(Get-Date -Format s)") + $hashes)
        Write-Log "$week sent to $Peer ($($hashes -join '; '))"
    } catch {
        Write-Log "$week not sent: $_"
        $failed = $true
    }
}
if ($failed) { exit 1 }
