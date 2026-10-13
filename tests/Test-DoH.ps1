param([switch]$Live, [int]$PhysicalInterfaceIndex = -1)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$outputPath = Join-Path $env:TEMP ('FastOrangeDoHTests-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $outputPath)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$library = Join-Path $outputPath 'FastOrangeDomainBridge.dll'
& $compiler /nologo /target:library "/out:$library" (Join-Path $workspace '源码\DomainBridge.cs')
if ($LASTEXITCODE -ne 0) { throw 'DoH library compilation failed.' }
$harness = Join-Path $outputPath 'DoHRegressionHarness.exe'
& $compiler /nologo /target:exe "/r:$library" "/out:$harness" (Join-Path $PSScriptRoot 'DoHRegressionHarness.cs')
if ($LASTEXITCODE -ne 0) { throw 'DoH harness compilation failed.' }
& $harness
if ($LASTEXITCODE -ne 0) { throw 'DoH regression tests failed.' }
if ($Live) {
    if ($PhysicalInterfaceIndex -le 0) { throw 'Live probes require the physical IPv4 interface index.' }
    $probe = Join-Path $outputPath 'DoHReadOnlyProbe.exe'
    & $compiler /nologo /target:exe "/r:$library" "/out:$probe" (Join-Path $PSScriptRoot 'DoHReadOnlyProbe.cs')
    if ($LASTEXITCODE -ne 0) { throw 'DoH probe compilation failed.' }
    & $probe $PhysicalInterfaceIndex
    if ($LASTEXITCODE -ne 0) { throw 'One or more live DoH probes failed.' }
}
Write-Output "Test binaries: $outputPath"
