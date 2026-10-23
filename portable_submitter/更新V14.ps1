param([string]$Target,[string]$Rollback)
$ErrorActionPreference='Stop'
function Get-SafePath([string]$Root,[string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':')) { throw 'Unsafe manifest path' }
    $full=[IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $full.StartsWith($Root.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Path escaped package root' }
    return $full
}
function Assert-NoLinks([string]$Root) {
    $item=Get-Item -LiteralPath $Root -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked target refused' }
    foreach($item in Get-ChildItem -LiteralPath $Root -Force -Recurse) {
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked files refused; no changes made' }
    }
}
function Assert-Closed([string]$Root) {
    $prefix=$Root.TrimEnd('\')+'\'
    foreach($p in Get-CimInstance Win32_Process) {
        if($p.ExecutablePath -and $p.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
            throw ('Close all submitter windows first; process '+$p.ProcessId+' is still running')
        }
        if($p.Name -match '^pythonw?\.exe$' -and $p.CommandLine -and $p.CommandLine.IndexOf($prefix,[StringComparison]::OrdinalIgnoreCase) -ge 0) {throw ('Close the source submitter first; process '+$p.ProcessId+' is still running')}
    }
}
if($Rollback) {
    $operation=[IO.Path]::GetFullPath($Rollback)
    $meta=Get-Content -LiteralPath $operation -Encoding UTF8 -Raw | ConvertFrom-Json
    $Target=[IO.Path]::GetFullPath($meta.target)
    $backup=[IO.Path]::GetFullPath($meta.backup)
    Assert-NoLinks $Target;Assert-NoLinks $backup;Assert-Closed $Target
    foreach($entry in $meta.files) {
        $dest=Get-SafePath $Target $entry.path
        if($entry.existed) {
            $old=Get-SafePath $backup $entry.path
            if((Get-FileHash -LiteralPath $old -Algorithm SHA256).Hash -ne $entry.oldSHA256) { throw 'Backup hash mismatch' }
        }
    }
    foreach($entry in $meta.files) {
        $dest=Get-SafePath $Target $entry.path
        if($entry.existed) { Copy-Item -LiteralPath (Get-SafePath $backup $entry.path) -Destination $dest -Force }
        elseif(Test-Path -LiteralPath $dest -PathType Leaf) { Remove-Item -LiteralPath $dest -Force }
    }
    Write-Output 'Rollback complete; current user data/configuration preserved. Full pre-update folder backup retained.'
    exit 0
}
if(-not $Target) {$Target=Read-Host 'Existing office submitter ROOT folder (not the exe folder)'}
$Target=[IO.Path]::GetFullPath($Target.Trim('"')).TrimEnd('\')
$source=[IO.Path]::GetFullPath($PSScriptRoot)
if($Target -eq $source -or $Target.StartsWith($source+'\',[StringComparison]::OrdinalIgnoreCase) -or $source.StartsWith($Target+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Extract V14 separately, outside the existing package' }
foreach($rel in @('启动统一任务递交器.cmd','正式任务递交器','ExampleBrand自然流任务递交器','程序\统一任务递交器\Unified-Task-Submitter.exe')) {
    if(-not(Test-Path -LiteralPath (Join-Path $Target $rel))) { throw 'Select the complete existing package root' }
}
Assert-NoLinks $source;Assert-NoLinks $Target;Assert-Closed $Target
$manifest=Get-Content -LiteralPath (Join-Path $source 'program-files.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$files=@($manifest.files)
foreach($name in @('program-files.json','SHA256SUMS.txt')) {
    $metaPath=Join-Path $source $name
    if(Test-Path -LiteralPath $metaPath -PathType Leaf) {
        $files+=[pscustomobject]@{path=$name;sha256=(Get-FileHash -LiteralPath $metaPath -Algorithm SHA256).Hash}
    }
}
$records=@();$seen=@{}
foreach($entry in $files) {
    $name=[IO.Path]::GetFileName($entry.path)
    if($name -match '(?i)^(saved_task_box|examplebrand_saved_task_box|task_submitter_config|connection|pipeline_paths|examplebrand-last-submission|task_submitter_pending|task_submitter_last_receipt)\.json$' -or $entry.path -match '(?i)(^|[\\/])(examplebrand-history|handoff|logs|data)([\\/]|$)') { throw 'User-data path in program manifest refused' }
    $key=$entry.path.Replace('/','\').ToLowerInvariant()
    if($seen.ContainsKey($key)) {throw 'Duplicate program path'}
    $seen[$key]=$true
    $src=Get-SafePath $source $entry.path;$dest=Get-SafePath $Target $entry.path
    if((Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash -ne $entry.sha256) { throw 'V14 package hash mismatch' }
    $exists=Test-Path -LiteralPath $dest -PathType Leaf
    $records+= [pscustomobject]@{path=$entry.path;existed=$exists;oldSHA256=$(if($exists){(Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash}else{''})}
}
# Full backup is a sibling: the existing recursive launcher cannot discover backup executables.
$stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
$backup=$Target+'-backup-before-V14-'+$stamp
if(Test-Path -LiteralPath $backup) {throw 'Backup already exists'}
Copy-Item -LiteralPath $Target -Destination $backup -Recurse -Force
$original=Get-ChildItem -LiteralPath $Target -File -Force -Recurse
foreach($file in $original) {
    $rel=$file.FullName.Substring($Target.TrimEnd('\').Length+1)
    if((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath (Get-SafePath $backup $rel) -Algorithm SHA256).Hash) {throw 'Full backup verification failed; update not started'}
}
$operation=Join-Path $backup 'V14-update-operation.json'
@{schema=1;target=$Target;backup=$backup;files=$records} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $operation -Encoding UTF8
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $backup 'V14-rollback.ps1')
Write-Output ('Full backup: '+$backup)
Write-Output ('Rollback: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $backup 'V14-rollback.ps1')+'" -Rollback "'+$operation+'"')
try {
    foreach($entry in $files) {
        $dest=Get-SafePath $Target $entry.path
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($dest))
        Copy-Item -LiteralPath (Get-SafePath $source $entry.path) -Destination $dest -Force
        if((Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash -ne $entry.sha256) {throw 'Installed file hash mismatch'}
    }
    # Every old non-program file, including all configs and saved tasks, must remain byte-identical.
    foreach($file in $original) {
        $rel=$file.FullName.Substring($Target.TrimEnd('\').Length+1)
        if(-not $seen.ContainsKey($rel.ToLowerInvariant())) {
            if((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath (Get-SafePath $backup $rel) -Algorithm SHA256).Hash) {throw 'User data preservation check failed'}
        }
    }
} catch {
    Write-Output 'Update failed; run the printed rollback command. Backup is complete.'
    throw
}
Write-Output 'V14 program files verified. All existing user data/configuration unchanged. No program was started.'
