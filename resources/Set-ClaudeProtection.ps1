#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Enable','Disable','Status')][string]$Action='Status',
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$OwnerSid,
    [string]$VpnGuid,
    [string]$AppPathsJsonPath,
    [string]$RequestId,
    [switch]$SelfTest
)
$ErrorActionPreference='Stop'
$OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding
$utf8=[Text.UTF8Encoding]::new($false)
$policyLoaded=$false
$failure=$null
$exitCode=0
$statePath=$null
$resultPath=$null
$selectedInterface=$null
$snapshot=$null
$selfTestCompleted=$false
$script:requestMutex=$null
$script:requestLockHeld=$false
$script:requestOrderVerified=$false
function Get-LocalAbsolutePath([string]$Value) {
    if([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.IndexOf([char]0) -ge 0) {
        throw 'Only absolute local drive paths are accepted.'
    }
    return [IO.Path]::GetFullPath($Value)
}
function Write-AtomicJson([string]$Path,$Value) {
    $temp=$Path+'.'+$PID+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    try {
        [IO.File]::WriteAllText($temp,(ConvertTo-Json -InputObject $Value -Depth 12),$utf8)
        for($attempt=0;$attempt -lt 3;$attempt++) {
            try {
                if([IO.File]::Exists($Path)){[IO.File]::Replace($temp,$Path,[NullString]::Value)}
                else{[IO.File]::Move($temp,$Path)}
                return
            } catch {if($attempt -eq 2){throw};Start-Sleep -Milliseconds (50*($attempt+1))}
        }
    } finally {if([IO.File]::Exists($temp)){[IO.File]::Delete($temp)}}
}
function Read-AppPaths {
    $path=Get-LocalAbsolutePath $AppPathsJsonPath
    if(-not $path.StartsWith($StateDir.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) {
        throw 'The application-path request must be inside the protection state directory.'
    }
    $info=Get-Item -LiteralPath $path -ErrorAction Stop
    if($info.PSIsContainer -or $info.Length -gt 65536){throw 'Invalid application-path request size.'}
    $stream=$null;$reader=$null
    try {
        $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
        $text=$reader.ReadToEnd()
    } finally {if($reader){$reader.Dispose()}elseif($stream){$stream.Dispose()}}
    if($text.TrimStart() -notmatch '^\['){throw 'Application paths must be a JSON array of strings.'}
    # PS5.1 pipeline enumeration can wrap a one-element JSON array as one
    # nested array. A property preserves the original JSON array dimensions.
    $wrapper=('{"apps":'+$text+'}') | ConvertFrom-Json -ErrorAction Stop
    if(@($wrapper.PSObject.Properties).Count -ne 1 -or $wrapper.apps -isnot [System.Array]) {
        throw 'Application paths must be a JSON array of strings.'
    }
    $items=$wrapper.apps
    if($items.Count -lt 1 -or $items.Count -gt 32){throw 'Expected between one and 32 existing Claude executables.'}
    $unique=@{}
    foreach($item in $items) {
        if($item -isnot [string]){throw 'Application paths must contain strings only.'}
        $app=Get-LocalAbsolutePath $item
        if([IO.Path]::GetFileName($app) -ine 'claude.exe'){throw 'Only the installed native claude.exe can be protected.'}
        $file=Get-Item -LiteralPath $app -ErrorAction Stop
        if($file.PSIsContainer -or $file.Length -lt 68){throw 'The Claude executable is not a valid PE file.'}
        $binary=$null;$binaryReader=$null
        try {
            $binary=[IO.File]::Open($app,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $binaryReader=[IO.BinaryReader]::new($binary)
            if($binaryReader.ReadUInt16() -ne 0x5A4D){throw 'The Claude executable has no MZ header.'}
            $null=$binary.Seek(0x3C,[IO.SeekOrigin]::Begin)
            $offset=$binaryReader.ReadInt32()
            if($offset -lt 64 -or $offset -gt ($binary.Length-4)){throw 'The Claude executable has an invalid PE header offset.'}
            $null=$binary.Seek($offset,[IO.SeekOrigin]::Begin)
            if($binaryReader.ReadUInt32() -ne 0x4550){throw 'The Claude executable has no PE signature.'}
        } finally {if($binaryReader){$binaryReader.Dispose()}elseif($binary){$binary.Dispose()}}
        $unique[$app]=$true
    }
    return @($unique.Keys | Sort-Object)
}
function Initialize-InterfaceIdentity {
    if(-not ('OrangeDeskClaudeGuard.InterfaceIdentity' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace OrangeDeskClaudeGuard {
    public static class InterfaceIdentity {
        [DllImport("iphlpapi.dll", ExactSpelling=true)]
        public static extern uint ConvertInterfaceGuidToLuid(ref Guid guid, out ulong luid);
        [DllImport("iphlpapi.dll", ExactSpelling=true)]
        public static extern uint ConvertInterfaceLuidToGuid(ref ulong luid, out Guid guid);
    }
}
'@
    }
}
function Get-ValidatedTunnel {
    $parsed=[guid]::Empty
    if(-not [guid]::TryParse($VpnGuid,[ref]$parsed) -or $parsed -eq [guid]::Empty){throw 'A valid TAP interface GUID is required.'}
    $taps=@(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {$_.Status -eq 'Up' -and $_.InterfaceDescription -like '*TAP-Windows*'})
    if($taps.Count -ne 1){throw 'Expected exactly one Up TAP-Windows adapter. Existing protection was not changed.'}
    $tap=$taps[0]
    if([guid]$tap.InterfaceGuid -ne $parsed){throw 'The selected TAP identity changed. Existing protection was not changed.'}
    $addresses=@(Get-NetIPAddress -InterfaceIndex $tap.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -notmatch '^(0\.|127\.|169\.254\.)'})
    if($addresses.Count -lt 1){throw 'The selected TAP has no preferred VPN IPv4 address. Existing protection was not changed.'}
    Initialize-InterfaceIdentity
    [uint64]$luid=0
    $code=[OrangeDeskClaudeGuard.InterfaceIdentity]::ConvertInterfaceGuidToLuid([ref]$parsed,[ref]$luid)
    if($code -ne 0 -or $luid -eq 0){throw ('Cannot resolve the selected TAP LUID, Windows error '+$code+'.')}
    return [pscustomobject]@{Guid=$parsed.ToString('D');Name=[string]$tap.Name;Luid=$luid}
}
function Get-ActualInterface([uint64]$Luid) {
    if($Luid -eq 0){return $null}
    Initialize-InterfaceIdentity
    $guid=[guid]::Empty
    if([OrangeDeskClaudeGuard.InterfaceIdentity]::ConvertInterfaceLuidToGuid([ref]$Luid,[ref]$guid) -ne 0){return $null}
    $adapter=Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {[guid]$_.InterfaceGuid -eq $guid} | Select-Object -First 1
    return [pscustomobject]@{Guid=$guid.ToString('D');Name=$(if($adapter){[string]$adapter.Name}else{$null});Luid=$Luid}
}
function Read-KnownFilterKeys {
    if(-not [IO.File]::Exists($statePath)){return @()}
    $stream=$null;$reader=$null
    try {
        $stream=[IO.File]::Open($statePath,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        if($stream.Length -gt 65536){throw 'Protection status exceeds the 64KB limit; administrator repair is required.'}
        $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
        $buffer=New-Object char[] 65537
        $count=$reader.ReadBlock($buffer,0,$buffer.Length)
        if($count -gt 65536){throw 'Protection status exceeds the 64KB limit; administrator repair is required.'}
        if($count -eq 0){throw 'Protection status is empty; administrator repair is required.'}
        $record=(-join $buffer[0..($count-1)]) | ConvertFrom-Json -ErrorAction Stop
    } finally {if($reader){$reader.Dispose()}elseif($stream){$stream.Dispose()}}
    # The file is a lookup hint only. Never trust its enabled/apps/interface data.
    if($null -eq $record.PSObject.Properties['filter_keys']){return @()}
    if($record.filter_keys -isnot [System.Array]){throw 'Invalid saved filter-key list; administrator repair is required.'}
    $items=@($record.filter_keys)
    if($items.Count -eq 0){return @()}
    if($items.Count -gt 128){throw 'Too many saved filter keys; administrator repair is required.'}
    $keys=@{}
    foreach($item in $items) {
        $key=[guid]::Empty
        if($item -isnot [string] -or -not [guid]::TryParseExact($item,'D',[ref]$key) -or $key -eq [guid]::Empty) {
            throw 'Invalid saved filter key; administrator repair is required.'
        }
        $keys[$key.ToString('D')]=$true
    }
    return @($keys.Keys | Sort-Object)
}
function Read-PolicySnapshot {
    if($Action -eq 'Status') {
        [string[]]$keys=@(Read-KnownFilterKeys)
        if($keys.Count -gt 0){return [OrangeDeskClaudeGuard.Policy]::ReadKnown($OwnerSid,$keys)}
    }
    # With no known keys, Read can establish "not installed" when no owned
    # sublayer exists. An existing sublayer plus denied enumeration must fail
    # explicitly instead of reporting disabled or trusting saved enabled=true.
    return [OrangeDeskClaudeGuard.Policy]::Read($OwnerSid)
}
function Confirm-LatestMutationRequest {
    $path=Join-Path $StateDir 'last-request.json'
    $stream=$null;$reader=$null
    try {
        $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        if($stream.Length -gt 65536){throw 'The pending request marker exceeds the 64KB limit.'}
        $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
        $buffer=New-Object char[] 65537
        $count=$reader.ReadBlock($buffer,0,$buffer.Length)
        if($count -eq 0 -or $count -gt 65536){throw 'The pending request marker is invalid.'}
        $record=(-join $buffer[0..($count-1)]) | ConvertFrom-Json -ErrorAction Stop
        if($record.request_id -isnot [string] -or $record.state -isnot [string] -or
            $record.request_id -cne $RequestId -or $record.state -cne 'pending') {
            throw 'This request is no longer the current pending request. No protection rules were changed.'
        }
        # All remaining marker fields are ignored; they cannot authorize policy
        # changes, choose an executable/interface, or set the reported result.
    } finally {if($reader){$reader.Dispose()}elseif($stream){$stream.Dispose()}}
}
function Enter-MutationRequestLock {
    $script:requestMutex=[Threading.Mutex]::new($false,('Local\OrangeDeskClaudeGuardMutation-'+$OwnerSid))
    try {$script:requestLockHeld=$script:requestMutex.WaitOne(60000)}
    catch [Threading.AbandonedMutexException] {$script:requestLockHeld=$true}
    if(-not $script:requestLockHeld){throw 'Timed out waiting for the protection request lock. No protection rules were changed.'}
    Confirm-LatestMutationRequest
    $script:requestOrderVerified=$true
}
try {
try {
    $StateDir=Get-LocalAbsolutePath $StateDir
    if([IO.Path]::GetFileName($StateDir.TrimEnd('\')) -ine 'claude-protection'){throw 'StateDir must be the dedicated claude-protection directory.'}
    $sid=[Security.Principal.SecurityIdentifier]::new($OwnerSid)
    if($OwnerSid -cne $sid.Value -or $OwnerSid -notmatch '^S-1-[0-9]+(?:-[0-9]+)+$'){throw 'Invalid owner SID.'}
    [IO.Directory]::CreateDirectory($StateDir)|Out-Null
    $statePath=Join-Path $StateDir 'status.json'
    if($RequestId) {
        $parsedRequest=[guid]::Empty
        if(-not [guid]::TryParseExact($RequestId,'D',[ref]$parsedRequest)){throw 'RequestId must be a UUID in D format.'}
        $resultPath=Join-Path $StateDir ('request-'+$parsedRequest.ToString('D')+'.result.json')
        if([IO.File]::Exists($resultPath)){throw 'A result already exists for this request. Submit a new request UUID.'}
    } elseif($Action -ne 'Status' -and -not $SelfTest){throw 'Enable and Disable require a unique RequestId.'}
    $source=Join-Path $PSScriptRoot 'ClaudeNetworkGuard.cs'
    if(-not [IO.File]::Exists($source)){throw 'The packaged ClaudeNetworkGuard.cs resource is missing.'}
    if(-not ('OrangeDeskClaudeGuard.Policy' -as [type])){Add-Type -Path $source -ErrorAction Stop -WarningAction SilentlyContinue}
    $policyLoaded=$true
    if($SelfTest){
        Initialize-InterfaceIdentity
        if(-not [OrangeDeskClaudeGuard.Policy].GetMethod('ReadKnown',[type[]]@([string],[string[]]))){throw 'The packaged policy backend lacks ReadKnown.'}
        $null=@(Read-KnownFilterKeys)
        # Exercise the PowerShell parameter type and function body as well as
        # compiling C#. Zero is deliberately non-operative and returns no NIC.
        $null=Get-ActualInterface ([uint64]0)
        if($Action -eq 'Status' -and $VpnGuid){$null=Get-ValidatedTunnel}
        if($RequestId){Confirm-LatestMutationRequest}
    }
    if($Action -ne 'Status') {
        if(-not $SelfTest) {
            $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
            $principal=[Security.Principal.WindowsPrincipal]::new($identity)
            if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator approval is required to change Claude protection.'}
        }
        if($Action -eq 'Enable') {
            [string[]]$apps=@(Read-AppPaths)
            $selectedInterface=Get-ValidatedTunnel
        }
    }
    if($SelfTest) {
        # Compilation and argument/native-file validation only. No WFP Read,
        # Install, Remove, process launch, network requests or UAC are performed.
        $selfTestCompleted=$true
    } else {
        if($Action -ne 'Status'){Enter-MutationRequestLock}
        if($Action -eq 'Enable'){[void][OrangeDeskClaudeGuard.Policy]::Install($OwnerSid,$apps,[uint64]$selectedInterface.Luid)}
        elseif($Action -eq 'Disable'){[void][OrangeDeskClaudeGuard.Policy]::Remove($OwnerSid)}
        $snapshot=Read-PolicySnapshot
        if($Action -eq 'Status' -and @($snapshot.FilterKeys).Count -gt 0 -and
            (-not $snapshot.Enabled -or $snapshot.FilterCount -ne @($snapshot.FilterKeys).Count)) {
            throw 'Known protection rules are missing or incomplete; administrator recovery is required.'
        }
        if($Action -eq 'Enable' -and (-not $snapshot.Enabled -or $snapshot.FilterCount -le 0)){throw 'The protection install could not be confirmed.'}
        if($Action -eq 'Disable' -and $snapshot.FilterCount -ne 0){throw 'Protection filters remain; disable was not confirmed.'}
    }
} catch {
    $failure=$_.Exception.Message
    if($Action -eq 'Status' -and -not $SelfTest){$failure+=' Rule state is unknown; authorize administrator recovery to restore protection verification.'}
    $exitCode=1
    if($policyLoaded -and -not $SelfTest -and $Action -ne 'Status') {
        try {$snapshot=[OrangeDeskClaudeGuard.Policy]::Read($OwnerSid)} catch {$failure+=' Actual filter state could not be read: '+$_.Exception.Message;$snapshot=$null}
    }
}
$enabled=$null;$filterCount=$null;$actualApps=@();$filterKeys=@();$luid=$null;$message=$failure
$actualInterface=$null
if($null -ne $snapshot) {
    $enabled=[bool]$snapshot.Enabled;$filterCount=[int]$snapshot.FilterCount;$actualApps=@($snapshot.Apps);$luid=[string]$snapshot.InterfaceLuid
    if($null -ne $snapshot.PSObject.Properties['FilterKeys']){$filterKeys=@($snapshot.FilterKeys)}
    try {$actualInterface=Get-ActualInterface ([uint64]$snapshot.InterfaceLuid)} catch {}
    if(-not $message){
        $message=if($snapshot.Enabled){'已核验所列原生 Claude 程序的 IPv4/IPv6 出口限制。本机回环通信除外；节点国家需自行确认。'}
            elseif($snapshot.FilterCount -eq 0){'Claude 出口保护未开启。'}
            else{'已有规则不完整，尚未确认保护有效。请导出诊断记录。'}
    }
}
if($selfTestCompleted){$message='Compile and validation self-test passed; no policy mutation or network request was performed.'}
$state=if($failure){'failed'}elseif($selfTestCompleted){'self-test'}elseif($enabled){'enabled'}else{'disabled'}
$result=[ordered]@{
    state=$state;enabled=$enabled;message=$message;apps=$actualApps;filter_keys=$filterKeys
    interface_name=$(if($actualInterface){$actualInterface.Name}else{$null})
    interface_guid=$(if($actualInterface){$actualInterface.Guid}else{$null})
    interface_luid=$luid;filter_count=$filterCount;updated_at=[DateTimeOffset]::Now.ToString('o')
    owner_sid=$OwnerSid;error=$failure;action=$Action;request_id=$RequestId;self_test=[bool]$SelfTest
    region_verified=$false;loopback_excluded=$true;scope='identified-native-executable-paths-all-users'
}
if($statePath -and -not $SelfTest -and $Action -ne 'Status' -and $script:requestOrderVerified) {
    try {Write-AtomicJson $statePath $result} catch {$result.error='Could not save protection status: '+$_.Exception.Message;$result.state='failed';$exitCode=1}
}
if($resultPath -and -not $SelfTest) {
    $requestTemp=$resultPath+'.'+$PID+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    try {
        if([IO.File]::Exists($resultPath)){throw 'Refusing to replace a completed request result.'}
        [IO.File]::WriteAllText($requestTemp,(ConvertTo-Json -InputObject $result -Depth 12),$utf8)
        # The two-argument Move is atomic and refuses an existing destination.
        [IO.File]::Move($requestTemp,$resultPath)
    } catch {$result.error='Could not save this request result: '+$_.Exception.Message;$result.state='failed';$exitCode=1}
    finally {if([IO.File]::Exists($requestTemp)){[IO.File]::Delete($requestTemp)}}
}
ConvertTo-Json -InputObject $result -Depth 12 -Compress
} finally {
    if($null -ne $script:requestMutex) {
        try {if($script:requestLockHeld){$script:requestMutex.ReleaseMutex()}}
        finally {$script:requestMutex.Dispose();$script:requestMutex=$null;$script:requestLockHeld=$false}
    }
}
exit $exitCode
