param(
    [string]$OutputName = 'exe-isolated',
    [switch]$NativeWindow,
    [string]$Scale = '1'
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$artifactRoot = [IO.Path]::GetFullPath((Join-Path $projectDir 'artifacts'))
$targetDir = [IO.Path]::GetFullPath((Join-Path $artifactRoot $OutputName))
if (-not $targetDir.StartsWith($artifactRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Acceptance output must remain under the project artifacts directory.'
}
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
$exePath = Join-Path $targetDir 'DevmemStudio.exe'
Copy-Item -LiteralPath (Join-Path $projectDir 'dist\DevmemStudio.exe') -Destination $exePath -Force
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $exePath
$start.WorkingDirectory = $targetDir
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.Environment['PATH'] = Join-Path $env:SystemRoot 'System32'
foreach ($key in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH')) {
    [void]$start.Environment.Remove($key)
}
$start.Environment['QT_SCALE_FACTOR'] = $Scale
if (-not $NativeWindow) { $start.ArgumentList.Add('--offscreen') }
$start.ArgumentList.Add('--smoke-test')
$start.ArgumentList.Add((Join-Path $targetDir 'acceptance'))
$process = [Diagnostics.Process]::Start($start)
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()
if (-not $process.WaitForExit(60000)) {
    $process.Kill($true)
    throw 'Executable acceptance exceeded 60 seconds.'
}
$stdout.Result | Set-Content -LiteralPath (Join-Path $targetDir 'stdout.log') -Encoding utf8
$stderr.Result | Set-Content -LiteralPath (Join-Path $targetDir 'stderr.log') -Encoding utf8
$reportPath = Join-Path $targetDir 'acceptance\report.json'
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $reportPath)) {
    throw "Executable acceptance failed with exit code $($process.ExitCode). See $targetDir."
}
$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
if (-not $report.passed -or -not $report.frozen) { throw "Packaged acceptance failed: $reportPath" }
[PSCustomObject]@{
    Passed = $report.passed
    BundledRuntime = $report.frozen
    Checks = $report.checks.Count
    PythonPathRemoved = $true
    NativeWindow = [bool]$NativeWindow
    Scale = $Scale
    Report = $reportPath
}
