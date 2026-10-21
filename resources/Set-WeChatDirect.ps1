#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Status','Enable','Disable')][string]$Action='Status',
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][string]$OwnerSid,
    [string]$RequestId,
    [string]$ComponentCache,
    [switch]$SelfTest
)
$ErrorActionPreference='Stop'
$utf8=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$utf8
$OutputEncoding=$utf8
# This dedicated management process needs only native Windows system modules.
# Python may inherit PS7 module paths; contain that inheritance here, before
# any cmdlet autoload. No registry, parent-process or machine setting changes.
$env:PSModulePath=[IO.Path]::Combine($PSHOME,'Modules')
$script:Data=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'OrangeDeskWeChatDirect'
$script:Bin=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'OrangeDeskWeChatDirect'
$script:TaskName='OrangeDesk-WeChatDirect'
$script:ServiceName='ProxiFyreService'
$script:RebootRequired=$false
$script:VerifiedRequest=$false
$script:ParametersValidated=$false
$script:Mutex=$null
$script:Held=$false
$exitCode=0

function Local-Path([string]$Value) {
    if([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.Contains([string][char]0)){throw 'An absolute local drive path is required.'}
    $p=[IO.Path]::GetFullPath($Value)
    $part=$p
    while($part){
        if(Test-Path -LiteralPath $part){if((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Reparse-point paths are not supported.'}}
        $parent=[IO.Path]::GetDirectoryName($part.TrimEnd('\'))
        if($parent -eq $part){break};$part=$parent
    }
    return $p
}
function Read-Json([string]$Path) {
    $null=Local-Path $Path
    $file=Get-Item -LiteralPath $Path -Force
    if($file.PSIsContainer -or $file.Length -gt 65536){throw 'JSON metadata exceeds the 64KB limit.'}
    $stream=$null;$reader=$null
    try{$stream=[IO.File]::Open($Path,'Open','Read',([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete));$reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true);return ($reader.ReadToEnd() | ConvertFrom-Json)}
    finally{if($reader){$reader.Dispose()}elseif($stream){$stream.Dispose()}}
}
function Write-Json([string]$Path,$Value) {
    $null=Local-Path $Path
    $tmp=$Path+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    try{
        [IO.File]::WriteAllText($tmp,(ConvertTo-Json -InputObject $Value -Depth 10),$utf8)
        if([IO.File]::Exists($Path)){[IO.File]::Replace($tmp,$Path,[NullString]::Value)}else{[IO.File]::Move($tmp,$Path)}
        if($Path.StartsWith($script:Data.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){Protect-File $Path}
    }finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
}
function Get-Hash([string]$Path){$null=Local-Path $Path;return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function Assert-Request {
    $r=Read-Json (Join-Path $StateDir 'last-request.json')
    if($r.state -cne 'pending' -or $r.request_id -cne $RequestId -or $r.owner_sid -cne $OwnerSid -or
        $r.desired_enabled -isnot [bool] -or $r.desired_enabled -ne ($Action -eq 'Enable')){throw 'This request is no longer the current pending operation.'}
}
function Set-Progress([string]$Message){
    Assert-Request
    Write-Json (Join-Path $StateDir 'progress.json') ([ordered]@{state='pending';request_id=$RequestId;owner_sid=$OwnerSid;desired_enabled=($Action -eq 'Enable');message=$Message;updated_at=[DateTime]::UtcNow.ToString('o')})
}
function Protect-Directory([string]$Path){
    $null=Local-Path $Path
    $null=New-Item -ItemType Directory -Path $Path -Force
    $security=[Security.AccessControl.DirectorySecurity]::new()
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544',$OwnerSid)){
        $rights=if($sid -eq $OwnerSid){[Security.AccessControl.FileSystemRights]::ReadAndExecute}else{[Security.AccessControl.FileSystemRights]::FullControl}
        $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),$rights,([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
        $null=$security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}
function Protect-File([string]$Path){
    $null=Local-Path $Path
    $security=[Security.AccessControl.FileSecurity]::new()
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544',$OwnerSid)){
        $rights=if($sid -eq $OwnerSid){[Security.AccessControl.FileSystemRights]::ReadAndExecute}else{[Security.AccessControl.FileSystemRights]::FullControl}
        $null=$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),$rights,[Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}
function Assert-Protected([string]$Path){
    $null=Local-Path $Path
    $acl=Get-Acl -LiteralPath $Path
    $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if($owner -notin @('S-1-5-18','S-1-5-32-544')){throw 'Privileged component ownership is invalid.'}
    $write=[Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
        if($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $write) -ne 0 -and $rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')){throw 'Privileged component is writable by an untrusted identity.'}
    }
}
function Get-OwnerMarker {
    $path=Join-Path $script:Data 'install-owner.json'
    if(-not (Test-Path -LiteralPath $path)){return $null}
    Assert-Protected $script:Data;Assert-Protected $path
    $m=Read-Json $path
    if($m.owner_sid -cne $OwnerSid -or $m.component -cne 'OrangeDesk-WeChatDirect'){throw 'This component belongs to a different owner.'}
    return $m
}
function Get-ProxiService {return Get-CimInstance Win32_Service -Filter "Name='ProxiFyreService'"}
function Get-ProxiExe($Service){
    if(-not $Service){throw 'ProxiFyre service is not installed.'}
    $raw=[string]$Service.PathName
    if($raw -match '^"([^"]+)"(?:\s.*)?$'){$p=$matches[1]}elseif($raw -match '^([^"\r\n]+?\.exe)(?:\s.*)?$'){$p=$matches[1]}else{throw 'Invalid ProxiFyre service image path.'}
    $p=Local-Path $p
    $expected=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'ProxiFyre\ProxiFyre.exe'
    if($p -ine $expected -or -not [IO.File]::Exists($p)){throw 'ProxiFyre is not installed at the expected protected location.'}
    Assert-Protected (Split-Path -Parent $p);Assert-Protected $p
    if((Get-Item -LiteralPath $p).VersionInfo.FileVersion -notmatch '^2\.6\.4(?:\.|$)'){throw 'ProxiFyre 2.6.4 is required.'}
    return $p
}
function Get-Physical([string]$Guid){
    $valid=@()
    foreach($a in @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up')){
        if($a.InterfaceDescription -match 'TAP|TUN|VPN|Virtual' -or ($Guid -and ([guid]$a.InterfaceGuid).ToString('D') -ine $Guid)){continue}
        $ip=Get-NetIPConfiguration -InterfaceIndex $a.ifIndex
        if(@($ip.IPv4DefaultGateway).Count -gt 0 -and @($ip.IPv4Address | Where-Object {$_.IPAddress -notmatch '^169\.254\.'}).Count -gt 0){$valid+=,$a}
    }
    if($valid.Count -ne 1){throw 'A unique active physical IPv4 gateway interface is required.'}
    return $valid[0]
}
function Assert-WeChat([string]$Path){
    $p=Local-Path $Path
    if([IO.Path]::GetFileName($p) -notin @('Weixin.exe','WeChat.exe')){throw 'Only Weixin.exe or WeChat.exe is supported.'}
    $s=[IO.File]::OpenRead($p);$b=[IO.BinaryReader]::new($s)
    try{if($s.Length -lt 68 -or $b.ReadUInt16() -ne 0x5A4D){throw 'Invalid WeChat PE image.'};$s.Position=60;$offset=$b.ReadInt32();if($offset -lt 64 -or $offset -gt $s.Length-4){throw 'Invalid WeChat PE image.'};$s.Position=$offset;if($b.ReadUInt32() -ne 0x4550){throw 'Invalid WeChat PE image.'}}finally{$b.Dispose()}
    $sig=Get-AuthenticodeSignature -LiteralPath $p
    if($sig.Status -ne 'Valid' -or $null -eq $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notmatch '(?i)Tencent|腾讯'){throw 'The installed WeChat executable must have a valid Tencent signature.'}
    return $p
}
function Find-WeChat {
    $candidates=@()
    foreach($p in @(Get-Process -Name Weixin,WeChat -ErrorAction SilentlyContinue)){if($p.Path){$candidates+=$p.Path}}
    foreach($base in @([Environment]::GetFolderPath('ProgramFiles'),${env:ProgramFiles(x86)})){
        if(-not $base){continue}
        foreach($name in @('Weixin','WeChat')){
            $folder=Join-Path $base ('Tencent\'+$name)
            foreach($exe in @('Weixin.exe','WeChat.exe')){$candidates+=Join-Path $folder $exe}
            if(Test-Path -LiteralPath $folder){foreach($child in @(Get-ChildItem -LiteralPath $folder -Directory | Select-Object -First 32)){foreach($exe in @('Weixin.exe','WeChat.exe')){$candidates+=Join-Path $child.FullName $exe}}}
        }
    }
    foreach($p in @($candidates | Select-Object -Unique)){if([IO.File]::Exists($p)){return Assert-WeChat $p}}
    throw 'No installed Tencent-signed WeChat executable was found. Open WeChat once and retry.'
}
function Compile-Relay([string]$Source,[string]$Output){
    # Source is always this packaged helper's sibling or our protected copy.
    # An unpackaged developer source can reside in OneDrive (cloud reparse tag).
    $null=Local-Path $Output
    Add-Type -TypeDefinition ([IO.File]::ReadAllText($Source,[Text.Encoding]::UTF8)) -ReferencedAssemblies @('System.dll','System.Core.dll','System.Web.Extensions.dll') -OutputAssembly $Output -OutputType ConsoleApplication | Out-Null
}
function Get-Package([string]$Name,[string]$Url,[string]$Hash,[long]$Size,[bool]$Signed){
    $path=Join-Path $script:Data $Name
    if(-not [IO.File]::Exists($path)){
        Set-Progress ('正在获取已固定版本的 '+$Name)
        $cached=if($ComponentCache){Join-Path $ComponentCache $Name}else{''}
        $tmp=$path+'.download'
        try{
            # The read-only cache can be on OneDrive; pinned bytes, never its ACL,
            # are trusted. All privileged destinations reject reparse points.
            if($cached -and [IO.File]::Exists($cached)){[IO.File]::Copy($cached,$tmp,$true)}
            else{[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $tmp}
            if((Get-Item -LiteralPath $tmp).Length -ne $Size -or (Get-Hash $tmp) -cne $Hash){throw 'The downloaded installer failed its fixed hash or size verification.'}
            [IO.File]::Move($tmp,$path)
        }finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
    }
    if((Get-Item -LiteralPath $path).Length -ne $Size -or (Get-Hash $path) -cne $Hash){throw 'The installer cache failed fixed hash or size verification.'}
    if($Signed -and (Get-AuthenticodeSignature -LiteralPath $path).Status -ne 'Valid'){throw 'The Windows Packet Filter installer signature is not valid.'}
    return $path
}
function Install-Package([string]$Path,[string]$LogName){
    Assert-Request
    $log=Join-Path $script:Data $LogName
    if([IO.Path]::GetExtension($Path) -ieq '.exe'){
        # Official Burn setup owns its prerequisite checks and supported MSI
        # chain properties. Never synthesize those properties in our launcher.
        $p=Start-Process -FilePath $Path -ArgumentList ('/install /quiet /norestart /log "'+$log+'"') -PassThru -WindowStyle Hidden
    }else{
        $p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') -ArgumentList ('/i "'+$Path+'" /qn /norestart /L*v "'+$log+'"') -PassThru -WindowStyle Hidden
    }
    try{
        # Start-Process -Wait waits for the whole descendant tree on Windows,
        # which can include a long-lived installer service. Wait only for our
        # installer entry point and preserve its native exit code.
        $null=$p.Handle
        if(-not $p.WaitForExit(300000)){throw ('Installer exceeded the 300-second wait limit. It was not terminated; wait for installation to finish before retrying. See '+$log)}
        $p.Refresh()
        $code=$p.ExitCode
        if($null -eq $code){throw ('Installer exited without a readable exit code. See '+$log)}
        if($code -eq 3010){$script:RebootRequired=$true}elseif($code -ne 0){throw ('Installer failed with exit code '+$code+'. See '+$log)}
    }finally{$p.Dispose()}
}
function Stop-Owned {
    $s=Get-Service -Name $script:ServiceName -ErrorAction SilentlyContinue
    if($s -and $s.Status -ne 'Stopped'){Stop-Service -Name $script:ServiceName;$s.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))}
    $t=Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if($t){Stop-ScheduledTask -TaskName $script:TaskName;Disable-ScheduledTask -TaskName $script:TaskName | Out-Null
        $until=[DateTime]::UtcNow.AddSeconds(15)
        do{$t=Get-ScheduledTask -TaskName $script:TaskName;if($t.State -ne 'Running'){break};Start-Sleep -Milliseconds 200}while([DateTime]::UtcNow -lt $until)
        if($t.State -eq 'Running'){throw 'The owned relay task did not stop.'}
    }
}
function Test-Greeting {
    $c=[Net.Sockets.TcpClient]::new()
    try{$async=$c.BeginConnect('127.0.0.1',17891,$null,$null);if(-not $async.AsyncWaitHandle.WaitOne(1500)){throw 'Local relay greeting timed out.'};$c.EndConnect($async);$s=$c.GetStream();$s.ReadTimeout=1500;$s.WriteTimeout=1500;$bytes=[byte[]](5,1,2);$s.Write($bytes,0,3);$a=$s.ReadByte();$b=$s.ReadByte();if($a -ne 5 -or $b -ne 2){throw 'The local relay did not require SOCKS authentication.'}}
    finally{$c.Dispose()}
}
function Get-Actual {
    $v=[ordered]@{state='not-installed';enabled=$false;owner_sid=$OwnerSid;request_id=$RequestId;desired_enabled=($Action -eq 'Enable');interface_name='';message='微信直连组件尚未安装。';supports_ipv6=$false;reboot_required=$script:RebootRequired;updated_at=[DateTime]::UtcNow.ToString('o');error=''}
    try{
        $owner=Get-OwnerMarker
        if(-not $owner){if(Get-ProxiService){throw 'An existing ProxiFyre service is not owned by this component.'};return $v}
        $s=Get-ProxiService
        $task=Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
        if(( -not $s -or $s.State -eq 'Stopped') -and (-not $task -or ($task.State -eq 'Disabled' -and -not $task.Settings.Enabled))){$v.state='disabled';$v.message='微信直连已关闭。';return $v}
        $manifestPath=Join-Path $script:Data 'manifest.json';Assert-Protected $manifestPath;$m=Read-Json $manifestPath
        if($m.owner_sid -cne $OwnerSid -or $m.supports_ipv6 -ne $false){throw 'Component manifest identity is invalid.'}
        $exe=Get-ProxiExe $s
        $config=Join-Path (Split-Path -Parent $exe) 'app-config.json'
        $relay=Join-Path $script:Bin 'WeChatDirectRelay.exe';$relayConfig=Join-Path $script:Data 'relay-config.json'
        if($m.proxi_exe -ine $exe -or $m.relay_exe -ine $relay){throw 'Component image paths do not match the manifest.'}
        foreach($p in @($relay,$relayConfig,$config)){Assert-Protected $p}
        if((Get-Hash $exe) -cne $m.proxi_exe_sha256 -or (Get-Hash $relay) -cne $m.relay_exe_sha256 -or (Get-Hash $config) -cne $m.proxi_config_sha256 -or (Get-Hash $relayConfig) -cne $m.relay_config_sha256){throw 'Component executable or configuration verification failed.'}
        $a=Get-Physical $m.interface_guid;$v.interface_name=[string]$a.Name
        if($s.State -ne 'Running' -or $s.StartMode -ne 'Auto' -or -not $task -or $task.State -ne 'Running' -or -not $task.Settings.Enabled){throw 'The owned service and relay task are not both running.'}
        if(@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ine $relay -or $task.Actions[0].Arguments -cne ('"'+$relayConfig+'"') -or $task.Principal.UserId -notin @('SYSTEM','S-1-5-18')){throw 'Relay task identity or action does not match.'}
        $heartbeat=Read-Json (Join-Path $script:Data 'relay-status.json')
        $age=([DateTime]::UtcNow-[DateTime]::Parse($heartbeat.updated_at).ToUniversalTime()).TotalSeconds
        if($age -lt -5 -or $age -ge 15 -or $heartbeat.state -ne 'running' -or $heartbeat.interface_guid -ine $m.interface_guid -or $heartbeat.listen_port -ne 17891 -or $heartbeat.supports_ipv6 -ne $false){throw 'The physical-interface relay heartbeat is not current and healthy.'}
        $log=Join-Path (Split-Path -Parent $exe) 'logs\logfile.txt'
        $lines=@(Get-Content -LiteralPath $log -Tail 1200)
        $association='Successfully associated '+$m.app_path+' to 127.0.0.1:17891 SOCKS5 proxy with protocols TCP, UDP and address families IPv4!'
        if(-not ($lines | Where-Object {$_.Contains($association)}) -or -not ($lines | Where-Object {$_.Contains('ProxiFyre Service is running...')})){throw 'The current engine log does not confirm the WeChat application rule.'}
        if((Get-Item -LiteralPath $log).LastWriteTimeUtc -lt [DateTime]::Parse($m.activated_at).ToUniversalTime().AddSeconds(-2)){throw 'The application-rule log predates activation.'}
        $v.state='running';$v.enabled=$true;$v.message='微信 IPv4 TCP/UDP 已通过独立应用分流使用物理网络。IPv6 不支持；此功能不是全 IP 防泄漏开关。'
    }catch{$v.state='error';$v.error=$_.Exception.Message;$v.message=$v.error}
    return $v
}
function Enable-Owned {
    $owner=Get-OwnerMarker;$existing=Get-ProxiService
    if($existing -and -not $owner){throw 'An existing ProxiFyre installation is not owned by OrangeDesk; it will not be changed.'}
    if(-not $owner -and (Test-Path -LiteralPath (Join-Path $script:Data 'manifest.json'))){throw 'Unowned component metadata already exists.'}
    if(-not $owner){foreach($folder in @($script:Data,$script:Bin)){if((Test-Path -LiteralPath $folder) -and @(Get-ChildItem -LiteralPath $folder -Force).Count -gt 0){throw 'A nonempty unowned component directory already exists.'}}}
    if(-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess){throw 'Use native 64-bit Windows PowerShell for the x64 components.'}
    $framework=Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release
    if($framework.Release -lt 461808){throw '.NET Framework 4.7.2 or later must already be installed.'}
    $runtimeReady=$false
    foreach($view in @([Microsoft.Win32.RegistryView]::Registry64,[Microsoft.Win32.RegistryView]::Registry32)){
        $registry=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,$view);$key=$null
        try{$key=$registry.OpenSubKey('SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64');if($key -and $key.GetValue('Installed') -eq 1){$version=[version](([string]$key.GetValue('Version')).TrimStart('v','V'));if($version -ge [version]'14.44.35211.0'){$runtimeReady=$true}}}finally{if($key){$key.Dispose()};$registry.Dispose()}
    }
    if(-not $runtimeReady){throw 'The compatible x64 Visual C++ runtime must already be installed (14.44.35211 or later).'}
    $app=Find-WeChat;$adapter=Get-Physical ''
    Protect-Directory $script:Data;Protect-Directory $script:Bin
    if(-not $owner){Write-Json (Join-Path $script:Data 'install-owner.json') ([ordered]@{component='OrangeDesk-WeChatDirect';owner_sid=$OwnerSid;created_at=[DateTime]::UtcNow.ToString('o')})}
    $null=Get-OwnerMarker
    Set-Progress '正在核验应用及系统组件。'
    if(-not (Get-Service -Name NDISRD -ErrorAction SilentlyContinue)){
        $msi=Get-Package 'Windows.Packet.Filter.3.6.2.1.x64.msi' 'https://github.com/wiresock/ndisapi/releases/download/v3.6.2/Windows.Packet.Filter.3.6.2.1.x64.msi' '9c388c0b7f189f7fa98720bae2caecf7d64f30910838b80b438ecf8956b8502c' 819200 $true
        Install-Package $msi 'ndis-install.log'
    }
    if(-not (Get-ProxiService)){
        $setup=Get-Package 'ProxiFyre-2.6.4-win-x64-setup.exe' 'https://github.com/wiresock/proxifyre/releases/download/v2.6.4/ProxiFyre-2.6.4-win-x64-setup.exe' 'afaf3ac8a2069f9653adb1b4a63b31d1edd8aa109ccdc367cf0d4462e78f5027' 2889730 $false
        Install-Package $setup 'proxifyre-setup.log'
    }
    $proxi=Get-ProxiExe (Get-ProxiService)
    $proxiConfig=Join-Path (Split-Path -Parent $proxi) 'app-config.json'
    $oldManifest=Join-Path $script:Data 'manifest.json'
    if(Test-Path -LiteralPath $proxiConfig){
        if(-not (Test-Path -LiteralPath $oldManifest)){throw 'An existing ProxiFyre live configuration is not owned by this component.'}
        $old=Read-Json $oldManifest
        if($old.owner_sid -cne $OwnerSid -or (Get-Hash $proxiConfig) -cne $old.proxi_config_sha256){throw 'The ProxiFyre configuration was modified externally; refusing to overwrite it.'}
    }
    Assert-Request;Stop-Owned
    Set-Progress '正在准备仅供微信使用的本地转发组件。'
    $source=Join-Path $PSScriptRoot 'WeChatDirectRelay.cs';$stableSource=Join-Path $script:Bin 'WeChatDirectRelay.cs'
    [IO.File]::Copy($source,$stableSource,$true);Protect-File $stableSource
    $relay=Join-Path $script:Bin 'WeChatDirectRelay.exe';$tempRelay=Join-Path $script:Bin ('relay-'+[guid]::NewGuid().ToString('N')+'.exe')
    try{Compile-Relay $stableSource $tempRelay;if([IO.File]::Exists($relay)){[IO.File]::Replace($tempRelay,$relay,[NullString]::Value)}else{[IO.File]::Move($tempRelay,$relay)}}finally{if([IO.File]::Exists($tempRelay)){[IO.File]::Delete($tempRelay)}}
    Protect-File $relay
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create();$random=[byte[]]::new(48);try{$rng.GetBytes($random)}finally{$rng.Dispose()}
    $username='orange-'+[Convert]::ToBase64String($random[0..15]);$password=[Convert]::ToBase64String($random[16..47]);[Array]::Clear($random,0,$random.Length)
    $guid=([guid]$adapter.InterfaceGuid).ToString('D');$relayConfig=Join-Path $script:Data 'relay-config.json'
    Write-Json $relayConfig ([ordered]@{listen_port=17891;interface_guid=$guid;username=$username;password=$password;status_path=(Join-Path $script:Data 'relay-status.json')});Protect-File $relayConfig
    Write-Json $proxiConfig ([ordered]@{logLevel='Info';bypassLan=$false;proxies=@([ordered]@{appNames=@($app);socks5ProxyEndpoint='127.0.0.1:17891';username=$username;password=$password;socks5Transport='TCP';supportedProtocols=@('TCP','UDP');supportedAddressFamilies=@('IPv4')});excludes=@('claude.exe','OrangeDesk.exe','FastOrange.exe','openvpn.exe',$relay)})
    Protect-File $proxiConfig
    $username=$null;$password=$null
    $activated=[DateTime]::UtcNow.ToString('o')
    $manifest=[ordered]@{owner_sid=$OwnerSid;app_path=$app;proxi_exe=$proxi;proxi_exe_sha256=(Get-Hash $proxi);relay_exe=$relay;relay_exe_sha256=(Get-Hash $relay);proxi_config_sha256=(Get-Hash $proxiConfig);relay_config_sha256=(Get-Hash $relayConfig);interface_guid=$guid;interface_name=[string]$adapter.Name;activated_at=$activated;supports_ipv6=$false}
    Write-Json $oldManifest $manifest
    $log=Join-Path (Split-Path -Parent $proxi) 'logs\logfile.txt'
    if([IO.File]::Exists($log)){[IO.File]::Move($log,(Join-Path $script:Data ('proxi-before-'+[DateTime]::UtcNow.ToString('yyyyMMddHHmmss')+'-'+[guid]::NewGuid().ToString('N')+'.log')))}
    $actionObject=New-ScheduledTaskAction -Execute $relay -Argument ('"'+$relayConfig+'"') -WorkingDirectory $script:Bin
    $trigger=New-ScheduledTaskTrigger -AtStartup
    $principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1))
    Assert-Request
    Register-ScheduledTask -TaskName $script:TaskName -Action $actionObject -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();$registered=$scheduler.GetFolder('\').GetTask($script:TaskName)
    $registered.SetSecurityDescriptor(('O:BAG:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;'+$OwnerSid+')'),0)
    Enable-ScheduledTask -TaskName $script:TaskName | Out-Null
    Start-ScheduledTask -TaskName $script:TaskName
    Set-Progress '正在等待物理接口转发组件就绪。'
    $until=[DateTime]::UtcNow.AddSeconds(20);$healthy=$false
    do{
        try{$h=Read-Json (Join-Path $script:Data 'relay-status.json');$age=([DateTime]::UtcNow-[DateTime]::Parse($h.updated_at).ToUniversalTime()).TotalSeconds
            if($h.state -eq 'running' -and $h.interface_guid -ieq $guid -and $age -ge -2 -and $age -lt 5){Test-Greeting;$healthy=$true;break}}catch{}
        Start-Sleep -Milliseconds 300
    }while([DateTime]::UtcNow -lt $until)
    if(-not $healthy){throw 'The authenticated physical-interface relay did not become ready.'}
    Assert-Request;Set-Service -Name $script:ServiceName -StartupType Automatic;Start-Service -Name $script:ServiceName
    Set-Progress '正在核验微信专用规则与后台服务。'
    $until=[DateTime]::UtcNow.AddSeconds(20)
    do{$actual=Get-Actual;if($actual.state -eq 'running'){return $actual};Start-Sleep -Milliseconds 400}while([DateTime]::UtcNow -lt $until)
    throw $actual.message
}

try{
    # Python can pass PowerShell 7's PSModulePath unchanged to powershell.exe.
    # Pin Get-Acl/Set-Acl/AuthentiCode to this host's native system module;
    # importing Security by name can otherwise load incompatible type data.
    # This does not alter the caller's or the machine's PSModulePath.
    Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1')) -ErrorAction Stop
    $StateDir=Local-Path $StateDir
    if([IO.Path]::GetFileName($StateDir.TrimEnd('\')) -ine 'wechat-direct'){throw 'StateDir must be the dedicated wechat-direct directory.'}
    $sid=[Security.Principal.SecurityIdentifier]::new($OwnerSid)
    if($sid.Value -cne $OwnerSid -or $OwnerSid -notmatch '^S-1-5-21-(?:\d+-){3}\d+$'){throw 'A canonical Windows user SID is required.'}
    if($ComponentCache){if($ComponentCache -notmatch '^[A-Za-z]:[\\/]') {throw 'ComponentCache must be an absolute local path.'};$ComponentCache=[IO.Path]::GetFullPath($ComponentCache)}
    if($Action -ne 'Status' -or $RequestId){$id=[guid]::Empty;if(-not [guid]::TryParseExact($RequestId,'D',[ref]$id) -or $id -eq [guid]::Empty -or $id.ToString('D') -cne $RequestId){throw 'A canonical nonzero request UUID is required.'}}
    $script:ParametersValidated=$true
    if($SelfTest){
        if($RequestId){Assert-Request}
        $null=Get-Acl -LiteralPath $PSHOME
        $null=Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256
        $tmp=Join-Path ([IO.Path]::GetTempPath()) ('Orange-WeChat-compile-'+[guid]::NewGuid().ToString('N')+'.exe')
        try{Compile-Relay (Join-Path $PSScriptRoot 'WeChatDirectRelay.cs') $tmp;if(-not [IO.File]::Exists($tmp)){throw 'Relay compile produced no executable.'}}finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
        $result=[ordered]@{state='self-test';enabled=$false;owner_sid=$OwnerSid;request_id=$RequestId;desired_enabled=($Action -eq 'Enable');supports_ipv6=$false;message='PS5.1 parameter, request and relay compilation checks passed; no system mutation.'}
    }elseif($Action -eq 'Status'){$result=Get-Actual;if($result.state -eq 'error'){$exitCode=1}}
    else{
        $identity=[Security.Principal.WindowsIdentity]::GetCurrent();$principal=[Security.Principal.WindowsPrincipal]::new($identity)
        if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator authorization is required.'}
        $script:Mutex=[Threading.Mutex]::new($false,('Local\OrangeDeskWeChatDirect-'+$OwnerSid))
        try{$script:Held=$script:Mutex.WaitOne(60000)}catch [Threading.AbandonedMutexException]{$script:Held=$true}
        if(-not $script:Held){throw 'Another WeChat direct setup is still running.'}
        Assert-Request;$script:VerifiedRequest=$true
        if($Action -eq 'Enable'){$result=Enable-Owned}
        else{
            $marker=Get-OwnerMarker
            if(-not $marker){if(Get-ProxiService){throw 'The existing ProxiFyre service is not owned by this component.'}}
            else{Set-Progress '正在停止微信专用分流。';Assert-Request;Stop-Owned;if(Get-ProxiService){Set-Service -Name $script:ServiceName -StartupType Manual}}
            $result=Get-Actual
            if($result.state -eq 'not-installed'){$result.state='disabled';$result.message='微信直连组件未启用。'}
            if($result.state -ne 'disabled' -or $result.enabled){throw 'WeChat direct components did not stop cleanly.'}
        }
    }
}catch{
    $failure=$_.Exception.Message;$exitCode=1
    $result=[ordered]@{state='error';enabled=$false;owner_sid=$OwnerSid;request_id=$RequestId;desired_enabled=($Action -eq 'Enable');supports_ipv6=$false;interface_name='';message=$failure;error=$failure;reboot_required=$script:RebootRequired;updated_at=[DateTime]::UtcNow.ToString('o')}
    if(-not $SelfTest -and $script:VerifiedRequest){try{$actual=Get-Actual;$result.enabled=[bool]$actual.enabled;$result.interface_name=$actual.interface_name}catch{}}
}finally{
    if(-not $SelfTest -and $script:ParametersValidated -and $Action -ne 'Status'){
        try{
            # A stale operation may emit its own receipt, never overwrite the latest status.
            $result.request_id=$RequestId;$result.owner_sid=$OwnerSid;$result.desired_enabled=($Action -eq 'Enable')
            $receipt=Join-Path $StateDir ('request-'+$RequestId+'.result.json')
            if(-not [IO.File]::Exists($receipt)){Write-Json $receipt $result}
            if($script:VerifiedRequest){Assert-Request;Write-Json (Join-Path $StateDir 'status.json') $result}
        }catch{$exitCode=1}
    }
    if($script:Held){$script:Mutex.ReleaseMutex()};if($script:Mutex){$script:Mutex.Dispose()}
}
$result | ConvertTo-Json -Depth 10 -Compress
exit $exitCode
