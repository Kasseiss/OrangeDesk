#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ClaudePath,
    [string]$DataRoot = $(if($env:ORANGEDESK_DATA_HOME){$env:ORANGEDESK_DATA_HOME}else{Join-Path $env:USERPROFILE '.OrangeDesk'}),
    [string]$SessionPath,
    [switch]$CheckOnly,
    [switch]$SingaporeNodeConfirmed,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$ClaudeArguments
)
$ErrorActionPreference='Stop'
$OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding

function Get-ProtectedProxy {
    $sessions=Join-Path $DataRoot 'network\sessions'
    if($SessionPath){
        $selected=[IO.Path]::GetFullPath($SessionPath)
        $parent=[IO.Path]::GetDirectoryName($selected)
        if([IO.Path]::GetFileName($selected) -notmatch '^[a-f0-9]{32}$' -or
            -not [string]::Equals($parent,[IO.Path]::GetFullPath($sessions),[StringComparison]::OrdinalIgnoreCase)){
            throw '指定的伴航会话不属于当前用户的数据目录，Claude未启动。'
        }
        $directories=@(Get-Item -LiteralPath $selected -ErrorAction SilentlyContinue | Where-Object PSIsContainer)
    } else {
        $directories=@(Get-ChildItem -LiteralPath $sessions -Directory -ErrorAction SilentlyContinue)
    }
    $candidates=@($directories |
        Where-Object {$_.Name -match '^[a-f0-9]{32}$'} |
        ForEach-Object {Get-Item -LiteralPath (Join-Path $_.FullName 'split-status.json') -ErrorAction SilentlyContinue} |
        Where-Object {$_.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddSeconds(-30)} |
        Sort-Object LastWriteTimeUtc -Descending)
    foreach($file in $candidates){
        try {
            $status=Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if($status.StrictVpnBinding -ne $true -or $status.PacDirectFallback -ne $false){continue}
            $recordPath=Join-Path $file.DirectoryName 'proxy-before.json'
            $record=Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            $pac=[uri]$record.PacUrl
            if($pac.Scheme -ne 'http' -or $pac.Host -ne '127.0.0.1' -or $pac.UserInfo -or
                $pac.AbsolutePath -notmatch '^/fastorange/[a-f0-9]{32}/proxy\.pac$'){continue}
            if($pac.AbsolutePath -cne ('/fastorange/'+$file.Directory.Name+'/proxy.pac')){continue}
            $health=[uri]($pac.AbsoluteUri -replace '/proxy\.pac$','/vpn-status.json')
            # This request is loopback only, bypasses other proxies and refuses redirects.
            $request=[Net.HttpWebRequest]::Create($health)
            $request.Proxy=$null;$request.AllowAutoRedirect=$false
            $request.Timeout=2000;$request.ReadWriteTimeout=2000
            $response=$request.GetResponse()
            try {
                if([int]$response.StatusCode -ne 200){continue}
                $reader=[IO.StreamReader]::new($response.GetResponseStream())
                try {
                    $buffer=New-Object char[] 4097
                    $count=$reader.ReadBlock($buffer,0,$buffer.Length)
                    if($count -gt 4096){continue}
                    $proof=(-join $buffer[0..($count-1)]) | ConvertFrom-Json
                } finally {$reader.Dispose()}
            } finally {$response.Dispose()}
            if($proof.vpnBindingEnforced -ne $true -or $proof.pacDirectFallback -ne $false -or
                $proof.vpnReady -ne $true -or $proof.claudeRouteIsVpn -ne $true -or
                [int]$proof.vpnInterfaceIndex -le 0){continue}
            $address=$null
            if(-not [Net.IPAddress]::TryParse([string]$proof.vpnAddress,[ref]$address) -or
                $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork){continue}
            return [pscustomobject]@{Proxy=('http://127.0.0.1:'+$pac.Port);InterfaceIndex=[int]$proof.vpnInterfaceIndex;Address=$address.ToString()}
        } catch {continue}
    }
    throw '未找到已就绪的出口保护版伴航。请先运行修复版、连接新加坡节点，再启动 Claude；本启动器不会回退直连。'
}

foreach($argument in $ClaudeArguments){
    if($argument -match '^--settings(?:=|$)'){throw '此入口保留 --settings 用于代理保护，请不要另外覆盖该参数。'}
}
$binding=Get-ProtectedProxy
if($CheckOnly){
    [pscustomobject]@{Ready=$true;Proxy=$binding.Proxy;VpnInterfaceIndex=$binding.InterfaceIndex;VpnAddress=$binding.Address;RegionVerified=$false;ExternalRequestsSent=$false} | ConvertTo-Json -Compress
    return
}
if($env:ANTHROPIC_BASE_URL){
    $base=[uri]$env:ANTHROPIC_BASE_URL
    if($base.Host -ne 'api.anthropic.com'){throw '当前使用自定义 API 地址，尚未验证该地址的分流规则；本入口停止启动。'}
}
if(-not $SingaporeNodeConfirmed){
    Write-Host '此入口锁定当前快橙隧道，不识别节点国家。' -ForegroundColor Yellow
    if((Read-Host '请先在快橙选择新加坡节点；确认后输入 SG') -cne 'SG'){throw '未确认新加坡节点，Claude 未启动。'}
}
if(-not $ClaudePath){
    $command=Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1
    if($command){$ClaudePath=$command.Source}
    foreach($candidate in @(
        (Join-Path $env:USERPROFILE '.local\bin\claude.exe'),
        (Join-Path $env:APPDATA 'npm\claude.cmd'),
        (Join-Path $env:SystemDrive 'tmp\node_global\node_modules\@anthropic-ai\claude-code\bin\claude.exe')
    )){if(-not $ClaudePath -and (Test-Path -LiteralPath $candidate)){$ClaudePath=$candidate}}
}
if(-not $ClaudePath -or -not (Test-Path -LiteralPath $ClaudePath -PathType Leaf)){throw '找不到现有 Claude 安装。请通过 -ClaudePath 指定路径，不会自动下载安装。'}

$proxyEnvironment=@{HTTPS_PROXY=$binding.Proxy;HTTP_PROXY=$binding.Proxy;ALL_PROXY='';NO_PROXY='localhost,127.0.0.1,::1'}
$before=@{}
$settingsPath=Join-Path ([IO.Path]::GetTempPath()) ('OrangeClaudeProxy-'+[guid]::NewGuid().ToString('N')+'.json')
try {
    foreach($name in $proxyEnvironment.Keys){
        $before[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
        [Environment]::SetEnvironmentVariable($name,$proxyEnvironment[$name],'Process')
    }
    # Session-only settings override project/user proxy values without editing them.
    [IO.File]::WriteAllText($settingsPath,(@{env=$proxyEnvironment}|ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))
    Write-Host ('Claude 通过 '+$binding.Proxy+' 使用当前 VPN；隧道不可用时连接失败，不自动直连。')
    & $ClaudePath --settings $settingsPath @ClaudeArguments
    $resultCode=$LASTEXITCODE
    if($null -eq $resultCode){$resultCode=0}
} finally {
    foreach($name in $before.Keys){[Environment]::SetEnvironmentVariable($name,$before[$name],'Process')}
    if(Test-Path -LiteralPath $settingsPath){Remove-Item -LiteralPath $settingsPath -Force}
}
if($resultCode -ne 0){throw ('Claude 退出，代码 '+$resultCode+'。未尝试更换代理或直连。')}
