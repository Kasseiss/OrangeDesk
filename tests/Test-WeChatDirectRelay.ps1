[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSource = [IO.File]::ReadAllText((Join-Path $taskRoot 'resources\WeChatDirectRelay.cs'))
$taskHarness = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'WeChatDirectRelayHarness.cs'))
# Compiles and exercises only temporary loopback sockets and an internal fake
# binding provider. Never calls Program.Main, loads user config or installs.
Add-Type -TypeDefinition ($taskSource + [Environment]::NewLine + $taskHarness) -ReferencedAssemblies System.Web.Extensions.dll
[OrangeDeskWeChatRelay.RelayHarness]::Run()
