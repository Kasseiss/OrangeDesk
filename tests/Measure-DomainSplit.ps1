#requires -Version 5.1
param([string]$ExistingProxy,[string]$OutputPath,[int]$Repeats=1,[string[]]$Urls,[string]$CompareProxy)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $taskRoot '源码\CombinedRuntime.built.ps1')
$bridge=$null
if (-not $Urls) {
    $Urls=@(
        'https://www.baidu.com/',
        'https://www.zhihu.com/',
        'https://www.bilibili.com/',
        'https://www.taobao.com/',
        'https://www.jd.com/',
        'https://www.weibo.com/',
        'https://www.douyin.com/',
        'https://www.qq.com/',
        'https://www.updream.cn/',
        'https://s1.hdslb.com/bfs/static/jinkela/ai-tool/assets/index-BapZbw9z.js',
        'https://s1.hdslb.com/bfs/static/jinkela/ai-tool/assets/index-C9VpEQyg.css',
        'https://chatgpt.com/'
    )
}
try {
    if ($ExistingProxy) { $proxyUrl=$ExistingProxy; $mode='existing-proxy' }
    else {
        Initialize-Bridge
        $policy=[FastOrangeCombined.DomainPolicy]::new((Read-DomainRules),((Read-ChinaPrefixes) -join "`n"))
        $bridge=[FastOrangeCombined.BridgeServer]::new($policy,[guid]::NewGuid().ToString('N'))
        $port=$bridge.Start(); $target=Get-BrowserTarget; $bridge.Configure($target.Index,$target.VpnReady)
        $proxyUrl='http://127.0.0.1:'+$port; $mode='new-standalone-bridge'
    }
    $caseProxies=@($proxyUrl)
    if ($CompareProxy) { $caseProxies=@($CompareProxy,$proxyUrl) }
    foreach ($iteration in 1..$Repeats) {
        foreach ($url in $Urls) {
          foreach ($caseProxy in $caseProxies) {
            # Schannel still verifies chain/hostname; tolerate unavailable online CRL endpoints for comparable timing.
            $priorPreference=$ErrorActionPreference
            try {
                $ErrorActionPreference='Continue'
                $taskMetrics=@(& curl.exe --proxy $caseProxy --noproxy '""' --ssl-revoke-best-effort --max-time 20 --silent --show-error --output NUL --write-out 'status=%{http_code}|connect=%{time_connect}|tls=%{time_appconnect}|firstByte=%{time_starttransfer}|total=%{time_total}|bytes=%{size_download}|speed=%{speed_download}' $url 2>&1)
                $curlExit=$LASTEXITCODE
            } finally { $ErrorActionPreference=$priorPreference }
            $events=if ($null -ne $bridge -and $caseProxy -eq $proxyUrl) { @($bridge.DrainEvents()) } else { @() }
            $caseMode=if ($CompareProxy -and $caseProxy -eq $CompareProxy) {'comparison-old'} else {$mode}
            $row=[pscustomobject]@{Time=(Get-Date -Format o);Iteration=$iteration;Mode=$caseMode;Url=$url;Exit=$curlExit;Metrics=($taskMetrics -join "`n");Events=$events}
            $json=$row | ConvertTo-Json -Depth 4 -Compress
            if ($OutputPath) { [IO.File]::AppendAllText($OutputPath,$json+[Environment]::NewLine,$script:Utf8) }
            Write-Host $json
          }
        }
    }
} finally { if ($null -ne $bridge) { $bridge.Dispose() } }
