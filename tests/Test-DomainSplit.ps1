#requires -Version 5.1
param([switch]$LiveNetwork,[switch]$ProxyLifecycle)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $taskRoot '源码\CombinedRuntime.built.ps1')
$script:Checks=0
function Check([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw ('FAIL: '+$Message) }
    $script:Checks++
    Write-Host ('PASS '+$Message)
}
Initialize-Bridge
Check ([FastOrangeCombined.ProxySettings]::MergeFlags(3,5,13) -eq 11) 'Restore untouched manual-proxy bit while preserving external autodetect change'
Check ([FastOrangeCombined.ProxySettings]::MergeFlags(9,5,5) -eq 9) 'Restore original autodetect flags'
Check ([FastOrangeCombined.ProxySettings]::MergeFlags(9,5,7) -eq 11) 'Keep newly enabled manual proxy flags'
$rules=Read-DomainRules
$policy=[FastOrangeCombined.DomainPolicy]::new($rules,((Read-ChinaPrefixes) -join "`n"))
Check ($policy.RuleCount -gt 100000 -and $policy.UnsupportedRuleCount -eq 0) 'Embedded domain rules parse completely'
Check ($policy.PrefixCount -eq 5495 -and $policy.InvalidPrefixCount -eq 0) 'Embedded CN prefixes parse completely'
foreach ($hostName in 'www.zhihu.com','pic1.zhimg.com','www.baidu.com','www.bilibili.com','223.5.5.5','10.20.30.40') {
    Check ($policy.IsDirect($hostName)) ('Domestic match '+$hostName)
}
foreach ($hostName in 'chatgpt.com','auth.openai.com','cdn.oaistatic.com','files.oaiusercontent.com','claude.ai','api.anthropic.com','www.zhihu.com.evil.example','zhihucom.example') {
    Check (-not $policy.IsDirect($hostName)) ('VPN match '+$hostName)
}
$override=[FastOrangeCombined.DomainPolicy]::new("openai.com`nchatgpt.com`nfull:only.test`nregexp:^cn[0-9]+\.test$",'')
Check (-not $override.IsDirect('chatgpt.com') -and -not $override.IsDirect('api.openai.com')) 'AI services remain VPN despite direct list'
Check ($override.IsDirect('only.test') -and -not $override.IsDirect('sub.only.test')) 'Exact host rule boundary'
Check ($override.IsDirect('cn123.test')) 'Regex rule is supported'
$bridge=$null; $lease=$null; $controller=$null; $mutex=$null; $locked=$false
$before=$null; $applied=$null; $proxyOwner=$null; $token=[guid]::NewGuid().ToString('N')
$testPath=Join-Path (Join-Path $script:Root 'sessions') $token
[void][IO.Directory]::CreateDirectory($testPath)
$SessionPath=$testPath
try {
    if ($ProxyLifecycle) {
        $mutex=[Threading.Mutex]::new($false,'Local\FastOrangeCombined')
        try { $locked=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked=$true }
        if (-not $locked) { throw 'A production helper is running; close it before the proxy lifecycle test.' }
        $proxyOwner=Acquire-ProxyOwnerLock
        $before=[FastOrangeCombined.ProxySettings]::Read()
        Write-AtomicJson (Join-Path $testPath 'original-profile.json') $before
    }
    $bridge=[FastOrangeCombined.BridgeServer]::new($policy,$token)
    $port=$bridge.Start()
    $target=Get-BrowserTarget
    $bridge.Configure($target.Index,$target.VpnReady)
    $pac=[Net.WebClient]::new()
    try { $pac.Proxy=$null; $pacText=$pac.DownloadString($bridge.PacUrl) } finally { $pac.Dispose() }
    Check ($pacText.Contains('PROXY 127.0.0.1:'+ $port+'; DIRECT')) 'PAC provides original-route fallback when server stops'
    if ($LiveNetwork) {
        foreach ($hostName in 'www.zhihu.com','pic1.zhimg.com','chatgpt.com') {
            # Retain chain/hostname validation; unavailable online CRL endpoints must not dominate this network test.
            $curlResult=@(& curl.exe --proxy ('http://127.0.0.1:'+$port) --noproxy '""' --ssl-revoke-best-effort --max-time 25 --silent --show-error --output NUL --write-out '%{http_code}' ('https://'+$hostName+'/') 2>&1)
            $curlExit=$LASTEXITCODE
            $events=@($bridge.DrainEvents())
            $events | ForEach-Object { Write-Host $_ }
            Write-AtomicJson (Join-Path $testPath ($hostName+'.json')) @{Host=$hostName;CurlExit=$curlExit;HttpStatus=($curlResult -join "`n");Events=$events}
            Check ($curlExit -eq 0 -and ($curlResult -join '').Trim() -match '^[1-5][0-9][0-9]$') ('HTTPS tunnel and certificate verification '+$hostName)
            $localAddress=@(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $target.Index | Where-Object AddressState -eq Preferred | Select-Object -ExpandProperty IPAddress)
            if ($hostName -ne 'chatgpt.com') {
                $line=@($events | Where-Object { $_ -match ('\bDIRECT '+[regex]::Escape($hostName)+' ') })
                Check ($line.Count -gt 0 -and @($localAddress | Where-Object { $line[-1].Contains('sourceIP='+$_+' ') }).Count -gt 0) 'Zhihu uses the physical interface while VPN stays connected'
            } else {
                $tapIndex=@(Get-NetRoute -DestinationPrefix '0.0.0.0/1' -AddressFamily IPv4 -PolicyStore ActiveStore)[0].InterfaceIndex
                $tapAddress=@(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $tapIndex | Select-Object -ExpandProperty IPAddress)
                $line=@($events | Where-Object { $_ -match '\bVPN chatgpt\.com ' })
                Check ($line.Count -gt 0 -and @($tapAddress | Where-Object { $line[-1].Contains('sourceIP='+$_+' ') }).Count -gt 0) 'ChatGPT uses the existing VPN interface'
            }
        }
    }
    if ($ProxyLifecycle) {
        $controller=Start-Process -FilePath $script:PowerShell -ArgumentList '-NoProfile -Command "Start-Sleep -Seconds 60"' -WindowStyle Hidden -PassThru
        $controllerTicks=$controller.StartTime.ToUniversalTime().Ticks
        Write-AtomicJson (Join-Path $testPath 'proxy-before.json') @{Version=2;PacUrl=$bridge.PacUrl;Before=$before;Applied=$null}
        $lease=[FastOrangeCombined.ProxyLease]::new($before,$bridge.PacUrl,$controller.Id,$controllerTicks,(Join-Path $testPath 'stop'))
        $lease.AttachResource($bridge)
        $lease.Start(); $applied=$lease.Applied
        Write-AtomicJson (Join-Path $testPath 'proxy-before.json') @{Version=2;PacUrl=$bridge.PacUrl;Before=$before;Applied=$applied}
        $during=[FastOrangeCombined.ProxySettings]::Read()
        Check ($during.Flags -eq 5 -and $during.AutoUrl -eq $bridge.PacUrl) 'Temporary system PAC is effective'
        Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'WinHttpPacProbe.cs'),[Text.Encoding]::UTF8))
        Check ([WinHttpPacProbe]::ReadSystemPac() -ceq $bridge.PacUrl) 'Windows system-proxy reader sees temporary PAC'
        foreach ($pacTestUrl in 'https://www.zhihu.com/','https://chatgpt.com/') {
            Check ([WinHttpPacProbe]::Resolve($pacTestUrl).Contains('127.0.0.1:'+$port)) ('Windows downloads and evaluates PAC for '+$pacTestUrl)
        }
        Stop-Process -Id $controller.Id -Force
        $watch=[Diagnostics.Stopwatch]::StartNew()
        while (-not $lease.Restored -and $watch.Elapsed.TotalSeconds -lt 5) { Start-Sleep -Milliseconds 50 }
        Check ($lease.Restored -and -not $lease.LastError) 'Independent watcher restores settings on controller termination'
        $after=[FastOrangeCombined.ProxySettings]::Read()
        Check ($after.Flags -eq $before.Flags -and $after.Server -eq $before.Server -and $after.Bypass -eq $before.Bypass -and $after.AutoUrl -eq $before.AutoUrl) 'Original effective proxy configuration restored'
        Check (($before.Raw | ConvertTo-Json -Depth 5 -Compress) -ceq ($after.Raw | ConvertTo-Json -Depth 5 -Compress)) 'Original registry values and absence restored exactly'
        $probe=[Net.Sockets.TcpClient]::new()
        $stopped=$false
        try { $probe.Connect('127.0.0.1',$port) } catch { $stopped=$true } finally { $probe.Dispose() }
        Check ($stopped) 'Watcher also stops local split server'
        Write-AtomicJson (Join-Path $testPath 'verification.json') @{Checks=$script:Checks;Before=$before;After=$after;WatcherMilliseconds=$watch.ElapsedMilliseconds;NetworkTest=[bool]$LiveNetwork}
    }
} finally {
    try { if ($null -ne $lease) { $lease.Dispose() } }
    finally {
        if ($null -ne $bridge) { $bridge.Dispose() }
        if ($null -ne $before -and $null -ne $applied) { [void][FastOrangeCombined.ProxySettings]::RestoreOwned($before,$applied,$bridge.PacUrl) }
        if ($null -ne $controller) { if (-not $controller.HasExited) { Stop-Process -Id $controller.Id -Force -ErrorAction SilentlyContinue }; $controller.Dispose() }
        if ($locked) { $mutex.ReleaseMutex() }; if ($null -ne $mutex) { $mutex.Dispose() }
        if ($null -ne $proxyOwner) { $proxyOwner.Dispose() }
    }
}
Write-Host ('Verified '+$script:Checks+' checks. Evidence: '+$testPath)
