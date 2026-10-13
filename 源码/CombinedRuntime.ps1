#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Control','Worker','Ui','Preview','Restore')][string]$Role = 'Control',
    [string]$SessionPath,
    [int]$ControllerId = 0,
    [long]$ControllerTicks = 0,
    [int]$OwnerId = 0,
    [long]$OwnerTicks = 0,
    [switch]$Simulation,
    [int]$SimulationAppId = 0,
    [int]$RunSeconds = 0,
    [string]$WhitelistPath = (Join-Path $env:LOCALAPPDATA 'OrangeDesk\whitelist.json')
)
$ErrorActionPreference = 'Stop'
$script:LeaseSeconds = 90
$script:RenewSeconds = 20
$script:PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script:Utf8 = [Text.UTF8Encoding]::new($false)
$script:Root = Join-Path $env:LOCALAPPDATA 'FastOrangeCombined'
$script:UiProcess = $null
$script:UiSequence = 0
$script:Owned = @{}
$script:Paused = $false
$script:SeenApp = $false
$script:StopReason = ''
$script:FatalRouteError = $false
$script:Bridge = $null
$script:ProxyLease = $null
$script:Version = '2026.10.11.doh'
$script:LastDnsWarning = -999.0
$script:BridgePolicy = $null
$script:WhitelistRevision = 0
$script:WhitelistCount = 0
$script:WhitelistSignature = $null
$script:LastWhitelistCheck = -999.0
$script:LastWhitelistWarning = -999.0
$script:BrowserTarget = $null
$script:Stopwatch = [Diagnostics.Stopwatch]::StartNew()
$script:ConnectLabels = @('连接','立即连接','开始连接','点击连接','一键连接','CONNECT')
$script:ConnectedLabels = @('断开连接','断开','已连接','DISCONNECT','CONNECTED')
$script:BusyLabels = @('连接中','正在连接','正在连接...','正在连接…','断开中','正在断开','CONNECTING','DISCONNECTING')

# The build embeds public APNIC data and native code here. No adjacent file is needed by the delivered CMD.
$script:ChinaHash = '__CHINA_HASH__'
$script:ChinaGzip = '__CHINA_GZIP__'
$script:NativeCode = @'
__NATIVE_CODE__
'@
$script:DomainsHash = '__DOMAINS_HASH__'
$script:DomainsGzip = '__DOMAINS_GZIP__'
$script:BridgeCode = @'
__BRIDGE_CODE__
'@
$script:ProxyCode = @'
__PROXY_CODE__
'@

function Initialize-Routes {
    if (-not ('FastOrangeCombined.RouteApi' -as [type])) { Add-Type -TypeDefinition $script:NativeCode }
    [FastOrangeCombined.RouteApi]::CheckLayout()
}
function Read-ChinaPrefixes {
    $inputStream = [IO.MemoryStream]::new([Convert]::FromBase64String($script:ChinaGzip))
    $gzip = [IO.Compression.GZipStream]::new($inputStream,[IO.Compression.CompressionMode]::Decompress)
    $outputStream = [IO.MemoryStream]::new()
    try { $gzip.CopyTo($outputStream); $bytes = $outputStream.ToArray() }
    finally { $gzip.Dispose(); $inputStream.Dispose(); $outputStream.Dispose() }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','') }
    finally { $sha.Dispose() }
    if ($hash -cne $script:ChinaHash) { throw '内置国内网段校验失败。' }
    $prefixes = @([Text.Encoding]::UTF8.GetString($bytes) -split '\r?\n' | Where-Object { $_ })
    if ($prefixes.Count -ne 5495) { throw '内置网段数量异常。' }
    foreach ($prefix in $prefixes) {
        if ($prefix -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]{1,2})$') { throw '无效网段。' }
        $parts = $prefix.Split('/'); $length = [int]$parts[1]
        $b = [Net.IPAddress]::Parse($parts[0]).GetAddressBytes()
        $number = [uint64]$b[0]*16777216 + [uint64]$b[1]*65536 + [uint64]$b[2]*256 + $b[3]
        if ($b.Length -ne 4 -or $length -lt 8 -or $length -gt 32 -or $number % [Math]::Pow(2,32-$length) -ne 0) { throw '无效网段。' }
    }
    return $prefixes
}
function Write-AtomicJson([string]$Path,$Value) {
    $temp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $backup = $temp + '.previous'
    try {
        [IO.File]::WriteAllText($temp,($Value | ConvertTo-Json -Depth 8 -Compress),$script:Utf8)
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp,$Path,$backup,$true) }
        else { [IO.File]::Move($temp,$Path) }
    } finally {
        if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
        if ([IO.File]::Exists($backup)) { [IO.File]::Delete($backup) }
    }
}
function Read-Json([string]$Path) {
    if ([IO.File]::Exists($Path)) {
        for ($attempt=0;$attempt -lt 3;$attempt++) {
            $stream=$null; $reader=$null
            try {
                $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                $reader=[IO.StreamReader]::new($stream,$script:Utf8)
                return ($reader.ReadToEnd() | ConvertFrom-Json)
            } catch [IO.FileNotFoundException] { return $null }
            catch [IO.IOException] { if ($attempt -ge 2) { throw }; Start-Sleep -Milliseconds 20 }
            finally { if ($null -ne $reader) { $reader.Dispose() }; if ($null -ne $stream) { $stream.Dispose() } }
        }
    }
    return $null
}
function Read-DomainRules {
    $inputStream=[IO.MemoryStream]::new([Convert]::FromBase64String($script:DomainsGzip))
    $gzip=[IO.Compression.GZipStream]::new($inputStream,[IO.Compression.CompressionMode]::Decompress)
    $outputStream=[IO.MemoryStream]::new()
    try { $gzip.CopyTo($outputStream); $bytes=$outputStream.ToArray() }
    finally { $gzip.Dispose(); $inputStream.Dispose(); $outputStream.Dispose() }
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $hash=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','') } finally { $sha.Dispose() }
    if ($hash -cne $script:DomainsHash) { throw '内置国内域名规则校验失败。' }
    $rules=[Text.Encoding]::UTF8.GetString($bytes)
    $bundlePath=if ($SessionPath) { Read-Json (Join-Path $SessionPath 'bundle.json') } else { $null }
    if ($bundlePath -and $bundlePath.Path) {
        $extraPath=Join-Path ([IO.Path]::GetDirectoryName([string]$bundlePath.Path)) '补充直连域名.txt'
        if ([IO.File]::Exists($extraPath)) {
            foreach ($entry in [IO.File]::ReadAllLines($extraPath,[Text.Encoding]::UTF8)) {
                $entry=$entry.Trim()
                if (-not $entry -or $entry.StartsWith('#')) { continue }
                if ($entry -notmatch '^(?!-)[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$') { throw ('补充域名格式错误：'+$entry+'；每行只填域名，不填网址。') }
                $rules+="`n"+$entry
            }
        }
    }
    return $rules
}
function Initialize-Bridge {
    if (-not ('FastOrangeCombined.ProxySettings' -as [type])) { Add-Type -TypeDefinition $script:ProxyCode }
    if (-not ('FastOrangeCombined.DomainPolicy' -as [type])) { Add-Type -TypeDefinition $script:BridgeCode }
}
function Read-WhitelistSnapshot {
    $stream=$null
    try {
        try { $stream=[IO.FileStream]::new($WhitelistPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)) }
        catch [IO.FileNotFoundException] { return [pscustomobject]@{Revision=0;Hosts=[string[]]@();Modes=[string[]]@();Subdomains=[bool[]]@()} }
        catch [IO.DirectoryNotFoundException] { return [pscustomobject]@{Revision=0;Hosts=[string[]]@();Modes=[string[]]@();Subdomains=[bool[]]@()} }
        if ($stream.Length -gt 262144) { throw 'Whitelist is too large.' }
        # One extra byte detects a file which grew after its initial length check.
        $bytes=[byte[]]::new(262145); $total=0
        while ($total -lt $bytes.Length) {
            $read=$stream.Read($bytes,$total,$bytes.Length-$total)
            if ($read -eq 0) { break }
            $total+=$read
        }
        if ($total -gt 262144) { throw 'Whitelist is too large.' }
        $encoding=[Text.UTF8Encoding]::new($false,$true)
        $text=$encoding.GetString($bytes,0,$total).TrimStart([char]0xFEFF)
        # PS5.1 unwraps a one-element top-level array into its contained object.
        if (-not $text.TrimStart().StartsWith('{',[StringComparison]::Ordinal)) { throw 'Whitelist root must be an object.' }
        $value=ConvertFrom-Json -InputObject $text -ErrorAction Stop
        $names=@($value.PSObject.Properties.Name)
        if ($value -isnot [pscustomobject] -or @($value.PSObject.Properties).Count -ne 3 -or
            $names -cnotcontains 'version' -or $names -cnotcontains 'revision' -or $names -cnotcontains 'rules' -or
            ($value.version -isnot [int] -and $value.version -isnot [long]) -or $value.version -ne 1 -or
            ($value.revision -isnot [int] -and $value.revision -isnot [long]) -or $value.revision -lt 0 -or $value.revision -gt [int]::MaxValue -or
            $value.rules -isnot [Array] -or $value.rules.Count -gt 1000) { throw 'Invalid whitelist document.' }
        $hosts=[Collections.Generic.List[string]]::new(); $modes=[Collections.Generic.List[string]]::new(); $scopes=[Collections.Generic.List[bool]]::new()
        foreach ($rule in $value.rules) {
            $names=@($rule.PSObject.Properties.Name)
            if ($rule -isnot [pscustomobject] -or @($rule.PSObject.Properties).Count -ne 3 -or
                $names -cnotcontains 'host' -or $names -cnotcontains 'mode' -or $names -cnotcontains 'subdomains' -or
                $rule.host -isnot [string] -or $rule.mode -isnot [string] -or $rule.subdomains -isnot [bool]) { throw 'Invalid whitelist rule.' }
            $hosts.Add($rule.host); $modes.Add($rule.mode); $scopes.Add($rule.subdomains)
        }
        return [pscustomobject]@{Revision=[int]$value.revision;Hosts=$hosts.ToArray();Modes=$modes.ToArray();Subdomains=$scopes.ToArray()}
    } finally { if ($null -ne $stream) { $stream.Dispose() } }
}
function Write-BrowserSplitStatus {
    if ($null -eq $script:Bridge -or $null -eq $script:BrowserTarget) { return }
    Write-AtomicJson (Join-Path $SessionPath 'split-status.json') @{Direct=$script:Bridge.DirectCount;Vpn=$script:Bridge.VpnCount;Errors=$script:Bridge.ErrorCount;Active=$script:Bridge.ActiveCount;Physical=$script:BrowserTarget.Alias;VpnReady=$script:BrowserTarget.VpnReady;WhitelistRevision=$script:WhitelistRevision;WhitelistCount=$script:WhitelistCount}
}
function Sync-WhitelistOverrides([switch]$Initial,[switch]$Force) {
    if ($null -eq $script:BridgePolicy) { return }
    $now=$script:Stopwatch.Elapsed.TotalSeconds
    if (-not $Initial -and -not $Force -and $now-$script:LastWhitelistCheck -lt 1) { return }
    $script:LastWhitelistCheck=$now
    try {
        $info=[IO.FileInfo]::new($WhitelistPath); $info.Refresh()
        $signature=if ($info.Exists) { '{0}|{1}' -f $info.LastWriteTimeUtc.Ticks,$info.Length } else { 'missing' }
        if (-not $Initial -and -not $Force -and $signature -ceq $script:WhitelistSignature) { return }
        $snapshot=Read-WhitelistSnapshot
        # DomainPolicy validates every rule before publishing a complete snapshot.
        $script:BridgePolicy.SetOverrides($snapshot.Hosts,$snapshot.Modes,$snapshot.Subdomains)
        $script:WhitelistRevision=$snapshot.Revision; $script:WhitelistCount=$script:BridgePolicy.OverrideCount
        $script:WhitelistSignature=$signature
        if (-not $Initial) {
            Write-Status ('白名单已应用：版本 '+$script:WhitelistRevision+'，'+$script:WhitelistCount+' 条规则。')
            try { Write-BrowserSplitStatus } catch { }
        }
    } catch {
        if ($Initial) { throw '白名单文件格式错误或无法读取，分流未启动；请修正白名单后重试。' }
        # Avoid re-parsing an unchanged malformed file or logging its private data.
        if ($null -ne $signature) { $script:WhitelistSignature=$signature }
        if ($now-$script:LastWhitelistWarning -ge 60) {
            Write-Status '白名单更新未通过校验，继续使用上次有效规则。'
            $script:LastWhitelistWarning=$now
        }
    }
}
function Acquire-ProxyOwnerLock {
    [void][IO.Directory]::CreateDirectory($script:Root)
    try { return [IO.FileStream]::new((Join-Path $script:Root 'proxy-owner.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) }
    catch [IO.IOException] { throw '本用户已有临时分流在运行，请先关闭旧的脚本窗口。' }
}
function Convert-ProxyProfile($Value) {
    $profile=[FastOrangeCombined.ProxyProfile]::new()
    $profile.Flags=[uint32]$Value.Flags; $profile.Server=[string]$Value.Server; $profile.Bypass=[string]$Value.Bypass; $profile.AutoUrl=[string]$Value.AutoUrl
    $raw=[Collections.Generic.List[FastOrangeCombined.RawProxyValue]]::new()
    foreach ($item in $Value.Raw) {
        if ($item.Path -notin 'Software\Microsoft\Windows\CurrentVersion\Internet Settings','Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections' -or
            $item.Name -notin 'ProxyEnable','ProxyServer','ProxyOverride','AutoConfigURL','AutoDetect','DefaultConnectionSettings','SavedLegacySettings' -or
            ($item.Exists -and $item.Kind -notin 'DWord','String','ExpandString','Binary','QWord')) { throw '代理恢复记录格式错误，停止修改。' }
        $record=[FastOrangeCombined.RawProxyValue]::new()
        $record.Path=[string]$item.Path; $record.Name=[string]$item.Name; $record.Exists=[bool]$item.Exists; $record.Kind=[string]$item.Kind; $record.Data=[string]$item.Data
        $raw.Add($record)
    }
    $profile.Raw=$raw.ToArray()
    return $profile
}
function Recover-TemporaryProxy {
    $current=[FastOrangeCombined.ProxySettings]::Read()
    if ($current.AutoUrl -notmatch '^http://127\.0\.0\.1:[0-9]+/fastorange/([a-f0-9]{32})/proxy\.pac$') { return $false }
    $sessionId=$Matches[1]
    $backupPath=Join-Path (Join-Path (Join-Path $script:Root 'sessions') $sessionId) 'proxy-before.json'
    $saved=Read-Json $backupPath
    if (-not $saved -or $saved.PacUrl -cne $current.AutoUrl) { throw '未找到临时代理的恢复记录，停止自动修改。' }
    $before=Convert-ProxyProfile $saved.Before
    if ($saved.Applied) { $applied=Convert-ProxyProfile $saved.Applied }
    else { $applied=Convert-ProxyProfile $saved.Before; $applied.Flags=5; $applied.AutoUrl=$saved.PacUrl }
    return [FastOrangeCombined.ProxySettings]::RestoreOwned($before,$applied,$saved.PacUrl)
}
function Get-BrowserTarget {
    $routes=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore)
    $physical=@{}; foreach ($adapter in @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')) { $physical[[int]$adapter.ifIndex]=$adapter }
    $defaults=@($routes | Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -ne '0.0.0.0' -and $physical.ContainsKey([int]$_.InterfaceIndex) })
    if ($defaults.Count -ne 1) { throw '没有找到唯一的 Wi-Fi／网线出口，等待网络恢复。' }
    $halves=@($routes | Where-Object DestinationPrefix -in '0.0.0.0/1','128.0.0.0/1')
    $tap=@(Get-NetAdapter -IncludeHidden | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -like '*TAP-Windows*' })
    $vpnReady=$halves.Count -eq 2 -and $halves[0].InterfaceIndex -eq $halves[1].InterfaceIndex -and
        @($tap | Where-Object ifIndex -eq $halves[0].InterfaceIndex).Count -eq 1
    return [pscustomobject]@{Index=[int]$defaults[0].InterfaceIndex;Alias=$defaults[0].InterfaceAlias;Gateway=$defaults[0].NextHop;VpnReady=$vpnReady}
}
function Start-BrowserSplit {
    Initialize-Bridge
    $policy=[FastOrangeCombined.DomainPolicy]::new((Read-DomainRules),((Read-ChinaPrefixes) -join "`n"))
    $script:BridgePolicy=$policy
    Sync-WhitelistOverrides -Initial
    [void](Recover-TemporaryProxy)
    $token=[IO.Path]::GetFileName($SessionPath)
    if ($token -notmatch '^[a-f0-9]{32}$') { throw '会话目录格式错误。' }
    $script:Bridge=[FastOrangeCombined.BridgeServer]::new($policy,$token)
    [void]$script:Bridge.Start()
    $target=Get-BrowserTarget
    $script:BrowserTarget=$target
    $script:Bridge.Configure($target.Index,$target.VpnReady)
    $before=[FastOrangeCombined.ProxySettings]::Read()
    $saved=@{Version=2;PacUrl=$script:Bridge.PacUrl;Before=$before;Applied=$null}
    Write-AtomicJson (Join-Path $SessionPath 'proxy-before.json') $saved
    $script:ProxyLease=[FastOrangeCombined.ProxyLease]::new($before,$script:Bridge.PacUrl,$ControllerId,$ControllerTicks,(Join-Path $SessionPath 'stop'))
    $script:ProxyLease.AttachResource($script:Bridge)
    $script:ProxyLease.Start()
    $saved.Applied=$script:ProxyLease.Applied
    Write-AtomicJson (Join-Path $SessionPath 'proxy-before.json') $saved
    Write-BrowserSplitStatus
    Write-Status ('临时网站分流已开启：'+$policy.RuleCount+' 条域名规则；国内出口 '+$target.Alias+' / '+$target.Gateway)
    Write-Status ('版本 '+$script:Version+'；国内域名优先使用本地出口的加密 DNS，避免使用快橙解析出的远端节点。')
    Write-Status '适用于使用系统代理的浏览器和 HTTP 客户端；白名单优先于自动分流。关闭黑框恢复原代理设置。'
    $script:BrowserTargetKey=''
}
function Sync-BrowserSplit {
    Assert-Continue
    Sync-WhitelistOverrides
    if ([FastOrangeCombined.ProxySettings]::Read().AutoUrl -cne $script:Bridge.PacUrl) {
        $script:StopReason='系统代理已被外部更改；保留新的设置'
        throw [OperationCanceledException]::new($script:StopReason)
    }
    $target=Get-BrowserTarget
    $script:BrowserTarget=$target
    $script:Bridge.Configure($target.Index,$target.VpnReady)
    $key='{0}|{1}|{2}' -f $target.Index,$target.Gateway,$target.VpnReady
    if ($key -cne $script:BrowserTargetKey) {
        Write-Status ('分流出口：国内 '+$target.Alias+' / '+$target.Gateway+'；快橙路由就绪='+$target.VpnReady)
        $script:BrowserTargetKey=$key
    }
    foreach ($event in $script:Bridge.DrainEvents()) {
        Write-Status $event
        if ($event -match 'systemDNS-fallback/(degraded|cooldown)' -and $script:Stopwatch.Elapsed.TotalSeconds-$script:LastDnsWarning -gt 60) {
            Write-Status '本地解析暂时不可用，已临时使用系统结果；国内访问可能变慢，脚本会自动重试。'
            $script:LastDnsWarning=$script:Stopwatch.Elapsed.TotalSeconds
        }
    }
    Write-BrowserSplitStatus
}
function Write-Status([string]$Message) {
    $line = '{0:HH:mm:ss} {1}' -f (Get-Date),$Message
    try {
        $path = Join-Path $SessionPath 'monitor.log'
        if ([IO.File]::Exists($path) -and ([IO.FileInfo]$path).Length -gt 2MB) {
            Move-Item -LiteralPath $path -Destination ($path+'.previous') -Force
        }
        [IO.File]::AppendAllText($path,$line+[Environment]::NewLine,$script:Utf8)
    } catch { }
}
function Test-ProcessIdentity([int]$ProcessId,[long]$Ticks) {
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return (-not $process.HasExited -and $process.StartTime.ToUniversalTime().Ticks -eq $Ticks)
    } catch { return $false }
}
function Test-AppAlive {
    if ($Simulation) { return $null -ne (Get-Process -Id $SimulationAppId -ErrorAction SilentlyContinue) }
    return @(Get-Process -Name FastOrange -ErrorAction SilentlyContinue).Count -gt 0
}
function Test-Continue {
    if (-not (Test-ProcessIdentity $ControllerId $ControllerTicks)) { $script:StopReason='黑框已关闭'; return $false }
    if ([IO.File]::Exists((Join-Path $SessionPath 'stop'))) { $script:StopReason='用户停止'; return $false }
    if ($RunSeconds -gt 0 -and $script:Stopwatch.Elapsed.TotalSeconds -ge $RunSeconds) { $script:StopReason='观察时间结束'; return $false }
    if (Test-AppAlive) { $script:SeenApp=$true; return $true }
    if ($script:SeenApp) { $script:StopReason='快橙已退出'; return $false }
    if ($script:Stopwatch.Elapsed.TotalSeconds -gt 60) { $script:StopReason='60 秒内没有启动快橙'; return $false }
    return $true
}
function Assert-Continue {
    if (-not (Test-Continue)) { throw [OperationCanceledException]::new($script:StopReason) }
    Sync-WhitelistOverrides
}
function Start-HiddenRole([string]$ChildRole,[string[]]$Extra,[switch]$Elevate) {
    $arguments = '-NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "{0}" -Role {1} -SessionPath "{2}" -WhitelistPath "{3}" {4}' -f
        $PSCommandPath,$ChildRole,$SessionPath,$WhitelistPath,($Extra -join ' ')
    if ($Elevate) { return Start-Process -FilePath $script:PowerShell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru }
    return Start-Process -FilePath $script:PowerShell -ArgumentList $arguments -WindowStyle Hidden -PassThru
}
function Stop-UiHelper {
    if ($null -ne $script:UiProcess) {
        try {
            if (-not $script:UiProcess.HasExited) {
                Stop-Process -Id $script:UiProcess.Id -Force -ErrorAction Stop
                [void]$script:UiProcess.WaitForExit(3000)
            }
        } catch { }
        $script:UiProcess.Dispose(); $script:UiProcess=$null
    }
}
function Request-Ui([string]$Action,$Expected) {
    Assert-Continue
    $script:UiSequence++
    $sequence = $script:UiSequence
    Write-AtomicJson (Join-Path $SessionPath 'ui-request.json') @{Sequence=$sequence;Action=$Action;Expected=$Expected}
    if ($null -eq $script:UiProcess -or $script:UiProcess.HasExited) {
        Stop-UiHelper
        $ticks = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
        $script:UiProcess = Start-HiddenRole 'Ui' @('-OwnerId',$PID,'-OwnerTicks',$ticks,'-ControllerId',$ControllerId,'-ControllerTicks',$ControllerTicks)
    }
    $deadline = $script:Stopwatch.Elapsed.TotalSeconds + 8
    while ($script:Stopwatch.Elapsed.TotalSeconds -lt $deadline) {
        Assert-Continue
        if ($script:UiProcess.HasExited) { throw '界面检查进程退出，稍后重试。' }
        $result = Read-Json (Join-Path $SessionPath 'ui-response.json')
        if ($null -ne $result -and $result.Sequence -eq $sequence) {
            if ($result.Error) { throw [string]$result.Error }
            return $result.Value
        }
        Start-Sleep -Milliseconds 100
    }
    Stop-UiHelper
    throw '快橙界面检查超过 8 秒，已结束本次检查；不会强制重启快橙。'
}

function Initialize-Automation {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ('FastOrangeCombined.NativeWindows' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace FastOrangeCombined {
    public static class NativeWindows {
        private delegate bool Callback(IntPtr hwnd,IntPtr arg);
        [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback,IntPtr arg);
        [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent,Callback callback,IntPtr arg);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
        [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hwnd,uint flags);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd,StringBuilder text,int count);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd,StringBuilder text,int count);
        [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool ShowWindowAsync(IntPtr hwnd,int command);
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
        private static bool IsCompanionWindow(IntPtr hwnd) {
            StringBuilder title=new StringBuilder(256),windowClass=new StringBuilder(256);
            GetWindowText(hwnd,title,title.Capacity); GetClassName(hwnd,windowClass,windowClass.Capacity);
            return title.ToString().StartsWith("快橙伴航",StringComparison.Ordinal) &&
                windowClass.ToString().StartsWith("Qt",StringComparison.Ordinal);
        }
        private static IntPtr CompanionFor(IntPtr hwnd) {
            IntPtr root=GetAncestor(hwnd,2);
            return root!=IntPtr.Zero && root!=hwnd && IsCompanionWindow(root) ? root : IntPtr.Zero;
        }
        public static void OpenWindow(IntPtr hwnd) {
            if (hwnd == IntPtr.Zero) return;
            IntPtr companion=CompanionFor(hwnd);
            if (companion!=IntPtr.Zero) hwnd=companion;
            ShowWindowAsync(hwnd,9);
            SetForegroundWindow(hwnd);
        }
        public static IntPtr[] ForProcess(int id) {
            List<IntPtr> handles=new List<IntPtr>();
            HashSet<IntPtr> seen=new HashSet<IntPtr>();
            EnumWindows(delegate(IntPtr hwnd,IntPtr arg) { uint pid; GetWindowThreadProcessId(hwnd,out pid);
                if (pid==(uint)id && seen.Add(hwnd)) handles.Add(hwnd);
                // Inspect only companion metadata; UIA remains scoped to the target process roots.
                if (IsCompanionWindow(hwnd)) {
                    EnumChildWindows(hwnd,delegate(IntPtr child,IntPtr unused) {
                        uint childPid; GetWindowThreadProcessId(child,out childPid);
                        if (childPid!=(uint)id) return true;
                        // EnumChildWindows is recursive: omit native descendants of a target root.
                        for (IntPtr parent=GetAncestor(child,1);parent!=IntPtr.Zero && parent!=hwnd;parent=GetAncestor(parent,1)) {
                            uint parentPid; GetWindowThreadProcessId(parent,out parentPid);
                            if (parentPid==(uint)id) return true;
                        }
                        if (seen.Add(child)) handles.Add(child);
                        return true;
                    },IntPtr.Zero);
                }
                return true; },IntPtr.Zero);
            return handles.ToArray();
        }
    }
}
'@
    }
}
function Get-ButtonForControl($Control,[int]$ProcessId) {
    $element=$Control
    for ($i=0;$i -lt 5 -and $null -ne $element;$i++) {
        if ($element.Current.ProcessId -ne $ProcessId) { return $null }
        if ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) { return $element }
        $element=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($element)
    }
    return $null
}
function Get-StateForLabel([string]$Text,[bool]$Enabled) {
    if ($Text -in $script:ConnectLabels) { if ($Enabled) { return 'Disconnected' }; return 'Busy' }
    if ($Text -in $script:ConnectedLabels) { return 'Connected' }
    if ($Text -in $script:BusyLabels) { return 'Busy' }
    return 'Unknown'
}
function Get-ConnectionSnapshot {
    $result=[pscustomobject]@{State='Unavailable';Text='';Detail='';Identity='';Enabled=$false;ProcessId=0;Control=$null;Button=$null;MatchCount=-1}
    $condition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'ConnectBtnText')
    $matches=@{}; $failed=$false
    foreach ($app in @(Get-Process -Name FastOrange -ErrorAction SilentlyContinue)) {
        foreach ($handle in [FastOrangeCombined.NativeWindows]::ForProcess($app.Id)) {
            if ([FastOrangeCombined.NativeWindows]::IsHungAppWindow($handle)) { $failed=$true; continue }
            try {
                $window=[System.Windows.Automation.AutomationElement]::FromHandle($handle)
                foreach ($control in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)) {
                    if ($control.Current.ProcessId -ne $app.Id) { continue }
                    $identity='{0}:{1}:{2}' -f $app.Id,$app.StartTime.ToUniversalTime().Ticks,($control.GetRuntimeId() -join ',')
                    $matches[$identity]=[pscustomobject]@{Control=$control;Identity=$identity;ProcessId=$app.Id}
                }
            } catch { $failed=$true }
        }
    }
    if (-not $failed) { $result.MatchCount=$matches.Count }
    if ($failed -or $matches.Count -ne 1) {
        $result.Detail='未找到唯一且响应正常的连接控件；请保持登录并返回快橙主页。'; return $result
    }
    $match=@($matches.Values)[0]
    $result.Control=$match.Control; $result.Identity=$match.Identity; $result.ProcessId=$match.ProcessId
    $result.Text=$match.Control.Current.Name.Trim()
    $result.Button=Get-ButtonForControl $match.Control $match.ProcessId
    $result.Enabled=$null -ne $result.Button -and $result.Button.Current.IsEnabled -and $match.Control.Current.IsEnabled
    $result.State=Get-StateForLabel $result.Text $result.Enabled
    return $result
}
function Test-UiMayAct {
    return (Test-ProcessIdentity $OwnerId $OwnerTicks) -and (Test-ProcessIdentity $ControllerId $ControllerTicks) -and
        -not [IO.File]::Exists((Join-Path $SessionPath 'stop')) -and -not [IO.File]::Exists((Join-Path $SessionPath 'pause')) -and
        (Test-AppAlive)
}
function Prepare-HomePage {
    $snapshot=Get-ConnectionSnapshot
    if ($snapshot.MatchCount -ne 0) { return $snapshot }
    $candidates=@{}
    $condition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'主页')
    foreach ($app in @(Get-Process -Name FastOrange -ErrorAction SilentlyContinue)) {
        foreach ($handle in [FastOrangeCombined.NativeWindows]::ForProcess($app.Id)) {
            if ([FastOrangeCombined.NativeWindows]::IsHungAppWindow($handle)) { continue }
            $window=[System.Windows.Automation.AutomationElement]::FromHandle($handle)
            foreach ($homeControl in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)) {
                $element=$homeControl
                for ($depth=0;$depth -lt 5 -and $null -ne $element;$depth++) {
                    if ($element.Current.ProcessId -ne $app.Id) { break }
                    if ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem) {
                        $pattern=$null
                        if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) {
                            $key='{0}:{1}' -f $app.Id,($element.GetRuntimeId() -join ',')
                            $candidates[$key]=[pscustomobject]@{Element=$element;Pattern=$pattern}
                        }
                        break
                    }
                    $element=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($element)
                }
            }
        }
    }
    if ($candidates.Count -eq 1) {
        $candidate=@($candidates.Values)[0]
        $enabled=$candidate.Element.Current.IsEnabled
        if ($enabled -and (Test-UiMayAct)) {
            ([System.Windows.Automation.SelectionItemPattern]$candidate.Pattern).Select()
            Start-Sleep -Milliseconds 200
            return Get-ConnectionSnapshot
        }
    }
    return $snapshot
}
function Invoke-Connect($Expected) {
    if (-not (Test-ProcessIdentity $OwnerId $OwnerTicks) -or [IO.File]::Exists((Join-Path $SessionPath 'stop'))) { return $false }
    $fresh=Get-ConnectionSnapshot
    if ($fresh.State -ne 'Disconnected' -or -not $fresh.Enabled -or $fresh.Identity -cne $Expected.Identity -or $fresh.Text -cne $Expected.Text) { return $false }
    $pattern=$null
    if (-not $fresh.Button.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) { return $false }
    if ($fresh.Control.Current.Name.Trim() -cne $Expected.Text -or -not $fresh.Button.Current.IsEnabled) { return $false }
    # UIA getters above can block; check lifetime again immediately before invoking.
    if (-not (Test-UiMayAct)) { return $false }
    ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
    return $true
}
function Start-Ui {
    Initialize-Automation
    $last=0
    while (Test-ProcessIdentity $OwnerId $OwnerTicks) {
        if ([IO.File]::Exists((Join-Path $SessionPath 'stop'))) { break }
        $request=Read-Json (Join-Path $SessionPath 'ui-request.json')
        if ($null -ne $request -and $request.Sequence -ne $last) {
            $last=$request.Sequence; $value=$null; $failure=''
            try {
                if ($request.Action -eq 'Inspect') { $value=Get-ConnectionSnapshot | Select-Object State,Text,Detail,Identity,Enabled,ProcessId }
                elseif ($request.Action -eq 'Prepare') { $value=Prepare-HomePage | Select-Object State,Text,Detail,Identity,Enabled,ProcessId }
                elseif ($request.Action -eq 'Connect') { $value=Invoke-Connect $request.Expected }
                else { throw '未知界面命令。' }
            } catch { $failure=$_.Exception.Message }
            Write-AtomicJson (Join-Path $SessionPath 'ui-response.json') @{Sequence=$last;Value=$value;Error=$failure}
        }
        Start-Sleep -Milliseconds 100
    }
}
function New-ReconnectMemory { return @{CandidateSince=$null;HealthySince=$null;NextAttempt=0.0;Attempts=0;Identity='';Text=''} }
function Reset-Observation($Memory) { $Memory.CandidateSince=$null; $Memory.HealthySince=$null; $Memory.Identity=''; $Memory.Text='' }
function Get-ReconnectDecision($Memory,$Snapshot,[double]$Now) {
    $decision=[pscustomobject]@{Action='Wait';Attempt=0;Delay=0}
    if ($Snapshot.Identity -cne $Memory.Identity -or $Snapshot.Text -cne $Memory.Text) {
        Reset-Observation $Memory; $Memory.Identity=$Snapshot.Identity; $Memory.Text=$Snapshot.Text
    }
    if ($Snapshot.State -eq 'Connected') {
        $Memory.CandidateSince=$null
        if ($null -eq $Memory.HealthySince) { $Memory.HealthySince=$Now }
        if ($Now-$Memory.HealthySince -ge 30) { $Memory.Attempts=0; $Memory.NextAttempt=0.0 }
        return $decision
    }
    $Memory.HealthySince=$null
    if ($Snapshot.State -ne 'Disconnected') { $Memory.CandidateSince=$null; return $decision }
    if ($null -eq $Memory.CandidateSince) { $Memory.CandidateSince=$Now }
    if ($Now-$Memory.CandidateSince -lt 6 -or $Now -lt $Memory.NextAttempt) { return $decision }
    $delay=[int][Math]::Min(120,30*[Math]::Pow(2,[Math]::Min($Memory.Attempts,10)))
    $Memory.Attempts++; $Memory.NextAttempt=$Now+$delay; $Memory.CandidateSince=$null
    $decision.Action='Connect'; $decision.Attempt=$Memory.Attempts; $decision.Delay=$delay
    return $decision
}

function Get-LiveRoutes {
    if ($Simulation) { return @($script:FakeRoutes.Values) }
    return [FastOrangeCombined.RouteApi]::Read()
}
function Add-LeasedRoute($Spec) {
    if ($Simulation) { $script:FakeRoutes[$Spec.Key]=$Spec; return 0 }
    return [FastOrangeCombined.RouteApi]::Create($Spec,$script:LeaseSeconds)
}
function Renew-LeasedRoute($Spec) {
    if ($Simulation) {
        if ($Spec.Immortal -ne 0 -or $Spec.ValidLifetime -gt $script:LeaseSeconds) { return 1306 }
        $script:FakeRoutes[$Spec.Key]=$Spec; return 0
    }
    return [FastOrangeCombined.RouteApi]::Renew($Spec,$script:LeaseSeconds)
}
function Remove-LeasedRoute($Spec) {
    if ($Simulation) {
        $current=$script:FakeRoutes[$Spec.Key]
        if ($null -eq $current) { return 1168 }
        if (-not $Spec.CreationLeaseInvalid -and ($current.Immortal -ne 0 -or $current.ValidLifetime -gt $script:LeaseSeconds)) { return 1306 }
        $script:FakeRoutes.Remove($Spec.Key); return 0
    }
    return [FastOrangeCombined.RouteApi]::Remove($Spec)
}
function Write-Journal([string]$Event,$Route) {
    $record=@{Event=$Event;Route=$Route;Boot=$script:Boot;At=[DateTime]::UtcNow.ToString('o')}
    $script:Journal.WriteLine(($record | ConvertTo-Json -Depth 5 -Compress)); $script:Journal.Flush()
}
function Get-Target($Routes) {
    if ($Simulation) { return [pscustomobject]@{Index=5;Luid=[uint64]12345;Gateway='192.168.2.1';Guid='simulation';Alias='模拟 WLAN'} }
    $physical=@{}
    foreach ($adapter in @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')) { $physical[[int]$adapter.ifIndex]=$adapter }
    $defaults=@($Routes | Where-Object { $_.Prefix -eq '0.0.0.0/0' -and $_.Gateway -ne '0.0.0.0' -and $physical.ContainsKey([int]$_.Index) })
    if ($defaults.Count -ne 1) { throw '没有找到唯一的本地物理网络出口，分流等待网络恢复。' }
    $route=$defaults[0]
    $halves=@($Routes | Where-Object Prefix -in '0.0.0.0/1','128.0.0.0/1')
    $vpnAdapters=@(Get-NetAdapter -IncludeHidden | Where-Object {
        $_.InterfaceDescription -like '*TAP-Windows*' -and $_.Status -eq 'Up'
    })
    if ($halves.Count -ne 2 -or $halves[0].Luid -ne $halves[1].Luid -or $halves[0].Index -eq $route.Index -or
        @($vpnAdapters | Where-Object ifIndex -eq $halves[0].Index).Count -ne 1) { throw '等待快橙的 TAP 连接路由就绪。' }
    return [pscustomobject]@{Index=$route.Index;Luid=$route.Luid;Gateway=$route.Gateway;
        Guid=[string]$physical[[int]$route.Index].InterfaceGuid;Alias=[string]$physical[[int]$route.Index].Name}
}
function New-RouteSpec([string]$Prefix,$Target) {
    $spec=[FastOrangeCombined.RouteSpec]::new()
    $spec.Prefix=$Prefix; $spec.Index=$Target.Index; $spec.Luid=$Target.Luid; $spec.Gateway=$Target.Gateway
    $spec.Metric=$script:Metric; $spec.Protocol=3; $spec.ValidLifetime=$script:LeaseSeconds; $spec.Immortal=0
    return $spec
}
function Remove-OwnedRoutes {
    $live=@{}
    foreach ($route in @(Get-LiveRoutes)) { $live[$route.Key]=$route }
    $failed=0; $removed=0
    foreach ($key in @($script:Owned.Keys)) {
        if (-not $live.ContainsKey($key)) { $script:Owned.Remove($key); continue }
        try {
            $code=Remove-LeasedRoute $script:Owned[$key]
            if ($code -eq 1306) {
                $script:Owned.Remove($key)
                Write-Status ('保留已被外部修改的路由：'+$live[$key].Prefix)
                continue
            }
            if ($code -in 0,1168,2) {
                $script:Owned.Remove($key); $removed++
                try { Write-Journal 'Removed' $live[$key] } catch { Write-Status '路由已删除，但清理日志暂时无法写入。' }
            } else { $failed++ }
        } catch { $failed++ }
    }
    Write-Status ('已撤销 {0} 条本工具的路由；剩余 {1} 条。' -f $removed,$script:Owned.Count)
    return $failed
}
function Sync-SplitRoutes($Prefixes) {
    Assert-Continue
    $routes=@(Get-LiveRoutes)
    $target=Get-Target $routes
    $key='{0}|{1}|{2}|{3}' -f $target.Index,$target.Luid,$target.Gateway,$target.Guid
    if ($script:TargetKey -and $key -cne $script:TargetKey) {
        Write-Status '本地网络出口改变，正在撤销旧分流路由。'
        if ((Remove-OwnedRoutes) -gt 0) { throw '旧分流路由尚未清理完成，稍后重试。' }
        Assert-Continue; $routes=@(Get-LiveRoutes)
    }
    if ($key -cne $script:TargetKey) { Write-Status ('国内 IPv4 直连出口：'+$target.Alias+' / '+$target.Gateway) }
    $script:TargetKey=$key
    $live=@{}; $existing=@{}
    foreach ($route in $routes) { $live[$route.Key]=$route; $existing[$route.Prefix]=$true }
    foreach ($ownedKey in @($script:Owned.Keys)) { if (-not $live.ContainsKey($ownedKey)) { $script:Owned.Remove($ownedKey) } }
    $created=0; $renewed=0; $i=0
    foreach ($prefix in $Prefixes) {
        if (($i++ % 25) -eq 0) { Assert-Continue }
        $spec=New-RouteSpec $prefix $target
        if ($script:Owned.ContainsKey($spec.Key) -and $live.ContainsKey($spec.Key)) {
            $current=$live[$spec.Key]
            if ($current.Immortal -ne 0 -or $current.ValidLifetime -gt $script:LeaseSeconds) {
                $script:Owned.Remove($spec.Key)
                Write-Status ('保留已被外部修改的路由：'+$current.Prefix)
                continue
            }
            $code=Renew-LeasedRoute $current
            if ($code -eq 0) { $renewed++ }
            elseif ($code -eq 1306) { $script:Owned.Remove($spec.Key) }
            elseif ($code -in 1168,2) { $script:Owned.Remove($spec.Key); $existing.Remove($prefix) }
            else { throw ('更新分流路由失败：Windows 错误 '+$code) }
        }
        if (-not $existing.ContainsKey($prefix)) {
            # Record intent before the native call; only a successful create becomes owned.
            Write-Journal 'Intent' $spec
            $code=Add-LeasedRoute $spec
            if ($code -eq 0) {
                $script:Owned[$spec.Key]=$spec; $existing[$prefix]=$true; $created++
                Write-Journal 'Created' $spec
                if ($spec.CreationLeaseInvalid) {
                    $script:FatalRouteError=$true
                    throw 'Windows 未接受短期路由有效期，停止工具并撤销刚创建的路由。'
                }
            } elseif ($code -eq 5010) { $existing[$prefix]=$true }
            else { throw ('创建分流路由失败：Windows 错误 '+$code) }
        }
    }
    if ($created -gt 0) { Write-Status ('分流已开启：新增 '+$created+' 条；共维护 '+$script:Owned.Count+' 条临时路由。') }
    if ($created -gt 0 -and -not $Simulation) {
        foreach ($current in @(Get-LiveRoutes)) {
            if ($script:Owned.ContainsKey($current.Key) -and ($current.Immortal -ne 0 -or $current.ValidLifetime -gt $script:LeaseSeconds)) {
                throw 'Windows 没有保留指定的短期路由有效期，撤销分流。'
            }
        }
    }
    $script:LastRenew=$script:Stopwatch.Elapsed.TotalSeconds
    if ($Simulation) { Write-AtomicJson (Join-Path $SessionPath 'simulation-routes.json') @($script:FakeRoutes.Values) }
}

function Start-Worker {
    $locks=[Collections.Generic.List[object]]::new(); $result=0; $proxyOwner=$null
    try {
        $lockNames=if ($Simulation) { @('Local\FastOrangeCombinedSimulation-'+([IO.Path]::GetFileName($SessionPath))) }
                   else { @('Local\FastOrangeCombined','Local\FastOrangeReconnectTool','Local\FastOrangeSplitRoutes') }
        foreach ($name in $lockNames) {
            $mutex=[Threading.Mutex]::new($false,$name); $owned=$false
            try { $owned=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned=$true }
            if (-not $owned) { $mutex.Dispose(); throw '已有快橙监测或分流工具在运行，请先关闭旧工具。' }
            $locks.Add($mutex)
        }
        $script:TargetKey=''; $script:LastRenew=-999.0
        $memory=New-ReconnectMemory; $lastState=''; $lastError=''; $nextUi=0.0; $nextRoutes=0.0; $lastUi=0.0; $prepared=$false
        if ($Simulation) {
            Initialize-Routes
            $script:Metric=53000+(Get-Random -Minimum 0 -Maximum 2000)
            $script:Boot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
            $script:Journal=[IO.StreamWriter]::new((Join-Path $SessionPath 'routes.jsonl'),$true,$script:Utf8)
            $script:FakeRoutes=@{}
            $base=New-RouteSpec '10.0.0.0/8' (Get-Target @())
            $base.Metric=10; $base.Immortal=1; $script:FakeRoutes[$base.Key]=$base
            $prefixes=@('1.0.1.0/24','1.0.2.0/23','10.0.0.0/8')
        } else {
            $proxyOwner=Acquire-ProxyOwnerLock
            Start-BrowserSplit
        }
        Write-Status '工具已启动。只在按钮明确显示未连接时重连；国内网站使用本地出口。'
        Write-AtomicJson (Join-Path $SessionPath 'worker-status.json') @{State='Running';ProcessId=$PID;Version=$script:Version}
        while (Test-Continue) {
            if (-not $Simulation) { Sync-WhitelistOverrides }
            $now=$script:Stopwatch.Elapsed.TotalSeconds
            $pause=[IO.File]::Exists((Join-Path $SessionPath 'pause'))
            if ($pause -ne $script:Paused) {
                $script:Paused=$pause; Reset-Observation $memory
                Stop-UiHelper
                Write-Status $(if ($pause) {'自动重连已暂停，分流仍会维护。'} else {'自动重连已恢复。'})
                $nextUi=$now
            }
            if (-not $pause -and $now -ge $nextUi -and $script:SeenApp) {
                if ($now-$lastUi -gt 30) { Reset-Observation $memory }
                $lastUi=$now
                try {
                    $snapshot=if ($Simulation) { [pscustomobject]@{State='Connected';Text='已连接';Identity='fake';Enabled=$true;Detail=''} }
                              else {
                                  $action=if ($prepared) {'Inspect'} else {'Prepare'}
                                  $value=Request-Ui $action $null
                                  $prepared=$true
                                  $value
                              }
                    $status='{0} | {1} {2}' -f $snapshot.State,$snapshot.Text,$snapshot.Detail
                    if ($status -cne $lastState) {
                        Write-Status ('快橙状态：'+$status.Trim()); $lastState=$status
                        $nextRoutes=0.0
                    }
                    $decision=Get-ReconnectDecision $memory $snapshot $script:Stopwatch.Elapsed.TotalSeconds
                    if ($decision.Action -eq 'Connect' -and -not [IO.File]::Exists((Join-Path $SessionPath 'pause'))) {
                        if (Request-Ui 'Connect' $snapshot) { Write-Status ('已请求连接；'+$decision.Delay+' 秒内不重复请求。') }
                        else { Write-Status '按钮状态变化，本次未点击。' }
                    }
                } catch [OperationCanceledException] { throw }
                catch { Reset-Observation $memory; Write-Status ('界面检查：'+$_.Exception.Message) }
                $nextUi=$script:Stopwatch.Elapsed.TotalSeconds+3
            }
            $now=$script:Stopwatch.Elapsed.TotalSeconds
            if ($now -ge $nextRoutes -and $script:SeenApp) {
                try {
                    if ($Simulation) { Sync-SplitRoutes $prefixes }
                    else { Sync-BrowserSplit }
                    $lastError=''
                    $nextRoutes=$script:Stopwatch.Elapsed.TotalSeconds+$(if ($Simulation) { $script:RenewSeconds } else { 3 })
                } catch [OperationCanceledException] { throw }
                catch {
                    if ($script:FatalRouteError) { throw }
                    $message=$_.Exception.Message
                    if ($message -cne $lastError) { Write-Status ('分流：'+$message); $lastError=$message }
                    if ($Simulation -and $script:Owned.Count -gt 0) { [void](Remove-OwnedRoutes) }
                    elseif ($null -ne $script:Bridge) { $script:Bridge.Configure(-1,$false) }
                    $nextRoutes=$script:Stopwatch.Elapsed.TotalSeconds+3
                }
            }
            Start-Sleep -Milliseconds 150
        }
    } catch [OperationCanceledException] { }
    catch { Write-Status ('工具停止：'+$_.Exception.Message); $result=1 }
    finally {
        Stop-UiHelper
        Write-Status ('停止原因：'+$script:StopReason+'；正在恢复启动前的网络设置。')
        $cleanupError=''
        try {
            if ($null -ne $script:ProxyLease) {
                $script:ProxyLease.Dispose()
                if ($script:ProxyLease.LastError) { throw $script:ProxyLease.LastError }
                if ($script:ProxyLease.ExternalChangePreserved) { Write-Status '系统代理已经在外部更改，保留新的设置。' }
            }
            elseif ($null -ne $script:Bridge) { $script:Bridge.Dispose() }
            if ($script:Owned.Count -gt 0) {
                for ($attempt=0;$attempt -lt 3 -and $script:Owned.Count -gt 0;$attempt++) {
                    [void](Remove-OwnedRoutes)
                    if ($script:Owned.Count -gt 0) { Start-Sleep -Milliseconds 250 }
                }
                if ($script:Owned.Count -gt 0) {
                    $cleanupError='部分临时路由删除失败，将在最后一次刷新后约 90 秒内失效。'
                    if (@($script:Owned.Values | Where-Object CreationLeaseInvalid).Count -gt 0) {
                        $cleanupError='Windows 拒绝短期有效期且路由删除失败，请保留 routes.jsonl 并联系维护者，或重启电脑清除本次临时路由。'
                    }
                    $result=1
                }
            }
        } catch {
            $cleanupError='恢复网络设置遇到错误：'+$_.Exception.Message+'；请保留本次 proxy-before.json 和日志。'; $result=1
            if ($null -ne $script:Bridge) { $script:Bridge.Dispose() }
        }
        if ($cleanupError) { Write-Status $cleanupError }
        if ($Simulation -and $null -ne $script:FakeRoutes) { Write-AtomicJson (Join-Path $SessionPath 'simulation-routes.json') @($script:FakeRoutes.Values) }
        if ($null -ne $script:Journal) { $script:Journal.Dispose() }
        foreach ($mutex in $locks) { $mutex.ReleaseMutex(); $mutex.Dispose() }
        if ($null -ne $proxyOwner) { $proxyOwner.Dispose() }
        Write-Status '自动重连与分流已停止，快橙保持原有运行状态。'
        try { Write-AtomicJson (Join-Path $SessionPath 'worker-status.json') @{State='Stopped';Result=$result;Remaining=$script:Owned.Count;Error=$cleanupError;Reason=$script:StopReason;ProxyRestored=($null -ne $script:ProxyLease -and $script:ProxyLease.Restored)} } catch { }
    }
    return $result
}
function Open-FastOrange {
    if (Test-AppAlive) {
        Initialize-Automation
        $apps=@(Get-Process -Name FastOrange -ErrorAction SilentlyContinue)
        if ($apps.Count -eq 1) { [FastOrangeCombined.NativeWindows]::OpenWindow($apps[0].MainWindowHandle) }
        return
    }
    $shortcut=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\FastOrange\FastOrange.appref-ms'
    if (-not [IO.File]::Exists($shortcut)) { throw '未找到已安装的快橙快捷方式，请先手动打开快橙，再运行本工具。' }
    Start-Process -FilePath $shortcut
}
function Read-ControlKey {
    try { if (-not [Console]::IsInputRedirected -and [Console]::KeyAvailable) { return [Console]::ReadKey($true).Key.ToString() } } catch { }
    return ''
}
function Start-Control {
    $worker=$null; $tail=$null; $reader=$null; $result=0
    Write-Host '快橙一键启动 | 自动重连 + 国内网站直连' -ForegroundColor Cyan
    Write-Host '关闭黑框或按 Q：停止工具并恢复原代理设置；快橙继续运行。'
    Write-Host 'P：暂停/恢复自动重连。快橙从托盘退出后，本窗口也会结束。'
    Write-Host '请保持快橙已登录。分流只在本窗口运行期间启用，无需管理员权限。'
    try {
        Open-FastOrange
        $ticks=(Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
        $worker=Start-HiddenRole 'Worker' @('-ControllerId',$PID,'-ControllerTicks',$ticks)
        $deadline=[DateTime]::UtcNow.AddSeconds(45)
        while (-not [IO.File]::Exists((Join-Path $SessionPath 'worker-status.json'))) {
            if ($worker.HasExited) { throw '启动失败，详细信息保存在本次日志中。' }
            if ([DateTime]::UtcNow -gt $deadline) { throw '工具启动超时，正在停止后台监测。' }
            Start-Sleep -Milliseconds 150
        }
        $tail=[IO.FileStream]::new((Join-Path $SessionPath 'monitor.log'),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        $reader=[IO.StreamReader]::new($tail,$script:Utf8)
        $paused=$false
        while (-not $worker.HasExited) {
            $line=$reader.ReadLine()
            while ($null -ne $line) { Write-Host $line; $line=$reader.ReadLine() }
            $key=Read-ControlKey
            if ($key -eq 'Q') { break }
            if ($key -eq 'P') {
                $paused=-not $paused; $pausePath=Join-Path $SessionPath 'pause'
                if ($paused) { [IO.File]::WriteAllText($pausePath,'pause',$script:Utf8) }
                elseif ([IO.File]::Exists($pausePath)) { [IO.File]::Delete($pausePath) }
            }
            Start-Sleep -Milliseconds 150
        }
    } catch {
        Write-Host ('未完成：'+$_.Exception.Message) -ForegroundColor Red
        Write-Host ('本次日志：'+(Join-Path $SessionPath 'monitor.log'))
        $result=1
    } finally {
        [IO.File]::WriteAllText((Join-Path $SessionPath 'stop'),'stop',$script:Utf8)
        if ($null -ne $worker) { [void]$worker.WaitForExit(15000) }
        if ($null -ne $reader) {
            $line=$reader.ReadLine()
            while ($null -ne $line) { Write-Host $line; $line=$reader.ReadLine() }
            $reader.Dispose(); $tail.Dispose()
        } elseif ([IO.File]::Exists((Join-Path $SessionPath 'monitor.log'))) { Write-Host ([IO.File]::ReadAllText((Join-Path $SessionPath 'monitor.log'),$script:Utf8)) }
        $status=Read-Json (Join-Path $SessionPath 'worker-status.json')
        if ($null -ne $status -and $status.State -eq 'Stopped') { $result=[Math]::Max($result,[int]$status.Result) }
        if ($null -ne $worker) { $worker.Dispose() }
    }
    if ($result -ne 0) { Write-Host '请查看提示。此窗口会在 12 秒后关闭。'; Start-Sleep -Seconds 12 }
    return $result
}
function Show-Preview {
    Initialize-Bridge
    $policy=[FastOrangeCombined.DomainPolicy]::new((Read-DomainRules),((Read-ChinaPrefixes) -join "`n"))
    $script:BridgePolicy=$policy
    Sync-WhitelistOverrides -Initial
    $target=Get-BrowserTarget
    [pscustomobject]@{Mode='只读预览：域名分流 + 指定本地接口';Version=$script:Version;DomesticDns='物理接口 HTTPS DNS';DomainRules=$policy.RuleCount;ChinaIpPrefixes=$policy.PrefixCount;WhitelistRevision=$script:WhitelistRevision;WhitelistCount=$script:WhitelistCount;Interface=$target.Alias;Gateway=$target.Gateway;VpnReady=$target.VpnReady;ZhihuDirect=$policy.IsDirect('www.zhihu.com');ChatGPTDirect=$policy.IsDirect('chatgpt.com');RouteChanges=0;ProxyChanges=0} | Format-List | Out-Host
}
function Restore-TemporarySplit {
    Initialize-Bridge
    $mutex=[Threading.Mutex]::new($false,'Local\FastOrangeCombined'); $owned=$false; $proxyOwner=$null
    try {
        try { $owned=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned=$true }
        if (-not $owned) { throw '工具仍在运行，请先关闭黑框，再执行恢复。' }
        $proxyOwner=Acquire-ProxyOwnerLock
        if (Recover-TemporaryProxy) { Write-Host '已恢复本工具启动前的代理设置。' }
        else { Write-Host '当前没有本工具遗留的临时代理，无需恢复。' }
    } finally { if ($null -ne $proxyOwner) { $proxyOwner.Dispose() }; if ($owned) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
}

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $SessionPath -and $Role -notin 'Preview','Restore') { throw '缺少会话目录，请从一键 CMD 启动。' }
    switch ($Role) {
        'Control' { exit (Start-Control) }
        'Worker' { exit (Start-Worker) }
        'Ui' { Start-Ui; exit 0 }
        'Preview' { Show-Preview; exit 0 }
        'Restore' { Restore-TemporarySplit; exit 0 }
    }
}
