#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SessionDir,
    [ValidateRange(1,1440)][int]$DurationMinutes = 1440,
    [ValidateRange(10,600)][int]$PostIncidentSeconds = 120,
    [switch]$SelfTest,
    [switch]$Continuous,
    [string]$CompanionSessionPath
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$SessionDir = [IO.Path]::GetFullPath($SessionDir)
[IO.Directory]::CreateDirectory($SessionDir) | Out-Null
$stopPath = Join-Path $SessionDir 'stop.request'
$statusPath = Join-Path $SessionDir 'status.json'
$logPath = Join-Path $SessionDir 'observations.jsonl'
$started = [DateTimeOffset]::Now
$deadline = $started.AddMinutes($DurationMinutes)
$script:incidentAt = $null
$script:lastError = $null
$script:operationErrors = @{}
$script:incidentLayer = $null
$script:lastWifi = @()
$script:claudePresent = $false
$script:totalBytes = 0L
$script:state = 'Starting'
$utf8 = [Text.UTF8Encoding]::new($false)
function Save-JsonAtomic([string]$Path, $Value) {
    $temp = "$Path.$PID.tmp"
    [IO.File]::WriteAllText($temp, (ConvertTo-Json -InputObject $Value -Depth 30), $utf8)
    if ([IO.File]::Exists($Path)) {[IO.File]::Replace($temp,$Path,[NullString]::Value)} else {[IO.File]::Move($temp,$Path)}
}
function Move-OwnedLog([string]$Source,[string]$Destination) {
    if ([IO.File]::Exists($Destination)) {[IO.File]::Delete($Destination)}
    [IO.File]::Move($Source,$Destination)
}
function Append-Incident($Data) {
    $path=Join-Path $SessionDir 'incidents.jsonl'
    if ([IO.File]::Exists($path) -and ([IO.FileInfo]$path).Length -ge 1MB) {
        for($i=3;$i -ge 1;$i--) {
            $old="$path.$i"
            if([IO.File]::Exists($old)) {
                if($i -eq 3){[IO.File]::Delete($old)}else{Move-OwnedLog $old "$path.$($i+1)"}
            }
        }
        Move-OwnedLog $path "$path.1"
    }
    [IO.File]::AppendAllText($path,((ConvertTo-Json -InputObject $Data -Depth 30 -Compress)+"`n"),$utf8)
}
function Write-Observation([string]$Kind, $Data) {
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -ge 10MB) {
        for ($i = 3; $i -ge 1; $i--) {
            $old = "$logPath.$i"
            if (Test-Path -LiteralPath $old) {
                if ($i -eq 3) { Remove-Item -LiteralPath $old -Force }
                else { Move-OwnedLog $old "$logPath.$($i+1)" }
            }
        }
        Move-OwnedLog $logPath "$logPath.1"
    }
    $line = ConvertTo-Json -InputObject ([ordered]@{At=[DateTimeOffset]::Now.ToString('o');Kind=$Kind;Data=$Data}) -Depth 30 -Compress
    [IO.File]::AppendAllText($logPath, "$line`n", $utf8)
    $script:totalBytes += $utf8.GetByteCount("$line`n")
}
function Record-Error([string]$Operation, $Exception) {
    $script:lastError = "$Operation : $($Exception.Exception.Message)"
    $script:operationErrors[$Operation] = $script:lastError
    Write-Observation 'error' @{Operation=$Operation;Message=$Exception.Exception.Message}
}
function Clear-OperationError([string]$Operation) {
    $script:operationErrors.Remove($Operation)
    if ($script:lastError -like "$Operation : *") {
        $script:lastError = @($script:operationErrors.Values | Select-Object -Last 1)[0]
    }
}
function Read-SharedJson([string]$Path) {
    for ($attempt=0; $attempt -lt 3; $attempt++) {
        $stream=$null; $reader=$null
        try {
            $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
            return ($reader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop)
        } catch {
            if ($attempt -eq 2) { throw }
            Start-Sleep -Milliseconds (50 * ($attempt+1))
        } finally {
            if ($reader) {$reader.Dispose()} elseif ($stream) {$stream.Dispose()}
        }
    }
}
function Save-Status {
    Save-JsonAtomic $statusPath ([ordered]@{
        State=$script:state;StartedAt=$started.ToString('o');UpdatedAt=[DateTimeOffset]::Now.ToString('o')
        Deadline=$deadline.ToString('o');Pid=$PID;Wifi=$script:lastWifi;ClaudePresent=$script:claudePresent
        IncidentAt=$(if($script:incidentAt){$script:incidentAt.ToString('o')}else{$null})
        Continuous=[bool]$Continuous;CompanionSessionPath=$CompanionSessionPath;IncidentLayer=$script:incidentLayer;LastError=$script:lastError;OperationErrors=$script:operationErrors;TotalJsonlBytesWritten=$script:totalBytes
    })
}
function Set-Incident([string]$Reason, $Evidence, [string]$Layer='PhysicalWifi') {
    if (-not $Continuous -and $null -ne $script:incidentAt) { return }
    $script:incidentAt = [DateTimeOffset]::Now
    $script:incidentLayer=$Layer
    $note=if($Layer -eq 'VPN') {'VPN-layer trigger; a planned node switch can cause this. This is not evidence of physical Wi-Fi failure or causal attribution.'} else {'This is a trigger, not a causal attribution.'}
    $data = [ordered]@{At=$script:incidentAt.ToString('o');Layer=$Layer;Reason=$Reason;Evidence=$Evidence;PostIncidentSeconds=$PostIncidentSeconds;Note=$note}
    Save-JsonAtomic (Join-Path $SessionDir 'incident.json') $data
    Append-Incident $data
    Write-Observation 'incident' $data
    $script:state = $(if($Continuous){'Running'}else{'PostIncident'})
}
$physicalIds = @{}
$interfaceIndexCache = @{}
$previousWifi = @{}
$previousTunnels = @{}
$script:lastVpnReady = $null
$script:vpnNotReadySince = $null
$script:vpnNotReadyTriggered = $false
$missingGateway = @{}
$eventCursors = @{}
$stateOffsets = @{}
$statePending = @{}
$lastSplitPath = $null
$lastSplitWrite = $null
$orangeRoot = Join-Path $env:USERPROFILE '.OrangeDesk\network\sessions'
if($CompanionSessionPath){$CompanionSessionPath=[IO.Path]::GetFullPath($CompanionSessionPath)}
$vpnLogRoot = Join-Path $env:APPDATA 'FastOrange\Logs'
function Read-NetworkSnapshot {
    $result = @()
    foreach ($nic in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        $isWifi = ($nic.NetworkInterfaceType -eq [Net.NetworkInformation.NetworkInterfaceType]::Wireless80211) -and ($physicalIds.Count -eq 0 -or $physicalIds.ContainsKey($nic.Id.Trim('{}').ToLowerInvariant()))
        $isTunnel = "$($nic.Name) $($nic.Description)" -match '(?i)\bTAP\b|TAP-Windows|Wintun|OpenVPN|TUN\b'
        if (-not ($isWifi -or $isTunnel)) { continue }
        $ips = $nic.GetIPProperties()
        $interfaceIndex=$null
        try {$interfaceIndex=$ips.GetIPv4Properties().Index} catch {
            # A disabled/unconfigured TAP can lack IPv4 properties. Keep observing
            # its Down state and use interface metadata, rather than losing the sample.
            if (-not $interfaceIndexCache.ContainsKey($nic.Id)) {
                $adapter=Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
                    Where-Object {([string]$_.InterfaceGuid).Trim('{}') -eq $nic.Id.Trim('{}')} | Select-Object -First 1
                if($adapter){$interfaceIndexCache[$nic.Id]=[int]$adapter.ifIndex}
            }
            $interfaceIndex=$interfaceIndexCache[$nic.Id]
        }
        if($null -ne $interfaceIndex){$interfaceIndexCache[$nic.Id]=$interfaceIndex}
        $result += [pscustomobject]@{
            Kind=$(if($isWifi){'PhysicalWifi'}else{'Tunnel'});Name=$nic.Name;Guid=$nic.Id;Description=$nic.Description
            OperationalStatus=[string]$nic.OperationalStatus;Speed=$nic.Speed
            InterfaceIndex=$interfaceIndex
            IPv4=@($ips.UnicastAddresses | Where-Object {$_.Address.AddressFamily -eq 'InterNetwork'} | ForEach-Object {$_.Address.ToString()})
            IPv4Gateway=@($ips.GatewayAddresses | Where-Object {$_.Address.AddressFamily -eq 'InterNetwork' -and $_.Address.ToString() -ne '0.0.0.0'} | ForEach-Object {$_.Address.ToString()})
        }
    }
    return $result
}
function Sample-Network {
    $all = @(Read-NetworkSnapshot)
    $wifi = @($all | Where-Object Kind -eq 'PhysicalWifi')
    $script:lastWifi = $wifi
    Write-Observation 'interfaces' $all
    foreach ($item in $wifi) {
        $id = $item.Guid
        if ($previousWifi.ContainsKey($id)) {
            $old = $previousWifi[$id]
            if ($old.OperationalStatus -eq 'Up' -and $item.OperationalStatus -ne 'Up') {
                Set-Incident 'Physical WLAN changed from Up to non-Up' @{Before=$old;After=$item}
            }
            if ($item.IPv4Gateway.Count -eq 0 -and ($old.IPv4Gateway.Count -gt 0 -or $missingGateway.ContainsKey($id))) {
                $missingGateway[$id] = 1 + [int]$missingGateway[$id]
                if ($missingGateway[$id] -eq 2) { Set-Incident 'Physical WLAN IPv4 gateway disappeared in two consecutive samples' @{Before=$old;After=$item} }
            } else { $missingGateway.Remove($id) }
        }
        $previousWifi[$id] = $item
    }
    foreach ($id in @($previousWifi.Keys)) {
        if ($id -notin @($wifi.Guid) -and $previousWifi[$id].OperationalStatus -eq 'Up') {
            Set-Incident 'Previously Up physical WLAN adapter disappeared' @{Before=$previousWifi[$id]}
            $previousWifi.Remove($id)
        }
    }
    Update-TunnelState @($all | Where-Object Kind -eq 'Tunnel')
}
function Update-TunnelState($Tunnels) {
    $tunnels=@($Tunnels)
    foreach ($item in $tunnels) {
        if ($previousTunnels.ContainsKey($item.Guid) -and $previousTunnels[$item.Guid].OperationalStatus -eq 'Up' -and $item.OperationalStatus -ne 'Up') {
            Set-Incident 'Previously Up VPN tunnel changed to non-Up; possibly a planned node switch' @{Before=$previousTunnels[$item.Guid];After=$item} 'VPN'
        }
        $previousTunnels[$item.Guid]=$item
    }
    foreach ($id in @($previousTunnels.Keys)) {
        if ($id -notin @($tunnels.Guid) -and $previousTunnels[$id].OperationalStatus -eq 'Up') {
            Set-Incident 'Previously Up VPN tunnel disappeared; possibly a planned node switch' @{Before=$previousTunnels[$id]} 'VPN'
            $previousTunnels.Remove($id)
        }
    }
}
function Sample-Routes {
    $routes=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object DestinationPrefix -in @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1') |
        Select-Object InterfaceIndex,InterfaceAlias,DestinationPrefix,NextHop,RouteMetric,State)
    $tunnels=@(Read-NetworkSnapshot | Where-Object Kind -eq 'Tunnel' | Select-Object Name,Guid,InterfaceIndex,OperationalStatus,IPv4)
    Write-Observation 'routeSummary' @{IPv4Routes=$routes;Tunnels=$tunnels}
}
function Sample-Gateway {
    foreach ($gateway in @($script:lastWifi | Where-Object OperationalStatus -eq 'Up' | ForEach-Object IPv4Gateway | Sort-Object -Unique)) {
        $pinger = [Net.NetworkInformation.Ping]::new()
        try {
            $reply = $pinger.SendPingAsync([Net.IPAddress]::Parse($gateway), 350).GetAwaiter().GetResult()
            Write-Observation 'localGatewayPing' @{Gateway=$gateway;Status=[string]$reply.Status;RoundtripMs=$reply.RoundtripTime}
        } catch { Record-Error 'localGatewayPing' $_ }
        finally { $pinger.Dispose() }
    }
}
function Sample-Processes {
    $processes = @(Read-RelevantProcesses)
    $script:claudePresent = @($processes | Where-Object Name -match '(?i)claude').Count -gt 0
    $tcp = @(Get-NetTCPConnection -ErrorAction Stop)
    $counts = @{}
    foreach ($connection in $tcp) { $key=[int]$connection.OwningProcess; $counts[$key]=1+[int]$counts[$key] }
    Write-Observation 'connectionsSummary' @{
        TcpStates=@($tcp | Group-Object State | ForEach-Object {@{State=$_.Name;Count=$_.Count}})
        RelevantProcesses=@($processes | ForEach-Object {@{Name=$_.Name;Pid=$_.Pid;ParentProcessId=$_.ParentProcessId;StartTime=$_.StartTime;TcpConnections=[int]$counts[[int]$_.Pid]}})
    }
    $vpnIds=@($processes | Where-Object Name -match '(?i)openvpn' | ForEach-Object Pid)
    $udp=if($vpnIds.Count) {@(Get-NetUDPEndpoint -ErrorAction Stop | Where-Object OwningProcess -in $vpnIds | Select-Object OwningProcess,LocalAddress,LocalPort)} else {@()}
    Write-Observation 'openvpnSockets' @{
        Tcp=@($tcp | Where-Object OwningProcess -in $vpnIds | Select-Object OwningProcess,LocalAddress,LocalPort,RemoteAddress,RemotePort,State)
        UdpLocalEndpoints=@($udp)
        Note='Passive socket metadata only. An unconnected UDP socket does not expose its remote peer.'
    }
}
function Read-RelevantProcesses {
    # Request only these CIM properties; never retrieve or record CommandLine.
    $filter="Name LIKE '%claude%' OR Name LIKE '%openvpn%' OR Name LIKE '%FastOrange%' OR Name LIKE '%OrangeDesk%' OR Name LIKE '%aTrust%'"
    foreach($process in @(Get-CimInstance Win32_Process -Filter $filter -Property Name,ProcessId,ParentProcessId,CreationDate -ErrorAction Stop)) {
        [pscustomobject]@{Name=$process.Name;Pid=[int]$process.ProcessId;ParentProcessId=[int]$process.ParentProcessId;StartTime=$(if($process.CreationDate){$process.CreationDate.ToString('o')}else{$null})}
    }
}
function Sample-WifiDetails {
    $details=& "$env:SystemRoot\System32\netsh.exe" wlan show interfaces 2>&1
    $line=ConvertTo-Json -InputObject @{Time=[DateTimeOffset]::Now.ToString('o');Text=($details|Out-String)} -Compress
    $detailPath=Join-Path $SessionDir 'wifi-details.jsonl'
    if([IO.File]::Exists($detailPath) -and ([IO.FileInfo]$detailPath).Length -ge 1MB){Move-OwnedLog $detailPath ($detailPath+'.previous')}
    [IO.File]::AppendAllText($detailPath,"$line`n",$utf8)
    $script:totalBytes += $utf8.GetByteCount("$line`n")
}
function Sample-Events {
    foreach ($channel in @('Microsoft-Windows-WLAN-AutoConfig/Operational','System')) {
        try {
            $cursor=[long]$eventCursors[$channel]
            $events=@(Get-WinEvent -LogName $channel -FilterXPath "*[System[EventRecordID > $cursor]]" -MaxEvents 1000 -ErrorAction Stop | Sort-Object RecordId)
            foreach ($event in $events) {
                if ($event.RecordId -gt [long]$eventCursors[$channel]) { $eventCursors[$channel]=$event.RecordId }
                if ($channel -eq 'System' -and $event.ProviderName -notmatch '(?i)WLAN|NDIS|Netwtw|NetwL|rtwl|Realtek|^e1|^e2|Tcpip|Dhcp|DNS.Client|Kernel-PnP|Kernel-Power|Power-Troubleshooter|DriverFrameworks|NetAdapter') { continue }
                Write-Observation 'windowsEvent' @{Channel=$channel;TimeCreated=$event.TimeCreated.ToString('o');Provider=$event.ProviderName;Id=$event.Id;RecordId=$event.RecordId;ProcessId=$event.ProcessId;Xml=$event.ToXml()}
                if ($channel -eq 'Microsoft-Windows-WLAN-AutoConfig/Operational' -and $event.Id -eq 8003) {
                    Set-Incident 'New WLAN-AutoConfig 8003 disconnect event' @{RecordId=$event.RecordId;TimeCreated=$event.TimeCreated.ToString('o');Xml=$event.ToXml()}
                }
            }
        } catch {
            if ($_.FullyQualifiedErrorId -notmatch '^NoMatchingEventsFound') { Record-Error "events:$channel" $_ }
        }
    }
}
function Sample-LocalState {
    if ($CompanionSessionPath -or (Test-Path -LiteralPath $orangeRoot)) {
        $latest=$null
        if($CompanionSessionPath) {
            $latest=Get-Item -LiteralPath (Join-Path $CompanionSessionPath 'split-status.json') -ErrorAction SilentlyContinue
        } else {
            $latest = Get-ChildItem -LiteralPath $orangeRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object {Get-Item -LiteralPath (Join-Path $_.FullName 'split-status.json') -ErrorAction SilentlyContinue} |
                Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        }
        if ($latest) {
            try {
                $raw = Read-SharedJson $latest.FullName
                $filtered = [ordered]@{}
                foreach ($name in @('VpnReady','Errors','Physical','Vpn','Direct','Active','UpdatedAt','Timestamp','State','Status')) {
                    if ($null -ne $raw.PSObject.Properties[$name]) { $filtered[$name]=$raw.$name }
                }
                if ($latest.FullName -ne $script:lastSplitPath -or $latest.LastWriteTimeUtc -ne $script:lastSplitWrite) {
                    Write-Observation 'orangeDeskStatus' @{Path=$latest.FullName;LastWrite=$latest.LastWriteTimeUtc.ToString('o');Status=$filtered}
                }
                Update-VpnReady $raw.VpnReady $latest.FullName $filtered
                $script:lastSplitPath=$latest.FullName; $script:lastSplitWrite=$latest.LastWriteTimeUtc
                Clear-OperationError 'orangeDeskStatus'
            } catch { Record-Error 'orangeDeskStatus' $_ }
        }
    }
    $vpnPath = Join-Path $vpnLogRoot ('openvpn_'+(Get-Date -Format 'yyyyMMdd')+'.log')
    if (Test-Path -LiteralPath $vpnPath) {
        $stream=$null; $reader=$null
        try {
            $stream=[IO.File]::Open($vpnPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            # Start at EOF: historic STATE entries must not be confused with this session.
            $offset=$stream.Length
            if ($stateOffsets.ContainsKey($vpnPath)) {
                if ($stateOffsets[$vpnPath] -le $stream.Length) {$offset=$stateOffsets[$vpnPath]}
                else {$offset=0L;$statePending[$vpnPath]=''}
            }
            $null=$stream.Seek($offset,[IO.SeekOrigin]::Begin)
            $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true,4096,$true)
            $chunk=[string]$statePending[$vpnPath]+$reader.ReadToEnd()
            $lines=[regex]::Split($chunk,"\r?\n")
            $statePending[$vpnPath]=$lines[-1]
            for($lineIndex=0;$lineIndex -lt ($lines.Count-1);$lineIndex++) {
                $line=$lines[$lineIndex]
                if ($line -match '(?i)(>STATE:|\bSTATE\s*:|\bSTATE\s*=)') { Write-Observation 'openvpnState' @{Path=$vpnPath;Line=$line} }
            }
            $stateOffsets[$vpnPath]=$stream.Position
        } catch { Record-Error 'openvpnState' $_ }
        finally {if($reader){$reader.Dispose()};if($stream){$stream.Dispose()}}
    }
}
function Update-VpnReady($Ready,[string]$Path,$Status) {
    if ($Ready -isnot [bool]) {return}
    if ($Ready) {$script:vpnNotReadySince=$null;$script:vpnNotReadyTriggered=$false}
    elseif ($script:lastVpnReady -eq $true -and $null -eq $script:vpnNotReadySince) {$script:vpnNotReadySince=[DateTimeOffset]::Now}
    if (-not $script:vpnNotReadyTriggered -and $script:vpnNotReadySince -and ([DateTimeOffset]::Now-$script:vpnNotReadySince).TotalSeconds -ge 5) {
        Set-Incident 'OrangeDesk VpnReady changed true to false for at least five seconds; possibly a planned node switch' @{Since=$script:vpnNotReadySince.ToString('o');Path=$Path;Status=$Status} 'VPN'
        $script:vpnNotReadyTriggered=$true
    }
    $script:lastVpnReady=$Ready
}
if ($SelfTest) {
    # Explicit bounded validation: no gateway ping, event loop, VPN log reads or ETW.
    Sample-Network
    Sample-Processes
    Sample-Routes
    $probePath=Join-Path $SessionDir 'selftest-shared.json'
    [IO.File]::WriteAllText($probePath,'{"VpnReady":true}',$utf8)
    $writer=[IO.File]::Open($probePath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $lockObserved=$false
    try {
        try {$null=Read-SharedJson $probePath} catch {$lockObserved=$true;Record-Error 'orangeDeskStatus' $_}
    } finally {$writer.Dispose()}
    if (-not $lockObserved) {throw 'Self-test expected exclusive writer to block reads.'}
    if ((Read-SharedJson $probePath).VpnReady -ne $true) {throw 'Shared JSON retry recovery failed.'}
    Clear-OperationError 'orangeDeskStatus'
    if ($script:lastError -or $script:operationErrors.Count) {throw 'Recovered status error was not cleared.'}
    $writer=[IO.File]::Open($probePath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {if((Read-SharedJson $probePath).VpnReady -ne $true){throw 'Concurrent shared writer read failed.'}} finally {$writer.Dispose()}
    Update-VpnReady $false 'selftest' @{}
    if($script:incidentAt){throw 'An initial false VpnReady must not trigger.'}
    Update-VpnReady $true 'selftest' @{}
    Update-VpnReady $false 'selftest' @{}
    if($script:incidentAt){throw 'VpnReady debounce triggered too soon.'}
    $script:vpnNotReadySince=[DateTimeOffset]::Now.AddSeconds(-6)
    Update-VpnReady $false 'selftest' @{SelfTest=$true}
    if($script:incidentLayer -ne 'VPN'){throw 'Expected VPN-layer trigger.'}
    $script:incidentAt=$null;$script:incidentLayer=$null;$previousTunnels.Clear()
    Update-TunnelState @([pscustomobject]@{Guid='selftest-tap';OperationalStatus='Up'})
    Update-TunnelState @([pscustomobject]@{Guid='selftest-tap';OperationalStatus='Down'})
    if($script:incidentLayer -ne 'VPN'){throw 'Expected TAP Down VPN-layer trigger.'}
    $script:incidentAt=$null;$script:incidentLayer=$null;$previousTunnels.Clear()
    Update-TunnelState @([pscustomobject]@{Guid='selftest-tap';OperationalStatus='Up'})
    Update-TunnelState @()
    if($script:incidentLayer -ne 'VPN'){throw 'Expected missing TAP VPN-layer trigger.'}
    $script:state='SelfTestComplete'
    Save-Status
    Write-Output 'Self-test passed: passive interfaces/process/socket/routes, bounded JSON lock recovery, shared reads, LastError clearing, VPN debounce and TAP Down/missing triggers. No collector loop, probes or ETW launched.'
    return
}
try {
    if (Test-Path -LiteralPath $stopPath) { throw 'stop.request already exists; choose a fresh session directory.' }
    Save-Status
    foreach ($channel in @('Microsoft-Windows-WLAN-AutoConfig/Operational','System')) {
        try {$eventCursors[$channel]=(Get-WinEvent -LogName $channel -MaxEvents 1 -ErrorAction Stop).RecordId}
        catch {$eventCursors[$channel]=0;Record-Error "initialEventCursor:$channel" $_}
    }
    $baseline=[ordered]@{At=$started.ToString('o');CollectorPid=$PID;PowerShell=$PSVersionTable.PSVersion.ToString();Policy='Local passive observations plus ICMP to physical IPv4 gateway only. No public probes. No application payload or command lines.'}
    foreach ($job in @(
        @{Name='Adapters';Run={@(Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceDescription,InterfaceGuid,ifIndex,Status,LinkSpeed,HardwareInterface,DriverInformation,DriverFileName,DriverVersion,DriverDate)}},
        @{Name='AdvancedProperties';Run={@(Get-NetAdapterAdvancedProperty -AllProperties -ErrorAction Stop | Select-Object Name,InterfaceDescription,DisplayName,DisplayValue,RegistryKeyword,RegistryValue)}},
        @{Name='NetworkDrivers';Run={@(Get-CimInstance Win32_PnPSignedDriver -Filter "DeviceClass='NET'" | Select-Object DeviceName,DeviceID,DriverVersion,DriverDate,DriverProviderName,InfName,IsSigned)}},
        @{Name='DefaultRoutes';Run={@(Get-NetRoute | Where-Object {$_.DestinationPrefix -in @('0.0.0.0/0','::/0')} | Select-Object InterfaceIndex,InterfaceAlias,DestinationPrefix,NextHop,RouteMetric,State,PolicyStore)}},
        @{Name='IPInterfaces';Run={@(Get-NetIPInterface | Select-Object InterfaceIndex,InterfaceAlias,AddressFamily,ConnectionState,InterfaceMetric,NlMtu,Dhcp)}},
        @{Name='Processes';Run={@(Read-RelevantProcesses)}}
    )) {
        try {$baseline[$job.Name]=& $job.Run} catch {$baseline[$job.Name]=@{Error=$_.Exception.Message}}
    }
    foreach ($adapter in @($baseline.Adapters | Where-Object HardwareInterface)) {
        if ($adapter.InterfaceGuid) {$physicalIds[([string]$adapter.InterfaceGuid).Trim('{}').ToLowerInvariant()]=$true}
    }
    Save-JsonAtomic (Join-Path $SessionDir 'baseline.json') $baseline
    try {Sample-WifiDetails} catch {Record-Error 'wifiDetailsBaseline' $_}
    $script:state='Running'
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $nextInterface=0.0;$nextGateway=0.0;$nextEvents=0.0;$nextProcesses=0.0;$nextRoutes=0.0;$nextLocal=0.0;$nextStatus=0.0;$nextWifiDetails=15.0
    while ($true) {
        $now=[DateTimeOffset]::Now
        if (Test-Path -LiteralPath $stopPath) {$script:state='StoppedByRequest';break}
        if ($now -ge $deadline) {$script:state='DurationComplete';break}
        if (-not $Continuous -and $script:incidentAt -and $now -ge $script:incidentAt.AddSeconds($PostIncidentSeconds)) {$script:state='IncidentCaptureComplete';break}
        if (-not $Continuous -and $script:totalBytes -ge 128MB) {$script:state='SizeLimitReached';break}
        $elapsed=$timer.Elapsed.TotalSeconds
        foreach ($task in @(
            @{Name='interfaces';Due=$nextInterface;Every=1;Run={Sample-Network}},
            @{Name='gateway';Due=$nextGateway;Every=3;Run={Sample-Gateway}},
            @{Name='events';Due=$nextEvents;Every=3;Run={Sample-Events}},
            @{Name='processes';Due=$nextProcesses;Every=3;Run={Sample-Processes}},
            @{Name='routes';Due=$nextRoutes;Every=3;Run={Sample-Routes}},
            @{Name='local';Due=$nextLocal;Every=1;Run={Sample-LocalState}},
            @{Name='wifiDetails';Due=$nextWifiDetails;Every=15;Run={Sample-WifiDetails}},
            @{Name='status';Due=$nextStatus;Every=5;Run={Save-Status}}
        )) {
            if ($elapsed -lt $task.Due) {continue}
            try {& $task.Run} catch {Record-Error $task.Name $_}
            $next=$timer.Elapsed.TotalSeconds+$task.Every
            switch ($task.Name) {'interfaces'{$nextInterface=$next};'gateway'{$nextGateway=$next};'events'{$nextEvents=$next};'processes'{$nextProcesses=$next};'routes'{$nextRoutes=$next};'local'{$nextLocal=$next};'wifiDetails'{$nextWifiDetails=$next};'status'{$nextStatus=$next}}
        }
        Start-Sleep -Milliseconds 100
    }
} catch {
    $script:state='Failed'
    try {Record-Error 'fatal' $_} catch {}
} finally {
    try {Write-Observation 'collectorStopped' @{State=$script:state};Save-Status} catch {}
    try {[IO.File]::WriteAllText($stopPath,([DateTimeOffset]::Now.ToString('o')+' '+$script:state),$utf8)} catch {}
}
