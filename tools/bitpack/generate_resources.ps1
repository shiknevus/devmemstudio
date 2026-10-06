param([string]$Root = $PSScriptRoot)
$ErrorActionPreference='Stop'
$build=Join-Path $Root '_build'
[IO.Directory]::CreateDirectory($build) | Out-Null
function Raw-Resource([int]$Id,[int]$Type,[byte[]]$Bytes) {
 $lines=[Collections.Generic.List[string]]::new()
 $lines.Add("$Id $Type`r`nBEGIN")
 for($i=0;$i -lt $Bytes.Length;$i+=16){
  $words=for($j=$i;$j -lt [Math]::Min($i+16,$Bytes.Length);$j+=2){
   $word=[int]$Bytes[$j]; if($j+1 -lt $Bytes.Length){$word=$word -bor ([int]$Bytes[$j+1] -shl 8)}
   '0x{0:X4}' -f $word
  }
  $line=' '+($words -join ', ')
  if($i+16 -lt $Bytes.Length){$line+=','}
  $lines.Add($line)
 }
 $lines.Add('END')
 return $lines -join "`r`n"
}
$ico=[IO.File]::ReadAllBytes((Join-Path $Root 'app.ico'))
if($ico.Length -lt 6 -or [BitConverter]::ToUInt16($ico,2) -ne 1){throw 'Invalid ICO'}
$count=[BitConverter]::ToUInt16($ico,4)
$group=[Collections.Generic.List[byte]]::new()
$group.AddRange([byte[]]$ico[0..5])
$output=[Collections.Generic.List[string]]::new()
for($i=0;$i -lt $count;$i++){
 $offset=6+$i*16
 $length=[BitConverter]::ToUInt32($ico,$offset+8)
 $imageOffset=[BitConverter]::ToUInt32($ico,$offset+12)
 if($imageOffset+$length -gt $ico.Length){throw 'Invalid ICO image offset'}
 $id=201+$i
 $group.AddRange([byte[]]$ico[$offset..($offset+11)])
 $group.AddRange([BitConverter]::GetBytes([uint16]$id))
 $output.Add((Raw-Resource $id 3 ([byte[]]$ico[$imageOffset..($imageOffset+$length-1)])))
}
$output.Add((Raw-Resource 101 14 $group.ToArray()))
$output.Add((Raw-Resource 1 24 ([Text.Encoding]::UTF8.GetBytes([IO.File]::ReadAllText((Join-Path $Root 'app.manifest'))))))
# .rc2, not .h: windres (like rc.exe) silently ignores resource statements in included .h/.c files.
[IO.File]::WriteAllText((Join-Path $build 'resource_data.rc2'),($output -join "`r`n"),[Text.Encoding]::ASCII)
