param([string]$JobPath)
$ErrorActionPreference = 'Stop'

function Invoke-DevmemUpdate($job) {
    $replaced = $false
    $pathsValid = $false
    $stagePath = [IO.Path]::GetFullPath($job.staged)
    $targetPath = [IO.Path]::GetFullPath($job.target)
    $backupPath = [IO.Path]::GetFullPath($job.backup)
    try {
        if ([IO.Path]::GetDirectoryName($stagePath) -ne [IO.Path]::GetDirectoryName($targetPath) -or
            [IO.Path]::GetDirectoryName($backupPath) -ne [IO.Path]::GetDirectoryName($targetPath) -or
            $stagePath -eq $targetPath -or $backupPath -eq $targetPath -or $backupPath -eq $stagePath -or
            -not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            throw 'Invalid update paths.'
        }
        $pathsValid = $true
        if ((Get-FileHash -LiteralPath $stagePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $job.sha256) {
            throw 'Update SHA-256 verification failed.'
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(120)
        foreach ($processId in $job.pids) {
            $oldProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($oldProcess) {
                $remaining = [Math]::Max(0, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
                if (-not $oldProcess.WaitForExit($remaining)) { throw 'The application did not exit within 120 seconds.' }
            }
        }
        # Older launchers do not export their PID; allow the outer EXE to release
        # its image lock after the Python child exits. Also handles other instances.
        $replaceDeadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not $replaced) {
            try {
                [IO.File]::Replace($stagePath, $targetPath, $backupPath, $false)
                $replaced = $true
            } catch [IO.IOException] {
                if ([DateTime]::UtcNow -ge $replaceDeadline) { throw }
                Start-Sleep -Milliseconds 250
            }
        }
        # No inherited PyInstaller/launcher variables may point the new process
        # back into the previous version's cache.
        Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI*' -or $_.Name -eq '_MEIPASS2' -or $_.Name -like 'DEVMEMSTUDIO_LAUNCHER*' } |
            ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
        $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
        $newProcess = Start-Process -FilePath $targetPath -WorkingDirectory ([IO.Path]::GetDirectoryName($targetPath)) -WindowStyle Normal -PassThru
        $null = $newProcess.Handle  # PS 5.1 loses ExitCode unless the handle is opened before exit
        if ($newProcess.WaitForExit(3000) -and $newProcess.ExitCode -ne 0) {
            throw ('The new application exited with code ' + $newProcess.ExitCode)
        }
        return @{ success = $true; version = $job.version; backup = $backupPath }
    } catch {
        $reason = $_.Exception.Message
        $restored = $false
        if ($replaced) {
            try {
                # Keep the failed new EXE at the staging path for diagnosis.
                [IO.File]::Replace($backupPath, $targetPath, $stagePath, $false)
                $restored = $true
                $restoredProcess = Start-Process -FilePath $targetPath -WorkingDirectory ([IO.Path]::GetDirectoryName($targetPath)) -WindowStyle Normal -PassThru
                $restoredProcess.WaitForExit(3000) | Out-Null
            } catch { $reason += ' Rollback/restart: ' + $_.Exception.Message }
        }
        return @{ success = $false; error = $reason; restored = $restored; backup = $backupPath }
    } finally {
        if ($pathsValid -and -not $replaced -and (Test-Path -LiteralPath $stagePath)) {
            Remove-Item -LiteralPath $stagePath -ErrorAction SilentlyContinue
        }
    }
}

if ($JobPath) {
    $job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $result = Invoke-DevmemUpdate $job
    $result | ConvertTo-Json | Set-Content -LiteralPath $job.result -Encoding UTF8
    if (-not $result.success) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show(('Update failed: ' + $result.error + "`nBackup: " + $result.backup), 'DevmemStudio update') | Out-Null
        exit 1
    }
}
