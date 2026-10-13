#requires -Version 5.1
param([switch]$KillWorker)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$runtimePath=Join-Path $taskRoot '源码\CombinedRuntime.built.ps1'
. $runtimePath
Initialize-Bridge
function Route-Profile {
    return (@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.DestinationPrefix,$_.InterfaceIndex,$_.NextHop,$_.RouteMetric } | Sort-Object) -join "`n")
}
function Await-Status([string]$Expected) {
    $deadline=[DateTime]::UtcNow.AddSeconds(20)
    do {
        $status=Read-Json (Join-Path $SessionPath 'worker-status.json')
        if ($status -and $status.State -eq $Expected) { return $status }
        if ($worker.HasExited -and $Expected -eq 'Running') { throw 'Worker failed during startup.' }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('Worker did not reach '+$Expected)
}
$token=[guid]::NewGuid().ToString('N')
$SessionPath=Join-Path (Join-Path $script:Root 'sessions') $token
[void][IO.Directory]::CreateDirectory($SessionPath)
Write-AtomicJson (Join-Path $SessionPath 'bundle.json') @{Path=(Join-Path $taskRoot '快橙一键启动.cmd')}
# Pause UI actions for this real lifecycle test; reconnect decisions have separate simulation tests.
[IO.File]::WriteAllText((Join-Path $SessionPath 'pause'),'pause',$script:Utf8)
$original=[FastOrangeCombined.ProxySettings]::Read(); $routes=Route-Profile
$worker=$null; $controller=$null
try {
    $controller=Start-Process -FilePath $script:PowerShell -ArgumentList '-NoProfile -Command "Start-Sleep -Seconds 60"' -WindowStyle Hidden -PassThru
    $ticks=$controller.StartTime.ToUniversalTime().Ticks
    $arguments='-NoProfile -STA -ExecutionPolicy Bypass -File "{0}" -Role Worker -SessionPath "{1}" -ControllerId {2} -ControllerTicks {3} -RunSeconds 30' -f $runtimePath,$SessionPath,$controller.Id,$ticks
    $worker=Start-Process -FilePath $script:PowerShell -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $status=Await-Status 'Running'
    $during=[FastOrangeCombined.ProxySettings]::Read()
    if ($during.AutoUrl -notmatch $token -or $during.Flags -ne 5) { throw 'Real worker did not install its PAC.' }
    Write-Host 'PASS Real production worker starts temporary split without route writes or UI actions'
    if ($KillWorker) {
        Stop-Process -Id $worker.Id -Force
        [void]$worker.WaitForExit(5000)
        & (Join-Path $taskRoot '快橙一键启动.cmd') --restore
        if ($LASTEXITCODE -ne 0) { throw 'Recovery command failed after worker termination.' }
        Write-Host 'PASS Standalone recovery command repairs a force-killed background worker'
    } else {
        Stop-Process -Id $controller.Id -Force
        $status=Await-Status 'Stopped'
        if (-not $status.ProxyRestored -or $status.Result -ne 0 -or $status.Error) { throw ('Worker cleanup failed: '+($status | ConvertTo-Json -Compress)) }
    }
    $restored=[FastOrangeCombined.ProxySettings]::Read()
    if (($original | ConvertTo-Json -Depth 6 -Compress) -cne ($restored | ConvertTo-Json -Depth 6 -Compress)) { throw 'Original system proxy was not fully restored.' }
    if ($routes -cne (Route-Profile)) { throw 'IPv4 route table changed.' }
    if (-not $worker.WaitForExit(5000)) { throw 'Worker stayed alive after cleanup.' }
    Write-Host 'PASS Real worker restores proxy and exits after controller termination; IPv4 routes unchanged'
    Write-Host ('Evidence: '+$SessionPath)
} finally {
    [IO.File]::WriteAllText((Join-Path $SessionPath 'stop'),'stop',$script:Utf8)
    if ($null -ne $worker) { [void]$worker.WaitForExit(10000) }
    $saved=Read-Json (Join-Path $SessionPath 'proxy-before.json')
    if ($saved) { [void][FastOrangeCombined.ProxySettings]::RestoreOwned((Convert-ProxyProfile $saved.Before),(Convert-ProxyProfile $(if ($saved.Applied) { $saved.Applied } else { $saved.Before })),$saved.PacUrl) }
    if ($null -ne $controller) { if (-not $controller.HasExited) { Stop-Process -Id $controller.Id -Force -ErrorAction SilentlyContinue }; $controller.Dispose() }
    if ($null -ne $worker) { $worker.Dispose() }
}
