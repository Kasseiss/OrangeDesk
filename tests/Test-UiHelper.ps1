#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $root '源码\CombinedRuntime.built.ps1'
. $runtime
$SessionPath=Join-Path $env:TEMP ('FastOrangeUiReadTest-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($SessionPath)
$ControllerId=$PID; $ControllerTicks=(Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
$script:SeenApp=$true
try {
    Initialize-Automation
    $OwnerId=0; $OwnerTicks=0
    $guarded=Prepare-HomePage
    if (-not $guarded.State) { throw 'Guarded homepage scan failed.' }
    if (Test-UiMayAct) { throw 'Guard should reject navigation without a live owner.' }
    $snapshot=Request-Ui 'Inspect' $null
    if ($null -eq $snapshot -or -not $snapshot.State) { throw 'Empty UI snapshot.' }
    if (-not $script:UiProcess -or $script:UiProcess.HasExited) { throw 'Helper did not remain available.' }
    $snapshot2=Request-Ui 'Inspect' $null
    $helper=$script:UiProcess
    Stop-UiHelper
    # Keep an independent process object; Stop-UiHelper disposes its own handle.
    Write-Host ('READ ONLY: '+$snapshot.State+' | '+$snapshot.Text+'; second scan '+$snapshot2.State)
    Write-Host 'PASS: Guarded homepage scan cannot navigate; UI helper responds to two read-only requests and stops. No clicks.'
} finally { Stop-UiHelper }
