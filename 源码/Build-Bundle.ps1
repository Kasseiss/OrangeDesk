#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$source=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'CombinedRuntime.ps1'),[Text.Encoding]::UTF8)
$native=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'RouteApi.cs'),[Text.Encoding]::UTF8)
$bridge=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'DomainBridge.cs'),[Text.Encoding]::UTF8)
$proxy=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'ProxySettings.cs'),[Text.Encoding]::UTF8)
function Pack-Data([string]$Path) {
    $bytes=[IO.File]::ReadAllBytes($Path)
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $hash=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','') } finally { $sha.Dispose() }
    $output=[IO.MemoryStream]::new()
    $gzip=[IO.Compression.GZipStream]::new($output,[IO.Compression.CompressionMode]::Compress,$true)
    $gzip.Write($bytes,0,$bytes.Length); $gzip.Dispose()
    $encoded=[Convert]::ToBase64String($output.ToArray()); $output.Dispose()
    return @{Hash=$hash;Gzip=$encoded}
}
$china=Pack-Data (Join-Path $root '分流工具\china-ipv4.txt')
$domains=Pack-Data (Join-Path $root '分流工具\china-domains.txt')
$source=$source.Replace('__NATIVE_CODE__',$native).Replace('__CHINA_HASH__',$china.Hash).Replace('__CHINA_GZIP__',$china.Gzip)
$source=$source.Replace('__BRIDGE_CODE__',$bridge).Replace('__PROXY_CODE__',$proxy).Replace('__DOMAINS_HASH__',$domains.Hash).Replace('__DOMAINS_GZIP__',$domains.Gzip)
$license=[IO.File]::ReadAllText((Join-Path $root '分流工具\域名规则-LICENSE.txt'),[Text.Encoding]::UTF8)
$source+="`n<#`nDomestic domain rules: https://github.com/Loyalsoldier/v2ray-rules-dat (release/direct-list.txt)`nDomain rule license follows. Source files are kept next to the development builder.`n"+$license+"`n#>`n"
$header=@'
@echo off
setlocal
title FastOrange AutoReconnect + Domestic Websites
set "FO_BUNDLE_FILE=%~f0"
set "FO_BUNDLE_MODE=%~1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $t=[IO.File]::ReadAllText($env:FO_BUNDLE_FILE,[Text.Encoding]::UTF8); $m='#__FASTORANGE_PAYLOAD__'; $p=$t.Substring($t.LastIndexOf($m)+$m.Length); $s=Join-Path $env:LOCALAPPDATA ('FastOrangeCombined\sessions\'+[guid]::NewGuid().ToString('N')); [void][IO.Directory]::CreateDirectory($s); $f=Join-Path $s 'runtime.ps1'; [IO.File]::WriteAllText($f,$p,[Text.UTF8Encoding]::new($true)); [IO.File]::WriteAllText((Join-Path $s 'bundle.json'),(@{Path=$env:FO_BUNDLE_FILE}|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false)); if($env:FO_BUNDLE_MODE -eq '--preview'){ & $f -Role Preview -SessionPath $s }elseif($env:FO_BUNDLE_MODE -eq '--restore'){ & $f -Role Restore -SessionPath $s }else{ & $f -Role Control -SessionPath $s } } catch { Write-Host $_.Exception.Message; Start-Sleep -Seconds 12; exit 1 }"
exit /b %errorlevel%
#__FASTORANGE_PAYLOAD__
'@
# ASCII batch header; UTF-8 payload without file BOM, converted to BOM-bearing PS1 at runtime.
$bundle=($header -replace '\r?\n',"`r`n")+"`r`n"+($source -replace '\r?\n',"`r`n")
$destination=Join-Path $root '快橙一键启动.cmd'
[IO.File]::WriteAllText($destination,$bundle,[Text.UTF8Encoding]::new($false))
# Built runtime is a development/test artifact, not a dependency of the CMD.
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'CombinedRuntime.built.ps1'),$source,[Text.UTF8Encoding]::new($true))
Write-Host ('已生成单文件入口：'+$destination)
