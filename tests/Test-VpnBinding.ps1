#requires -Version 5.1
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$bridgeSource=[IO.File]::ReadAllText((Join-Path $taskRoot '源码\DomainBridge.cs'),[Text.Encoding]::UTF8)
$harnessSource=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'VpnBindingHarness.cs'),[Text.Encoding]::UTF8)
# Internal test injection is available only because these sources form one assembly.
# All sockets in the harness are explicitly loopback; no system proxy/VPN changes.
Add-Type -TypeDefinition ($bridgeSource+[Environment]::NewLine+$harnessSource)
[void][FastOrangeCombined.Tests.VpnBindingHarness]::Run()
. (Join-Path $taskRoot '源码\CombinedRuntime.ps1')
# Mock local discovery, not the production network or system proxy.
$script:MockHalves=@('0.0.0.0/1','128.0.0.0/1')
$script:MockAddresses=@('192.168.255.7')
function Get-NetRoute {
    param($AddressFamily,$PolicyStore)
    [pscustomobject]@{DestinationPrefix='0.0.0.0/0';NextHop='192.168.31.1';InterfaceIndex=5;InterfaceAlias='mock-physical'}
    foreach($prefix in $script:MockHalves) { [pscustomobject]@{DestinationPrefix=$prefix;NextHop='192.168.255.1';InterfaceIndex=7;InterfaceAlias='mock-tap'} }
}
function Get-NetAdapter {
    param([switch]$Physical,[switch]$IncludeHidden)
    if($Physical){ [pscustomobject]@{ifIndex=5;Status='Up';InterfaceDescription='mock physical'} }
    else { [pscustomobject]@{ifIndex=7;Status='Up';InterfaceDescription='TAP-Windows mock';InterfaceGuid='00000000-0000-0000-0000-000000000007'} }
}
function Get-NetIPAddress {
    param($AddressFamily,$InterfaceIndex)
    if($InterfaceIndex -ne 7){throw 'Unexpected interface lookup.'}
    foreach($address in $script:MockAddresses) { [pscustomobject]@{IPAddress=$address;AddressState='Preferred'} }
}
$target=Get-BrowserTarget
if(-not $target.VpnReady -or $target.VpnIndex -ne 7 -or $target.VpnAddress -ne '192.168.255.7' -or $target.VpnGuid -ne '00000000-0000-0000-0000-000000000007'){throw 'Valid TAP identity was not propagated.'}
Write-Host 'PASS runtime discovery propagates unique TAP interface, Guid and preferred IPv4'
$script:MockHalves=@('0.0.0.0/1','0.0.0.0/1')
if((Get-BrowserTarget).VpnReady){throw 'Duplicate half-route prefixes must not enable VPN.'}
Write-Host 'PASS duplicate half routes fail closed'
$script:MockHalves=@('0.0.0.0/1','128.0.0.0/1');$script:MockAddresses=@('192.168.255.7','192.168.255.8')
$target=Get-BrowserTarget
if($target.VpnReady -or $target.VpnIndex -ne -1 -or $target.VpnAddress){throw 'Ambiguous VPN addresses must not enable binding.'}
Write-Host 'PASS ambiguous VPN IPv4 addresses fail closed'
$script:MockAddresses=@()
if((Get-BrowserTarget).VpnReady){throw 'Missing VPN address must not enable binding.'}
Write-Host 'PASS missing preferred VPN IPv4 fails closed'
