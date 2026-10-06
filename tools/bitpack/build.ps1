param([switch]$Test)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
Push-Location -LiteralPath $root
try {
 foreach($tool in @('gcc.exe','windres.exe')){if(-not(Get-Command $tool -ErrorAction SilentlyContinue)){throw "$tool is not on PATH (MinGW-w64 required)."}}
 [IO.Directory]::CreateDirectory((Join-Path $root '_build')) | Out-Null
 Write-Host '[1/3] Generate icon and manifest resources (no binary-resource subprocess reads)'
 & (Join-Path $root 'generate_resources.ps1')
 Write-Host '[2/3] Compile resources'
 & windres -c 65001 -O coff app.rc -o '_build\app.res'
 if($LASTEXITCODE -ne 0){throw 'Resource compilation failed'}
 Write-Host '[3/3] Compile and link Bit_pack 2.0'
 $link='-Wl,--dynamicbase,--nxcompat'
 if((& gcc -dumpmachine) -match 'x86_64'){$link+=',--high-entropy-va'}
 & gcc -std=c11 -Os -s -Wall -Wextra -Wpedantic -Wformat=2 -Werror -fstack-protector-strong -municode -mwindows $link -o '_build\pack_bit.new.exe' pack_bit.c pack_core.c '_build\app.res' -lshell32 -lole32 -luuid -ladvapi32 -lcomctl32
 if($LASTEXITCODE -ne 0){throw 'Compilation failed; previous pack_bit.exe preserved'}
 # .NET replacement operates on exact paths; the old EXE is kept until successful compilation.
 $new=Join-Path $root '_build\pack_bit.new.exe'; $target=Join-Path $root 'pack_bit.exe'
 if([IO.File]::Exists($target)){[IO.File]::Replace($new,$target,(Join-Path $root '_build\pack_bit.previous.exe'))}else{[IO.File]::Move($new,$target)}
 Write-Host ('Build OK: '+$target)
 if($Test){& (Join-Path $root 'tests\test.ps1'); if($LASTEXITCODE -ne 0){throw 'Tests failed'}}
} finally {Pop-Location}
