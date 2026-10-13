#requires -Version 5.1
[CmdletBinding()]
param([ValidateSet('Preview', 'Apply', 'Restore')][string]$Mode = 'Preview')
$ErrorActionPreference = 'Stop'
$script:StateDirectory = Join-Path $env:LOCALAPPDATA 'FastOrangeSplitRoutes'
$script:JournalPath = Join-Path $script:StateDirectory 'created-routes.jsonl'

function Convert-IPv4Number([string]$Address) {
    $bytes = [Net.IPAddress]::Parse($Address).GetAddressBytes()
    if ($bytes.Length -ne 4) { throw 'Expected an IPv4 address.' }
    return [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + $bytes[3]
}

function Test-RoutePrefix([string]$Prefix) {
    if ($Prefix -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]{1,2})$') { return $false }
    $parts = $Prefix.Split('/')
    $length = [int]$parts[1]
    if ($length -lt 8 -or $length -gt 32) { return $false }
    try {
        $number = Convert-IPv4Number $parts[0]
        return ($number % [math]::Pow(2, 32 - $length)) -eq 0
    } catch { return $false }
}

function Read-ChinaPrefixes {
    $dataPath = Join-Path $PSScriptRoot 'china-ipv4.txt'
    $metadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'china-ipv4.metadata.json') -Raw | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath $dataPath -Algorithm SHA256).Hash -cne $metadata.Sha256) { throw 'IP list checksum mismatch.' }
    $prefixes = @(Get-Content -LiteralPath $dataPath | Where-Object { $_.Trim() -ne '' })
    if ($prefixes.Count -lt 1000 -or $prefixes.Count -gt 20000 -or $prefixes.Count -ne $metadata.PrefixCount) { throw 'Unexpected IP list size.' }
    foreach ($prefix in $prefixes) {
        if (-not (Test-RoutePrefix $prefix)) { throw ('Invalid IP prefix: ' + $prefix) }
    }
    return $prefixes
}

function Get-DirectTarget($Routes) {
    $physical = @{}
    foreach ($adapter in @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' })) { $physical[[int]$adapter.ifIndex] = $adapter }
    $candidates = @($Routes | Where-Object {
        $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -ne '0.0.0.0' -and $physical.ContainsKey([int]$_.InterfaceIndex)
    })
    if ($candidates.Count -ne 1) { throw 'Expected one physical network default gateway. Resolve competing gateways before applying.' }
    $route = $candidates[0]
    return [pscustomobject]@{
        InterfaceIndex = [int]$route.InterfaceIndex; InterfaceAlias = $route.InterfaceAlias
        InterfaceGuid = [string]$physical[[int]$route.InterfaceIndex].InterfaceGuid
        NextHop = [string]$route.NextHop; RouteMetric = 5
    }
}

function Get-SplitPlan($Prefixes, $Routes, $Target) {
    # Exact existing prefixes are preserved. More-specific routes also retain priority.
    $existing = @{}
    foreach ($route in $Routes) { $existing[[string]$route.DestinationPrefix] = $true }
    foreach ($prefix in $Prefixes) {
        if (-not $existing.ContainsKey($prefix)) {
            [pscustomobject]@{
                DestinationPrefix = $prefix; InterfaceIndex = $Target.InterfaceIndex
                InterfaceGuid = $Target.InterfaceGuid; NextHop = $Target.NextHop; RouteMetric = $Target.RouteMetric
            }
        }
    }
}

function Get-RouteKey($Route) {
    return '{0}|{1}|{2}|{3}' -f $Route.DestinationPrefix, $Route.InterfaceIndex, $Route.NextHop, $Route.RouteMetric
}

function Read-OwnedRoutes {
    if (-not (Test-Path -LiteralPath $script:JournalPath)) { return }
    foreach ($line in Get-Content -LiteralPath $script:JournalPath) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $record = $line | ConvertFrom-Json
        if (-not (Test-RoutePrefix $record.DestinationPrefix) -or [int]$record.InterfaceIndex -le 0 -or
            [int]$record.RouteMetric -ne 5 -or [string]::IsNullOrWhiteSpace($record.InterfaceGuid) -or
            [string]::IsNullOrWhiteSpace($record.BootUtc)) { throw 'Invalid restore journal; no routes were changed.' }
        [void](Convert-IPv4Number $record.NextHop)
        $record
    }
}

function Undo-OwnedRoutes($Records, [string]$BootUtc) {
    $live = @{}
    foreach ($route in @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore)) { $live[(Get-RouteKey $route)] = $route }
    $adapters = @{}
    foreach ($adapter in @(Get-NetAdapter -IncludeHidden)) { $adapters[[int]$adapter.ifIndex] = [string]$adapter.InterfaceGuid }
    $removed = 0
    foreach ($record in $Records) {
        if ($record.BootUtc -cne $BootUtc -or -not $adapters.ContainsKey([int]$record.InterfaceIndex) -or
            $adapters[[int]$record.InterfaceIndex] -cne $record.InterfaceGuid) { continue }
        $key = Get-RouteKey $record
        if (-not $live.ContainsKey($key)) { continue }
        Remove-NetRoute -InputObject $live[$key] -Confirm:$false -ErrorAction Stop
        $live.Remove($key)
        $removed++
    }
    return $removed
}

function Install-SplitPlan($Plan, [string]$BootUtc) {
    [void][IO.Directory]::CreateDirectory($script:StateDirectory)
    [string[]]$previousRecords = @()
    if (Test-Path -LiteralPath $script:JournalPath) { $previousRecords = @(Get-Content -LiteralPath $script:JournalPath) }
    $created = [Collections.Generic.List[object]]::new()
    $writer = [IO.StreamWriter]::new($script:JournalPath, $true, [Text.UTF8Encoding]::new($false))
    try {
        foreach ($entry in $Plan) {
            $route = New-NetRoute -AddressFamily IPv4 -DestinationPrefix $entry.DestinationPrefix -InterfaceIndex $entry.InterfaceIndex `
                -NextHop $entry.NextHop -RouteMetric $entry.RouteMetric -PolicyStore ActiveStore -ErrorAction Stop
            $record = [pscustomobject]@{
                DestinationPrefix = $entry.DestinationPrefix; InterfaceIndex = $entry.InterfaceIndex
                InterfaceGuid = $entry.InterfaceGuid; NextHop = $entry.NextHop; RouteMetric = $entry.RouteMetric; BootUtc = $BootUtc
            }
            $created.Add($record)
            $writer.WriteLine(($record | ConvertTo-Json -Compress))
            $writer.Flush()
            if ($created.Count % 100 -eq 0) { Write-Host ('已添加 {0}/{1} 条国内直连路由。' -f $created.Count, $Plan.Count) }
        }
        return $created.Count
    } catch {
        $originalError = $_
        $writer.Dispose()
        try {
            [void](Undo-OwnedRoutes @($created.ToArray()) $BootUtc)
            [IO.File]::WriteAllLines($script:JournalPath, [string[]]$previousRecords, [Text.UTF8Encoding]::new($false))
        }
        catch { Write-Warning '部分路由未能撤销，请运行 Restore，或重启电脑清除临时路由。' }
        throw $originalError
    } finally { $writer.Dispose() }
}

function Show-TestRoutes {
    foreach ($address in @('223.5.5.5', '114.114.114.114', '8.8.8.8')) {
        $interfaces = @(Find-NetRoute -RemoteIPAddress $address | ForEach-Object { $_.InterfaceAlias } | Sort-Object -Unique)
        Write-Host ('{0} -> {1}' -f $address, ($interfaces -join ', '))
    }
}

function Start-ChinaSplit {
    if ($Mode -ne 'Preview') {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw '请右键对应的 cmd 文件，选择“以管理员身份运行”。'
        }
    }
    $mutex = [Threading.Mutex]::new($false, 'Local\FastOrangeSplitRoutes')
    $owned = $false
    try {
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw '另一个分流操作正在运行，请等待完成。' }
        $bootUtc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
        $records = @(Read-OwnedRoutes)
        if ($Mode -eq 'Restore') {
            $count = Undo-OwnedRoutes $records $bootUtc
            if (Test-Path -LiteralPath $script:JournalPath) {
                $archivePath = Join-Path $script:StateDirectory ('restored-' + [guid]::NewGuid().ToString('N') + '.jsonl')
                Move-Item -LiteralPath $script:JournalPath -Destination $archivePath -ErrorAction Stop
            }
            Write-Host ('已撤销本工具添加的 {0} 条路由。' -f $count)
            Show-TestRoutes
            return
        }
        $prefixes = @(Read-ChinaPrefixes)
        $routes = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore)
        $target = Get-DirectTarget $routes
        $halves = @($routes | Where-Object { $_.DestinationPrefix -in '0.0.0.0/1', '128.0.0.0/1' })
        if ($halves.Count -ne 2 -or $halves[0].InterfaceIndex -ne $halves[1].InterfaceIndex -or
            $halves[0].InterfaceIndex -eq $target.InterfaceIndex -or -not (Get-Process -Name FastOrange -ErrorAction SilentlyContinue)) {
            throw '请先连接快橙。当前 VPN 路由不符合已检查的配置，未修改。'
        }
        $liveKeys = @{}
        foreach ($route in $routes) { $liveKeys[(Get-RouteKey $route)] = $true }
        foreach ($record in $records) {
            if ($record.BootUtc -ceq $bootUtc -and $liveKeys.ContainsKey((Get-RouteKey $record)) -and
                ($record.InterfaceGuid -cne $target.InterfaceGuid -or $record.NextHop -cne $target.NextHop)) {
                throw '网络已切换，请先运行恢复脚本，再重新开启国内直连。'
            }
        }
        $plan = @(Get-SplitPlan $prefixes $routes $target)
        Write-Host ('本地出口：{0}；国内 IPv4 网段：{1}；拟新增路由：{2}。' -f $target.InterfaceAlias, $prefixes.Count, $plan.Count)
        if ($Mode -eq 'Preview') { Write-Host '当前仅预览，没有修改网络。'; Show-TestRoutes; return }
        [void][IO.Directory]::CreateDirectory($script:StateDirectory)
        $routes | Select-Object DestinationPrefix, InterfaceIndex, InterfaceAlias, NextHop, RouteMetric |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $script:StateDirectory ('before-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')) -Encoding UTF8
        $count = Install-SplitPlan $plan $bootUtc
        Write-Host ('已添加 {0} 条临时路由。重启电脑后会自动清除。' -f $count)
        Show-TestRoutes
        Write-Host '请实际检查国内网站和 ChatGPT。切换 Wi-Fi 后先恢复，再重新开启。'
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Start-ChinaSplit; exit 0 }
    catch { Write-Host ('未完成：' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
