#requires -Version 5.1
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
try {
    $source=Join-Path $PSScriptRoot '快橙伴航.exe'
    $install=Join-Path $env:LOCALAPPDATA 'Programs\OrangeDesk'
    $target=Join-Path $install 'OrangeDesk.exe'
    if(-not (Test-Path -LiteralPath $source -PathType Leaf)){throw '找不到随附的快橙伴航.exe。请保持更新器和新版程序在同一个文件夹。'}
    $version=(Get-Item -LiteralPath $source).VersionInfo.ProductVersion
    if($version -ne '1.3.3'){throw '新版程序的版本不正确，更新已停止。'}
    $sourceHash=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $expected=(Get-Content -LiteralPath (Join-Path $PSScriptRoot '程序SHA256.txt') -Raw).Trim()
    if($sourceHash -ne $expected){throw '新版程序完整性检查失败，更新已停止。'}
    Write-Host '正在正常关闭旧版伴航并保存程序备份。快橙本体和所选VPN节点保持原状态。'
    if(Test-Path -LiteralPath $target -PathType Leaf) {
        $old=Start-Process -FilePath $target -ArgumentList @('shutdown','--close-ui') -WindowStyle Hidden -PassThru
        if(-not $old.WaitForExit(60000)){throw '旧版仍在保存或恢复设置，请等待完成后重新运行更新器。'}
        if($old.ExitCode -ne 0){throw '旧版未确认正常退出，尚未替换程序。请在旧版中停止伴航后重试。'}
        $backup=Join-Path $install ('backups\'+(Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination (Join-Path $backup 'OrangeDesk.exe')
    } else {New-Item -ItemType Directory -Path $install -Force | Out-Null}
    $pending=Join-Path $install ('OrangeDesk.update.'+[Guid]::NewGuid().ToString('N')+'.exe')
    Copy-Item -LiteralPath $source -Destination $pending
    if((Get-FileHash -LiteralPath $pending -Algorithm SHA256).Hash -ne $sourceHash){throw '复制后的程序校验失败，尚未替换旧版。'}
    if(Test-Path -LiteralPath $target){[IO.File]::Replace($pending,$target,[NullString]::Value)}else{[IO.File]::Move($pending,$target)}
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '网络监测使用说明.txt') -Destination (Join-Path $install '网络监测使用说明.txt')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Claude出口保护使用说明.txt') -Destination (Join-Path $install 'Claude出口保护使用说明.txt')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '微信直连使用说明.txt') -Destination (Join-Path $install '微信直连使用说明.txt')
    Start-Process -FilePath $target -WindowStyle Hidden | Out-Null
    Write-Host '已更新为1.3.3新版界面。微信直连、Claude出口保护及网络监测的原有设置已保留。'
} catch {Write-Host ('更新未完成：'+$_.Exception.Message) -ForegroundColor Red;exit 1}
