#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root '源码\CombinedRuntime.built.ps1')
Initialize-Automation
foreach ($app in @(Get-Process -Name FastOrange -ErrorAction SilentlyContinue)) {
    foreach ($handle in [FastOrangeCombined.NativeWindows]::ForProcess($app.Id)) {
        $window=[System.Windows.Automation.AutomationElement]::FromHandle($handle)
        $all=$window.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
        Write-Host ('Window='+$handle+' Descendants='+$all.Count)
        foreach ($element in $all) {
            $id=$element.Current.AutomationId
            $name=$element.Current.Name
            if ($id -match '(?i)connect' -or $name -in ($script:ConnectLabels+$script:ConnectedLabels+$script:BusyLabels)) {
                [pscustomobject]@{Id=$id;Name=$name;Type=$element.Current.ControlType.ProgrammaticName;Enabled=$element.Current.IsEnabled} | Format-List | Out-Host
            }
        }
    }
}
