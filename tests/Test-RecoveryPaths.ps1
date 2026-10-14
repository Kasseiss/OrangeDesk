#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
# An isolated fake native API: this file cannot query or change system proxies.
Add-Type -TypeDefinition @'
namespace FastOrangeCombined {
 public class RawProxyValue { public string Path,Name,Kind,Data; public bool Exists; }
 public class ProxyProfile { public uint Flags; public string Server,Bypass,AutoUrl; public RawProxyValue[] Raw; }
 public static class ProxySettings {
  public static ProxyProfile Current=new ProxyProfile(); public static int Restores;
  public static ProxyProfile Read(){return Current;}
  public static bool RestoreOwned(ProxyProfile before,ProxyProfile applied,string url){Restores++;return true;}
 }
}
'@
$temp=Join-Path $env:TEMP ('OrangeRecoveryTests-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($temp)
$originalLocal=$env:LOCALAPPDATA
$env:LOCALAPPDATA=Join-Path $temp 'local'
$StateRoot=Join-Path $temp 'profile\.OrangeDesk'
. (Join-Path $root '源码\CombinedRuntime.ps1') -StateRoot $StateRoot
$SessionPath=Join-Path $temp 'current-session'
[void][IO.Directory]::CreateDirectory($SessionPath)
$script:Checks=0
function Check([bool]$Condition,[string]$Message) {
 if(-not $Condition){throw ('FAIL '+$Message)}
 $script:Checks++;Write-Host ('PASS '+$Message)
}
function Record([string]$Pac) {
 $path='Software\Microsoft\Windows\CurrentVersion\Internet Settings'
 $raw=@()
 foreach($name in @('ProxyEnable','ProxyServer','ProxyOverride','AutoConfigURL','AutoDetect')){
  $raw+=@{Path=$path;Name=$name;Exists=$false;Kind='';Data=''}
 }
 foreach($name in @('DefaultConnectionSettings','SavedLegacySettings')){
  $raw+=@{Path=$path+'\Connections';Name=$name;Exists=$false;Kind='';Data=''}
 }
 $before=@{Flags=1;Server='';Bypass='';AutoUrl='';Raw=$raw}
 return @{Version=2;PacUrl=$Pac;Before=$before;Applied=@{Flags=5;Server='';Bypass='';AutoUrl=$Pac;Raw=$raw}}
}
function SaveRecord([string]$Path,$Value){
 [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path));Write-AtomicJson $Path $Value
}
try {
 $id='11111111111111111111111111111111'
 $pac='http://127.0.0.1:12345/fastorange/'+$id+'/proxy.pac'
 $relative='sessions\'+$id+'\proxy-before.json'
 $cached=Join-Path $env:LOCALAPPDATA ('Packages\ExamplePackage\LocalCache\Local\FastOrangeCombined\'+$relative)
 SaveRecord $cached (Record $pac)
 [FastOrangeCombined.ProxySettings]::Current.AutoUrl=$pac
 Check (Recover-TemporaryProxy) 'restore discovers matching record from previous package cache'
 Check ([FastOrangeCombined.ProxySettings]::Restores -eq 1) 'only validated matching record reaches fake restoration'
 [FastOrangeCombined.ProxySettings]::Current.AutoUrl='https://other.example/proxy.pac'
 Check (-not (Recover-TemporaryProxy) -and [FastOrangeCombined.ProxySettings]::Restores -eq 1) 'external PAC is left untouched'
 [FastOrangeCombined.ProxySettings]::Current.AutoUrl=$pac
 [IO.File]::Delete($cached)
 $refused=$false;try{Recover-TemporaryProxy}catch{$refused=$true}
 Check ($refused -and [FastOrangeCombined.ProxySettings]::Restores -eq 1) 'missing record refuses without changing proxy'
 SaveRecord $cached (Record ($pac+'different'))
 $refused=$false;try{Recover-TemporaryProxy}catch{$refused=$true}
 Check ($refused) 'a different PAC record is never accepted'
 SaveRecord $cached (Record $pac)
 $canonical=Join-Path $script:Root $relative
 [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($canonical))
 [IO.File]::WriteAllText($canonical,'broken',$script:Utf8)
 $refused=$false;try{Recover-TemporaryProxy}catch{$refused=$true}
 Check ($refused -and [FastOrangeCombined.ProxySettings]::Restores -eq 1) 'corrupt canonical record is not silently bypassed'
 [IO.File]::Delete($canonical)
 $invalid=Record $pac;$invalid.Before.Raw[0].Path='Software\UnrelatedApplication'
 SaveRecord $cached $invalid
 $refused=$false;try{Recover-TemporaryProxy}catch{$refused=$true}
 Check ($refused -and [FastOrangeCombined.ProxySettings]::Restores -eq 1) 'invalid registry paths cannot reach restoration'
 [IO.File]::WriteAllText($cached,(' '*262145),$script:Utf8)
 $refused=$false;try{Recover-TemporaryProxy}catch{$refused=$true}
 Check ($refused) 'oversized record is rejected before parsing'
 [IO.File]::Delete($cached)
 $legacy=Join-Path $env:LOCALAPPDATA ('FastOrangeCombined\'+$relative)
 SaveRecord $legacy (Record $pac)
 Check (Recover-TemporaryProxy) 'legacy desktop record remains supported'
 Check ($script:Root -eq (Join-Path $StateRoot 'network')) 'all new network state uses explicit shared state root'
 Write-Host ('ALL '+$script:Checks+' RECOVERY CHECKS PASSED; no real proxy operations.')
} finally {
 $env:LOCALAPPDATA=$originalLocal
 $absolute=[IO.Path]::GetFullPath($temp)
 $boundary=[IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')+'\'
 if($absolute.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($absolute).StartsWith('OrangeRecoveryTests-')){
  Remove-Item -LiteralPath $absolute -Recurse -Force
 }
}
