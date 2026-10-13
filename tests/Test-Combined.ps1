#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $root '源码\CombinedRuntime.built.ps1'
. $runtime
$script:Assertions=0
function Check([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw ('FAIL: '+$Message) }
    $script:Assertions++
}
function Snapshot([string]$State,[string]$Identity='test',[string]$Text='连接') {
    return [pscustomobject]@{State=$State;Identity=$Identity;Text=$Text;Enabled=$true}
}
function Temp-Session {
    $path=Join-Path $env:TEMP ('FastOrangeCombinedTest-'+[guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($path)
    return $path
}
function Start-TestProcess {
    return Start-Process -FilePath $script:PowerShell -ArgumentList '-NoProfile -Command "Start-Sleep -Seconds 90"' -WindowStyle Hidden -PassThru
}
function Wait-Status([string]$Path,[string]$Expected,[int]$Seconds=15) {
    $deadline=[DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $status=Read-Json (Join-Path $Path 'worker-status.json')
        if ($status -and $status.State -eq $Expected) { return $status }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('No status '+$Expected+' in '+$Path)
}
function Route-Keys { return @([FastOrangeCombined.RouteApi]::Read() | ForEach-Object { $_.Key } | Sort-Object) -join "`n" }

Initialize-Routes
$before=Route-Keys
$native=@(Get-LiveRoutes | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.Prefix,$_.Index,$_.Gateway,$_.Metric } | Sort-Object)
$cim=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.DestinationPrefix,$_.InterfaceIndex,$_.NextHop,$_.RouteMetric } | Sort-Object)
Check (($native -join "`n") -ceq ($cim -join "`n")) 'Native API layout and full table match Windows CIM'
$immortal=@(Get-LiveRoutes | Where-Object { $_.Immortal -ne 0 -or $_.ValidLifetime -gt 90 })[0]
Check ([FastOrangeCombined.RouteApi]::CheckOwnership($immortal) -eq 1306) 'Native immediate ownership query preserves existing permanent routes'
$row=[FastOrangeCombined.RouteApi]::Preview($immortal,[uint32]90)
Check ($row.Loopback -eq 0 -and $row.AutoconfigureAddress -eq 0 -and $row.Immortal -eq 0 -and $row.ValidLifetime -eq 90) 'Native creation flags and finite lease correctly initialized'
$prefixes=@(Read-ChinaPrefixes)
Check ($prefixes.Count -eq 5495) 'Embedded public data verified'
$memory=New-ReconnectMemory
Check ((Get-ReconnectDecision $memory (Snapshot 'Disconnected') 0).Action -eq 'Wait') 'First disconnected sample waits'
Check ((Get-ReconnectDecision $memory (Snapshot 'Disconnected') 5).Action -eq 'Wait') 'Short disconnect does not click'
$d=Get-ReconnectDecision $memory (Snapshot 'Disconnected') 6
Check ($d.Action -eq 'Connect' -and $d.Delay -eq 30) 'Confirmed disconnect first attempt'
Check ((Get-ReconnectDecision $memory (Snapshot 'Disconnected') 20).Action -eq 'Wait') 'Cooldown'
$d=Get-ReconnectDecision $memory (Snapshot 'Disconnected') 36
Check ($d.Action -eq 'Connect' -and $d.Delay -eq 60) 'Retry backs off'
[void](Get-ReconnectDecision $memory (Snapshot 'Connected' 'test' '已连接') 37)
[void](Get-ReconnectDecision $memory (Snapshot 'Connected' 'test' '已连接') 68)
Check ($memory.Attempts -eq 0) 'Stable connection resets attempts'
foreach ($state in @('Busy','Unknown','Unavailable')) {
    [void](Get-ReconnectDecision $memory (Snapshot $state) 100)
    Check ($null -eq $memory.CandidateSince) ('No connect in '+$state)
}
$memory=New-ReconnectMemory
[void](Get-ReconnectDecision $memory (Snapshot 'Disconnected' 'old') 0)
Check ((Get-ReconnectDecision $memory (Snapshot 'Disconnected' 'new') 20).Action -eq 'Wait') 'Changed control identity requires new confirmation'
Check ((Get-StateForLabel '断开连接' $true) -eq 'Connected') 'Never treat disconnect label as connect'
Check ((Get-StateForLabel '连接' $false) -eq 'Busy') 'Disabled connect button does not click'
Check ((Get-StateForLabel '登录' $true) -eq 'Unknown') 'Login not automated'

# Simulate route repair, network changes, cancellation, and ownership in memory.
$Simulation=$true
$SessionPath=Temp-Session
$ControllerId=$PID; $ControllerTicks=(Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
$SimulationAppId=$PID; $script:SeenApp=$true; $script:Stopwatch=[Diagnostics.Stopwatch]::StartNew()
$script:FakeRoutes=@{}; $script:Owned=@{}; $script:Metric=54001; $script:Boot='test'; $script:TargetKey=''
$script:Journal=[IO.StreamWriter]::new((Join-Path $SessionPath 'routes.jsonl'),$true,$script:Utf8)
try {
    $base=New-RouteSpec '10.0.0.0/8' (Get-Target @()); $base.Metric=10; $base.Immortal=1
    $script:FakeRoutes[$base.Key]=$base
    $list=@('1.0.1.0/24','1.0.2.0/23','10.0.0.0/8')
    Sync-SplitRoutes $list
    Check ($script:Owned.Count -eq 2 -and $script:FakeRoutes.Count -eq 3) 'Pre-existing exact route preserved'
    $lost=@($script:Owned.Keys)[0]; $script:FakeRoutes.Remove($lost)
    Sync-SplitRoutes $list
    Check ($script:Owned.Count -eq 2 -and $script:FakeRoutes.Count -eq 3) 'Missing route after reconnect repaired'
    $modified=@($script:Owned.Keys)[0]
    $replacement=New-RouteSpec $script:Owned[$modified].Prefix (Get-Target @()); $replacement.Metric=42
    $script:FakeRoutes.Remove($modified); $script:FakeRoutes[$replacement.Key]=$replacement
    [void](Remove-OwnedRoutes)
    Check ($script:Owned.Count -eq 0 -and $script:FakeRoutes.Count -eq 2 -and $script:FakeRoutes.ContainsKey($base.Key) -and $script:FakeRoutes.ContainsKey($replacement.Key)) 'Cleanup preserves pre-existing and user-modified routes'
    $script:FakeRoutes.Remove($replacement.Key)
    Sync-SplitRoutes $list
    $permanentKey=@($script:Owned.Keys)[0]
    $permanent=New-RouteSpec $script:Owned[$permanentKey].Prefix (Get-Target @()); $permanent.Immortal=1; $permanent.ValidLifetime=[uint32]::MaxValue
    $script:FakeRoutes[$permanent.Key]=$permanent
    [void](Remove-OwnedRoutes)
    Check ($script:Owned.Count -eq 0 -and $script:FakeRoutes.ContainsKey($permanent.Key)) 'Cleanup relinquishes route externally changed to permanent'
    $script:FakeRoutes.Remove($permanent.Key)
    $originalAdd=${function:Add-LeasedRoute}
    $script:AddCount=0
    function Add-LeasedRoute($Spec) {
        $script:AddCount++
        if ($script:AddCount -eq 2) { return 87 }
        $script:FakeRoutes[$Spec.Key]=$Spec; return 0
    }
    $creationFailed=$false
    try { Sync-SplitRoutes $list } catch { $creationFailed=$true }
    Check ($creationFailed -and $script:Owned.Count -eq 1) 'Partial creation failure retains ownership of created route'
    [void](Remove-OwnedRoutes)
    Check ($script:Owned.Count -eq 0 -and $script:FakeRoutes.Count -eq 1) 'Partial failure cleanup preserves original route'
    Set-Item -Path Function:Add-LeasedRoute -Value $originalAdd
    function Add-LeasedRoute($Spec) {
        $Spec.CreationLeaseInvalid=$true; $Spec.Immortal=1; $Spec.ValidLifetime=[uint32]::MaxValue
        $script:FakeRoutes[$Spec.Key]=$Spec; return 0
    }
    $leaseFailed=$false
    try { Sync-SplitRoutes $list } catch { $leaseFailed=$true }
    Check ($leaseFailed -and $script:Owned.Count -eq 1 -and $script:FatalRouteError) 'Lease-contract failure retains created route for cleanup'
    [void](Remove-OwnedRoutes)
    Check ($script:Owned.Count -eq 0 -and $script:FakeRoutes.Count -eq 1) 'Just-created invalid lease is cleaned without losing original route'
    $script:FatalRouteError=$false
    Set-Item -Path Function:Add-LeasedRoute -Value $originalAdd
    Sync-SplitRoutes $list
    [IO.File]::WriteAllText((Join-Path $SessionPath 'stop'),'stop',$script:Utf8)
    $cancelled=$false
    try { Sync-SplitRoutes @('2.0.0.0/24') } catch [OperationCanceledException] { $cancelled=$true }
    Check $cancelled 'No new routes after stop request'
    [void](Remove-OwnedRoutes)
} finally { $script:Journal.Dispose() }

# A blocked UI helper is forcibly ended after the finite deadline.
$SessionPath=Temp-Session; $script:UiSequence=0; $script:UiProcess=$null
$script:Stopwatch=[Diagnostics.Stopwatch]::StartNew()
$originalStart=${function:Start-HiddenRole}
function Start-HiddenRole([string]$ChildRole,[string[]]$Extra,[switch]$Elevate) {
    $child=Start-TestProcess; $script:HungHelperId=$child.Id; return $child
}
$uiTimedOut=$false; $uiTimer=[Diagnostics.Stopwatch]::StartNew()
try { [void](Request-Ui 'Inspect' $null) } catch { $uiTimedOut=$_.Exception.Message -like '*8 秒*' }
finally { Stop-UiHelper; Set-Item -Path Function:Start-HiddenRole -Value $originalStart }
Check ($uiTimedOut -and $uiTimer.Elapsed.TotalSeconds -lt 12) 'UI hang has bounded timeout'
Check ($null -eq (Get-Process -Id $script:HungHelperId -ErrorAction SilentlyContinue)) 'Hung helper terminated, no delayed invocation'

# Independent child processes survive their controller, then clean their simulated routes.
foreach ($scenario in @('close-console','exit-app','quit')) {
    $session=Temp-Session; $controller=$null; $app=$null; $worker=$null
    try {
        $controller=Start-TestProcess; $app=Start-TestProcess
        $controlTicks=$controller.StartTime.ToUniversalTime().Ticks
        $args='-NoProfile -STA -ExecutionPolicy Bypass -File "{0}" -Role Worker -SessionPath "{1}" -ControllerId {2} -ControllerTicks {3} -Simulation -SimulationAppId {4}' -f
            $runtime,$session,$controller.Id,$controlTicks,$app.Id
        $worker=Start-Process -FilePath $script:PowerShell -ArgumentList $args -WindowStyle Hidden -PassThru
        [void](Wait-Status $session 'Running')
        $deadline=[DateTime]::UtcNow.AddSeconds(12)
        do {
            $simulated=@(Read-Json (Join-Path $session 'simulation-routes.json'))
            if ($simulated.Count -eq 3) { break }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        Check ($simulated.Count -eq 3) ($scenario+' installed simulated routes')
        $timer=[Diagnostics.Stopwatch]::StartNew()
        switch ($scenario) {
            'close-console' { Stop-Process -Id $controller.Id -Force }
            'exit-app' { Stop-Process -Id $app.Id -Force }
            'quit' { [IO.File]::WriteAllText((Join-Path $session 'stop'),'stop',$script:Utf8) }
        }
        $status=Wait-Status $session 'Stopped'
        [void]$worker.WaitForExit(4000)
        Check ($status.Remaining -eq 0 -and $status.Result -eq 0 -and $worker.HasExited) ($scenario+' stopped and cleaned')
        $remaining=@(Read-Json (Join-Path $session 'simulation-routes.json'))
        Check ($remaining.Count -eq 1 -and $remaining[0].Prefix -eq '10.0.0.0/8') ($scenario+' left pre-existing route intact')
        Check ($timer.Elapsed.TotalSeconds -lt 5) ($scenario+' cleanup prompt')
        if ($scenario -eq 'close-console') { Check (-not $app.HasExited) 'Closing black window keeps app alive' }
        if ($scenario -eq 'exit-app') { Check (-not $controller.HasExited) 'Worker stops when app exits' }
    } finally {
        foreach ($ownedProcess in @($worker,$controller,$app)) {
            if ($null -ne $ownedProcess) {
                try { if (-not $ownedProcess.HasExited) { Stop-Process -Id $ownedProcess.Id -Force }; $ownedProcess.Dispose() } catch { }
            }
        }
    }
}
$Simulation=$false
Check ((Route-Keys) -ceq $before) 'No real network routes changed by tests'
Write-Host ('PASS: '+$script:Assertions+' assertions; no real clicks or route writes.')
