# Home PC: register a hidden scheduled task that imports the weekly copy sent by the
# company PC (Taildrop -> Downloads) into voice-client\data\weekly every morning (a missed run
# starts as soon as the PC is on; an at-logon trigger needs admin rights, so none is used).
# The voice client switches to the current ISO week's imported file at the start of each batch.
param(
    [string]$Inbox = 'E:\Backup\Downloads',
    [string]$Python = (Join-Path $PSScriptRoot '..\..\workbench\.venv\Scripts\pythonw.exe')
)
$ErrorActionPreference = 'Stop'
$script = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'import_week.py'))
$Python = [IO.Path]::GetFullPath($Python)
if (-not (Test-Path -LiteralPath $Python)) { throw "pythonw.exe not found: $Python" }
if (-not (Test-Path -LiteralPath $Inbox)) { throw "Inbox not found: $Inbox" }
$taskName = 'Weekly Copy Import'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --inbox "{1}"' -f $script, $Inbox) -WorkingDirectory $PSScriptRoot
$triggers = @(New-ScheduledTaskTrigger -Daily -At '06:00')
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -InputObject (New-ScheduledTask -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Description 'Imports accepted weekly voice copy from Taildrop into the voice client.') -Force -ErrorAction Stop | Out-Null
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
Write-Output "Registered '$taskName'. Log: $([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\data\weekly\import.log')))"
