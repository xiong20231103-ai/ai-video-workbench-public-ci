$ErrorActionPreference = 'Stop'
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $appDir 'workbench.py'
$pythonw = 'E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\.cuda_env\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\pythonw.exe'
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('Python runtime was not found.', 'Launch failed', 'OK', 'Error') | Out-Null
    exit 1
}

# The portable CUDA runtime includes tkinter and is the runtime used by the
# Step 1 bridge.  Start it hidden so the workbench remains a normal desktop app.
Start-Process -FilePath $pythonw -ArgumentList @($script) -WorkingDirectory $appDir -WindowStyle Hidden
