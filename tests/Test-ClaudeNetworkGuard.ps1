#requires -Version 5.1
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$guard=[IO.File]::ReadAllText((Join-Path $projectRoot 'resources\ClaudeNetworkGuard.cs'),[Text.Encoding]::UTF8)
$harness=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'ClaudeNetworkGuardHarness.cs'),[Text.Encoding]::UTF8)
# Compile into one assembly to access internal mocks. No public Read/Install/
# Remove entry point is called, no native WFP engine is opened, and no UAC runs.
Add-Type -TypeDefinition ($guard+[Environment]::NewLine+$harness)
[void][OrangeDeskClaudeGuard.Tests.Harness]::Run()
