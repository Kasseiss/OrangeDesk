#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$SessionDir,[ValidateRange(1,1440)][int]$DurationMinutes=120)
$ErrorActionPreference='Stop'
# Keep native netsh output consistent when the elevated process uses a Chinese UI locale.
$OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding
$SessionDir=[IO.Path]::GetFullPath($SessionDir)
[IO.Directory]::CreateDirectory($SessionDir)|Out-Null
$traceId=(Get-Date -Format 'yyyyMMdd-HHmmss-fff')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
$tracePath=Join-Path $SessionDir ('wifi-'+$traceId+'.etl')
$traceStopPath=Join-Path $SessionDir 'trace-stop.request'
$traceOwnerLock=$null
$sessionName='CodexWifi-'+[Guid]::NewGuid().ToString('N').Substring(0,16)
$statusPath=Join-Path $SessionDir 'trace-status.json'
$utf8=[Text.UTF8Encoding]::new($false)
$started=[DateTimeOffset]::Now
$deadline=$started.AddMinutes($DurationMinutes)
$owned=$false
$traceState='Starting'
$traceError=$null
function Save-TraceStatus {
    if($null -eq $traceOwnerLock){return}
    $temp="$statusPath.$PID.tmp"
    [IO.File]::WriteAllText($temp,(ConvertTo-Json -InputObject @{State=$traceState;StartedAt=$started.ToString('o');UpdatedAt=[DateTimeOffset]::Now.ToString('o');Deadline=$deadline.ToString('o');Pid=$PID;TraceFile=$tracePath;SessionName=$sessionName;Owned=$owned;Error=$traceError} -Depth 5),$utf8)
    if([IO.File]::Exists($statusPath)){[IO.File]::Replace($temp,$statusPath,[NullString]::Value)}else{[IO.File]::Move($temp,$statusPath)}
}
function Get-TraceStatusText([switch]$Global) {
    if($Global){$output=& "$env:SystemRoot\System32\netsh.exe" trace show status 2>&1}
    else{$output=& "$env:SystemRoot\System32\netsh.exe" trace show status "sessionname=$sessionName" 2>&1}
    return ($output | Out-String)
}
function Read-SharedCollectorStatus([string]$Path) {
    for($attempt=0;$attempt -lt 3;$attempt++) {
        $stream=$null;$reader=$null
        try {
            $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
            return ($reader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop)
        } catch {if($attempt -eq 2){throw};Start-Sleep -Milliseconds (50*($attempt+1))}
        finally {if($reader){$reader.Dispose()}elseif($stream){$stream.Dispose()}}
    }
}
try {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=[Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Administrator privileges required. This script will not request UAC itself.'}
    if (Test-Path -LiteralPath (Join-Path $SessionDir 'stop.request')) {throw 'stop.request already exists; use a fresh session.'}
    $traceOwnerLock=[IO.File]::Open((Join-Path $SessionDir 'trace-owner.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    if([IO.File]::Exists($traceStopPath)){[IO.File]::Delete($traceStopPath)}
    if (Test-Path -LiteralPath $tracePath) {throw 'Trace file already exists; refusing overwrite.'}
    $before=Get-TraceStatusText -Global
    [IO.File]::WriteAllText((Join-Path $SessionDir ('trace-'+$traceId+'-before.txt')),$before,$utf8)
    # An active session displays a trace filename. Only an explicit not-running result is accepted.
    if ($before -notmatch '(?i)no trace session|not running|未运行|没有正在运行|当前没有|没有跟踪会话|未在运行|没有运行') {throw 'Could not establish that no netsh trace session is running. Refusing to start or stop another session.'}
    $argsList=@('trace','start',"sessionname=$sessionName",'capture=no','report=disabled','persistent=no','maxsize=128','filemode=circular','overwrite=no',"tracefile=$tracePath",'provider=Microsoft-Windows-WLAN-AutoConfig','keywords=0x7FF','level=4','provider=Microsoft-Windows-NDIS','keywords=0x125F','level=4','provider=Microsoft-Windows-Kernel-PnP','keywords=0xFF000','level=4')
    # Inspected locally: AFD exposes coarse STREAM/DATAGRAM/source categories rather
    # than connection-only keywords. Omit it to avoid broad I/O records. TCPIP's
    # TCB/ConnectPath/ClosePath covers state, abort and retransmission timeouts;
    # never enable Transfer, Packet, SendPath, ReceivePath or per-packet levels.
    $providerNotes=[Collections.Generic.List[object]]::new()
    foreach($providerName in @('Microsoft-Windows-TCPIP','Microsoft-Windows-Winsock-AFD')) {
        $providerOutput=& "$env:SystemRoot\System32\netsh.exe" trace show provider $providerName 2>&1
        $providerCode=$LASTEXITCODE
        $providerText=$providerOutput|Out-String
        [IO.File]::WriteAllText((Join-Path $SessionDir ($providerName+'-'+$traceId+'-metadata.txt')),$providerText,$utf8)
        if($providerName -eq 'Microsoft-Windows-TCPIP' -and $providerCode -eq 0 -and
            $providerText -match 'ut:TcpipTcb' -and $providerText -match 'ut:ConnectPath' -and $providerText -match 'ut:ClosePath') {
            $argsList+=@('provider=Microsoft-Windows-TCPIP','keywords=0x1400000004','level=4')
            $providerNotes.Add(@{Provider=$providerName;Enabled=$true;Keywords='0x1400000004';Level=4;Scope='TCB + ConnectPath + ClosePath; connection metadata and retransmission/error state only'})
        } else {
            $providerNotes.Add(@{Provider=$providerName;Enabled=$false;Reason=$(if($providerName -eq 'Microsoft-Windows-Winsock-AFD'){'Only coarse STREAM/DATAGRAM/I/O categories available; omitted to keep tracing bounded and payload-free.'}else{'Required local keyword metadata unavailable; WLAN/NDIS/PnP tracing remains enabled.'})})
        }
    }
    [IO.File]::WriteAllText((Join-Path $SessionDir ('transport-provider-selection-'+$traceId+'.json')),(ConvertTo-Json -InputObject @($providerNotes) -Depth 6),$utf8)
    $startOutput=& "$env:SystemRoot\System32\netsh.exe" @argsList 2>&1
    $startCode=$LASTEXITCODE
    [IO.File]::WriteAllText((Join-Path $SessionDir ('trace-'+$traceId+'-start.txt')),($startOutput|Out-String),$utf8)
    $after=Get-TraceStatusText
    [IO.File]::WriteAllText((Join-Path $SessionDir ('trace-'+$traceId+'-after.txt')),$after,$utf8)
    if ($startCode -ne 0 -or $after.IndexOf($tracePath,[StringComparison]::OrdinalIgnoreCase) -lt 0) {throw 'Trace start was not confirmed as this session. No stop command will be issued.'}
    $owned=$true;$traceState='Running';Save-TraceStatus
    $collectorSeen=$false
    while ([DateTimeOffset]::Now -lt $deadline) {
        if((Test-Path -LiteralPath (Join-Path $SessionDir 'stop.request')) -or (Test-Path -LiteralPath $traceStopPath)) {break}
        $collectorStatusPath=Join-Path $SessionDir 'status.json'
        if(Test-Path -LiteralPath $collectorStatusPath) {
            try {
                $collector=Read-SharedCollectorStatus $collectorStatusPath
                $collectorSeen=$true
                if($traceError -like 'Could not read collector status:*'){$traceError=$null}
                if($collector.State -notin @('Starting','Running','PostIncident')) {break}
                if(-not (Get-Process -Id ([int]$collector.Pid) -ErrorAction SilentlyContinue)) {break}
            } catch {if($collectorSeen){$traceError='Could not read collector status: '+$_.Exception.Message}}
        }
        Save-TraceStatus
        Start-Sleep -Seconds 2
    }
    $traceState='Stopping'
} catch {$traceState='Failed';$traceError=$_.Exception.Message}
finally {
    if($owned) {
        try {
            $current=Get-TraceStatusText
            if($current.IndexOf($tracePath,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $stopOutput=& "$env:SystemRoot\System32\netsh.exe" trace stop "sessionname=$sessionName" 2>&1
                $stopCode=$LASTEXITCODE
                [IO.File]::WriteAllText((Join-Path $SessionDir ('trace-'+$traceId+'-stop.txt')),($stopOutput|Out-String),$utf8)
                if($stopCode -eq 0){$traceState='Stopped'}else{$traceState='StopFailed';$traceError='netsh trace stop returned '+$stopCode}
            } else {$traceState='OwnershipLost';$traceError='Current trace no longer names this session; no stop command issued.'}
        } catch {$traceState='StopFailed';$traceError=$_.Exception.Message}
    }
    Save-TraceStatus
    if($null -ne $traceOwnerLock){$traceOwnerLock.Dispose()}
}
