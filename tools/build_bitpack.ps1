$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $PSScriptRoot 'bitpack'
& (Join-Path $backend 'build.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Bit_pack backend build failed' }
Copy-Item -LiteralPath (Join-Path $backend 'pack_bit.exe') -Destination (Join-Path $root 'assets/bitpack/pack_bit.exe')
Write-Host 'Updated bundled Bit_pack CLI engine.'
