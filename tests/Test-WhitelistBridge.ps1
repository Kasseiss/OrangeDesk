#requires -Version 5.1
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
# Load source directly: no generated bundle and no system proxy or route mutation.
$bridgeSource=[IO.File]::ReadAllText((Join-Path $taskRoot '源码\DomainBridge.cs'),[Text.Encoding]::UTF8)
$harnessSource=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'WhitelistBridgeHarness.cs'),[Text.Encoding]::UTF8)
Add-Type -TypeDefinition ($bridgeSource+[Environment]::NewLine+$harnessSource)
[void][FastOrangeCombined.Tests.WhitelistBridgeHarness]::Run()
. (Join-Path $taskRoot '源码\CombinedRuntime.ps1')
$script:Checks=0
function Check([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw ('FAIL '+$Message) }
    $script:Checks++; Write-Host ('PASS '+$Message)
}
$tempRoot=Join-Path $env:TEMP ('OrangeWhitelistTests-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($tempRoot)
$SessionPath=$tempRoot; $WhitelistPath=Join-Path $tempRoot 'whitelist.json'
$script:BridgePolicy=[FastOrangeCombined.DomainPolicy]::new('example.com','127.0.0.0/8')
$script:Bridge=[pscustomobject]@{DirectCount=0;VpnCount=0;ErrorCount=0;ActiveCount=0}
$script:BrowserTarget=[pscustomobject]@{Alias='loopback-test';VpnReady=$true}
function Save-Whitelist([int]$Revision,$Rules) { Write-AtomicJson $WhitelistPath @{version=1;revision=$Revision;rules=@($Rules)} }
try {
    Sync-WhitelistOverrides -Initial
    Check ($script:WhitelistRevision -eq 0 -and $script:WhitelistCount -eq 0) 'missing whitelist means empty rules'
    Save-Whitelist 1 @(@{host='example.com';mode='vpn';subdomains=$true})
    Sync-WhitelistOverrides -Force
    $status=Read-Json (Join-Path $SessionPath 'split-status.json')
    Check ($script:BridgePolicy.GetOverride('a.example.com') -ceq 'vpn' -and $status.WhitelistRevision -eq 1 -and $status.WhitelistCount -eq 1) 'atomic file snapshot applies and publishes accepted revision/count'
    [IO.File]::WriteAllText($WhitelistPath,'{"version":1,"revision":2,"rules":[{"host":"example.com","mode":"direct","subdomains":"false"}]}',$script:Utf8)
    Sync-WhitelistOverrides -Force
    Check ($script:WhitelistRevision -eq 1 -and $script:BridgePolicy.GetOverride('example.com') -ceq 'vpn') 'malformed refresh retains last-good snapshot and revision'
    $log=[IO.File]::ReadAllText((Join-Path $SessionPath 'monitor.log'),$script:Utf8)
    Sync-WhitelistOverrides -Force
    Check ([IO.File]::ReadAllText((Join-Path $SessionPath 'monitor.log'),$script:Utf8) -ceq $log) 'malformed refresh warning is bounded'
    Save-Whitelist 3 @(@{host='example.com';mode='direct';subdomains=$false})
    Sync-WhitelistOverrides -Force
    Check ($script:WhitelistRevision -eq 3 -and $script:BridgePolicy.GetOverride('example.com') -ceq 'direct' -and $script:BridgePolicy.GetOverride('a.example.com') -ceq '') 'valid refresh replaces the entire old snapshot'
    Save-Whitelist 4 @()
    Sync-WhitelistOverrides -Force
    Check ($script:WhitelistRevision -eq 4 -and $script:WhitelistCount -eq 0 -and $script:BridgePolicy.IsDirect('a.example.com')) 'atomic removal restores automatic rules'
    Save-Whitelist 5 @(@{host='127.0.0.1';mode='vpn';subdomains=$false})
    Sync-WhitelistOverrides -Force
    [IO.File]::Delete($WhitelistPath); Sync-WhitelistOverrides -Force
    Check ($script:WhitelistRevision -eq 0 -and $script:WhitelistCount -eq 0 -and $script:BridgePolicy.GetOverride('127.0.0.1') -ceq '') 'file deletion removes all overrides'
    Save-Whitelist 6 @(@{host='localhost';mode='vpn';subdomains=$false})
    $reloadWatch=[Diagnostics.Stopwatch]::StartNew()
    do { Sync-WhitelistOverrides; if ($script:WhitelistRevision -eq 6) { break }; Start-Sleep -Milliseconds 150 } while ($reloadWatch.Elapsed.TotalSeconds -lt 2)
    Check ($script:WhitelistRevision -eq 6 -and $reloadWatch.Elapsed.TotalSeconds -lt 2) 'periodic metadata checks apply live update in about one second'
    Save-Whitelist ([int]::MaxValue) @()
    Sync-WhitelistOverrides -Force
    Check ($script:WhitelistRevision -eq [int]::MaxValue -and $script:WhitelistCount -eq 0) 'maximum supported Int32 revision applies without rounding'
    [IO.File]::Delete($WhitelistPath); Sync-WhitelistOverrides -Force
    [IO.File]::WriteAllText($WhitelistPath,(' ' * 262145),$script:Utf8)
    Sync-WhitelistOverrides -Force
    Check ($script:WhitelistCount -eq 0 -and $script:WhitelistRevision -eq 0) 'reader rejects files exceeding 256 KiB'
    foreach ($invalid in @(
        '{"version":1,"revision":-1,"rules":[]}',
        '{"version":1,"revision":1.5,"rules":[]}',
        '{"version":1,"revision":2147483648,"rules":[]}',
        '{"version":1,"revision":6,"rules":null}',
        '[{"version":1,"revision":6,"rules":[]}]',
        '{"VERSION":1,"revision":6,"rules":[]}',
        '{"version":1,"revision":6,"rules":[{"host":"API.OPENAI.COM","mode":"direct","subdomains":false}]}',
        '{"version":1,"revision":6,"rules":[{"host":"example.com","mode":"DIRECT","subdomains":false}]}',
        '{"version":1,"revision":6,"rules":[],"extra":true}',
        'not-json'
    )) {
        [IO.File]::WriteAllText($WhitelistPath,$invalid,$script:Utf8)
        $rejected=$false; try { Sync-WhitelistOverrides -Initial } catch { $rejected=$true }
        Check ($rejected -and $script:WhitelistRevision -eq 0) 'invalid initial schema refuses application'
    }
    # Mock only the startup boundary; any proxy recovery/application call fails this test.
    function Initialize-Bridge { }
    function Read-DomainRules { return '' }
    function Read-ChinaPrefixes { return @() }
    $script:RecoverCalls=0
    function Recover-TemporaryProxy { $script:RecoverCalls++; throw 'Proxy mutation is forbidden in this test.' }
    $script:Bridge=$null
    $rejected=$false; try { Start-BrowserSplit } catch { $rejected=$true }
    Check ($rejected -and $script:RecoverCalls -eq 0 -and $null -eq $script:Bridge) 'initial malformed whitelist fails before proxy recovery or bridge startup'
    Write-Host ('ALL '+$script:Checks+' WHITELIST RUNTIME CHECKS PASSED')
} finally {
    # Delete only this test-created directory, after verifying its resolved boundary.
    $resolved=[IO.Path]::GetFullPath($tempRoot)
    $tempBoundary=[IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')+'\'
    if ($resolved.StartsWith($tempBoundary,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($resolved).StartsWith('OrangeWhitelistTests-')) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
