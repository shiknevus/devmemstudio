param([string]$JobPath)
$ErrorActionPreference = 'Stop'

# Start-Process treats [ ] in paths as wildcards; Process.Start takes them literally.
function Start-Devmem($path) {
    $info = New-Object Diagnostics.ProcessStartInfo $path
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($path)
    $info.UseShellExecute = $false
    return [Diagnostics.Process]::Start($info)
}

# Microsoft.PowerShell.Utility is not always importable (locked-down images, redirected
# PSModulePath, ConstrainedLanguage), so hash through .NET like the rest of this script.
function Get-DevmemHash($path) {
    $stream = [IO.File]::OpenRead($path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}

# Authenticode thumbprint of the staged EXE's signer (null when unsigned or
# when the signature is not present). Used to pin the update to the publisher's
# certificate, so a compromised release feed cannot install arbitrary code.
function Get-DevmemSignerThumbprint($path) {
    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $path
        if ($sig -and $sig.Status -eq 'Valid' -and $sig.SignerCertificate) {
            return $sig.SignerCertificate.Thumbprint
        }
        if ($sig -and $sig.Status -in @('NotSigned', 'UnknownError', 'HashMismatch', 'NotTrusted', 'Incompatible')) {
            # Incomplete/unknown states never satisfy a pinned expectation: the
            # caller treats a null here as "cannot prove the expected signer".
            return $null
        }
    } catch { }
    return $null
}

# Verify the staged executable against a pinned signer thumbprint. Empty
# expected means the publisher has not enabled pinning yet (legacy behavior:
# SHA-256 only). Any mismatch aborts the update before the swap.
function Assert-DevmemSigner($path, $expected) {
    if (-not $expected) { return }
    $thumb = Get-DevmemSignerThumbprint $path
    if (-not $thumb) {
        throw ('The update is not Authenticode-signed; refusing to install. Expected signer thumbprint: ' + $expected)
    }
    if ($thumb -ne $expected) {
        throw ('The update signer does not match the pinned certificate. Found: ' + $thumb + '; expected: ' + $expected)
    }
}

# Antivirus scans and a just-exited image keep the EXE locked briefly (sharing or access denied).
function Replace-DevmemFile($source, $target, $backup) {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($true) {
        try {
            [IO.File]::Replace($source, $target, $backup, $false)
            return
        } catch [IO.IOException], [UnauthorizedAccessException] {
            if ([DateTime]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds 250
        }
    }
}

# The launcher's only unowned window is its failure screen (the starting splash is owned); it runs the app as a child process.
function Wait-DevmemStarted($process, $seconds) {
    $begin = [DateTime]::UtcNow
    while ($true) {
        if ($process.WaitForExit(250)) {
            if ($process.ExitCode -ne 0) { throw ('The new application exited with code ' + $process.ExitCode) }
            return
        }
        $process.Refresh()
        if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
            throw ('The new application showed an error: ' + $process.MainWindowTitle)
        }
        $elapsed = ([DateTime]::UtcNow - $begin).TotalSeconds
        if ($elapsed -ge 3) {
            try {
                $child = Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $process.Id) -ErrorAction Stop |
                    Where-Object { $_.CreationDate -ge $process.StartTime } | Select-Object -First 1
            } catch { return }   # no WMI: the plain 3 s check
            if ($child) { return }
        }
        if ($elapsed -ge $seconds) { throw ('The new application did not start within ' + $seconds + ' seconds.') }
    }
}

function Invoke-DevmemUpdate($job) {
    $replaced = $false
    $ownsStage = $false
    $stagePath = $null
    $backupPath = $null
    $newProcess = $null
    $startSeconds = 120
    if ($job.start_timeout) { $startSeconds = [int]$job.start_timeout }
    try {
        $stagePath = [IO.Path]::GetFullPath($job.staged)
        # The staged name is ours alone, so it is removed even if the other paths are bad.
        $ownsStage = [IO.Path]::GetFileName($stagePath) -match '^\.DevmemStudio-update-[0-9a-f]{32}\.exe$'
        $targetPath = [IO.Path]::GetFullPath($job.target)
        $backupPath = [IO.Path]::GetFullPath($job.backup)
        if ([IO.Path]::GetDirectoryName($stagePath) -ne [IO.Path]::GetDirectoryName($targetPath) -or
            [IO.Path]::GetDirectoryName($backupPath) -ne [IO.Path]::GetDirectoryName($targetPath) -or
            $stagePath -eq $targetPath -or $backupPath -eq $targetPath -or $backupPath -eq $stagePath -or
            -not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            throw 'Invalid update paths.'
        }
        $ownsStage = $true
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
        # Verify right before the swap: the staged file sat beside the target during the wait.
        if ((Get-DevmemHash $stagePath) -ne ([string]$job.sha256).ToLowerInvariant()) {
            throw 'Update SHA-256 verification failed.'
        }
        Assert-DevmemSigner $stagePath ([string]$job.expected_signer)
        Replace-DevmemFile $stagePath $targetPath $backupPath
        $replaced = $true
        # No inherited PyInstaller/launcher variables may point the new process
        # back into the previous version's cache.
        Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI*' -or $_.Name -eq '_MEIPASS2' -or $_.Name -like 'DEVMEMSTUDIO_LAUNCHER*' } |
            ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
        $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
        $newProcess = Start-Devmem $targetPath
        Wait-DevmemStarted $newProcess $startSeconds
        return @{ success = $true; version = $job.version; backup = $backupPath; pid = $newProcess.Id }
    } catch {
        $reason = $_.Exception.Message
        $restored = $false
        if ($newProcess -and -not $newProcess.HasExited) {
            try { $newProcess.Kill(); $newProcess.WaitForExit(5000) | Out-Null } catch { }
        }
        if ($replaced) {
            try {
                # Keep the failed new EXE at the staging path for diagnosis.
                Replace-DevmemFile $backupPath $targetPath $stagePath
                $restored = $true
                $restoredProcess = Start-Devmem $targetPath
                $restoredProcess.WaitForExit(3000) | Out-Null
            } catch { $reason += ' Rollback/restart: ' + $_.Exception.Message }
        }
        return @{ success = $false; error = $reason; restored = $restored; backup = $backupPath }
    } finally {
        if ($ownsStage -and -not $replaced -and (Test-Path -LiteralPath $stagePath)) {
            Remove-Item -LiteralPath $stagePath -ErrorAction SilentlyContinue
        }
    }
}

if ($JobPath) {
    # AppLocker/WDAC ConstrainedLanguage cannot swap files; no marker makes the app stay open.
    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') { exit 3 }
    $job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($job.started) { [IO.File]::WriteAllText([string]$job.started, [string]$PID) }
    $result = Invoke-DevmemUpdate $job
    try { $result | ConvertTo-Json | Set-Content -LiteralPath $job.result -Encoding UTF8 } catch { }
    if (-not $result.success) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show(('Update failed: ' + $result.error + "`nBackup: " + $result.backup), 'DevmemStudio update') | Out-Null
        exit 1
    }
}
