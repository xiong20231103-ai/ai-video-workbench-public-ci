param([Parameter(Mandatory=$true)][string]$AssignmentFile, [Parameter(Mandatory=$true)][string]$UsageFile)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$stream = $null
$locked = $false
function Write-Atomic([string]$destination, [string]$json) {
    $temporary = $destination + '.' + $PID + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($json)
        $output = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $output.Write($bytes, 0, $bytes.Length); $output.Flush($true) } finally { $output.Dispose() }
        for ($attempt = 0; $attempt -lt 4; $attempt++) {
            try {
                if ([IO.File]::Exists($destination)) { [IO.File]::Replace($temporary, $destination, [NullString]::Value) }
                else { [IO.File]::Move($temporary, $destination) }
                return
            } catch { if ($attempt -eq 3) { throw }; Start-Sleep -Milliseconds 50 }
        }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
try {
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($AssignmentFile)) | Out-Null
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($UsageFile)) | Out-Null
    $stream = [IO.File]::Open($AssignmentFile + '.oslock', [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $locked) {
        try { $stream.Lock(0, 1); $locked = $true }
        catch [IO.IOException] { if ([DateTime]::UtcNow -ge $deadline) { throw 'COPY_LOCK_TIMEOUT' }; Start-Sleep -Milliseconds 50 }
    }
    $record = @{version=1; assignments=@{}}
    if ([IO.File]::Exists($AssignmentFile)) { $record = [IO.File]::ReadAllText($AssignmentFile) | ConvertFrom-Json }
    if ($record.version -ne 1 -or $null -eq $record.assignments) { throw 'COPY_STATE_INVALID' }
    $usage = $record.usageState
    if ($null -eq $usage) {
        $usage = @{version=1; materialDecks=@{}; copyDecks=@{}}
        if ([IO.File]::Exists($UsageFile)) { $usage = [IO.File]::ReadAllText($UsageFile) | ConvertFrom-Json }
    }
    if ($usage.version -ne 1 -or $null -eq $usage.materialDecks -or $null -eq $usage.copyDecks) { throw 'COPY_USAGE_INVALID' }
    [Console]::WriteLine((@{type='ready'; lockOwnerPid=$PID; assignments=$record.assignments; usage=$usage} | ConvertTo-Json -Depth 100 -Compress))
    # Pipe EOF (including Node death) releases the OS lock without deleting it.
    $line = [Console]::ReadLine()
    if ($null -ne $line) {
        $request = $line | ConvertFrom-Json
        if ($request.type -eq 'commit') {
            if ($null -eq $request.assignments -or $request.usage.version -ne 1) { throw 'COPY_COMMIT_INVALID' }
            $authority = @{version=1; assignments=$request.assignments; usageState=$request.usage}
            Write-Atomic $AssignmentFile ($authority | ConvertTo-Json -Depth 100 -Compress)
            # This legacy file is a projection only. The next read uses authority.
            $projectionError = ''
            if ([IO.File]::Exists($UsageFile) -or @($request.usage.copyDecks.PSObject.Properties).Count -gt 0 -or @($request.usage.materialDecks.PSObject.Properties).Count -gt 0) {
                try { Write-Atomic $UsageFile ($request.usage | ConvertTo-Json -Depth 100 -Compress) } catch { $projectionError = $_.Exception.Message }
            }
            [Console]::WriteLine((@{type='committed'; projectionError=$projectionError} | ConvertTo-Json -Compress))
        } elseif ($request.type -ne 'abort') { throw 'COPY_COMMAND_INVALID' }
    }
} catch {
    [Console]::WriteLine((@{type='error'; message=$_.Exception.Message} | ConvertTo-Json -Compress))
    exit 1
} finally {
    if ($locked) { $stream.Unlock(0, 1) }
    if ($null -ne $stream) { $stream.Dispose() }
}
