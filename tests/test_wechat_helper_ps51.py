"""Real PS5.1 compile/request fixtures; no installer, task, service or network use."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 required")
class WeChatHelperTests(unittest.TestCase):
    SID = "S-1-5-21-11-22-33-1001"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-wechat-ps51-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder / "wechat-direct"
        self.state.mkdir()
        resources = Path(__file__).resolve().parents[1] / "resources"
        for name in ("Set-WeChatDirect.ps1", "WeChatDirectRelay.cs"):
            shutil.copyfile(resources / name, self.folder / name)
        self.request_id = str(uuid.uuid4())

    def invoke(self, action="Status", marker=None, sid=None, request=True, state=None, environment=None):
        if marker is not None:
            (self.state / "last-request.json").write_text(json.dumps(marker), encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(self.folder / "Set-WeChatDirect.ps1"), "-SelfTest", "-Action", action,
                   "-StateDir", str(state or self.state), "-OwnerSid", sid or self.SID]
        if request:
            command += ["-RequestId", self.request_id]
        result = subprocess.run(command, capture_output=True, encoding="utf-8-sig", errors="replace",
                                timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                env=environment)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()},
                         "SelfTest must not write status, receipts, or privileged state")
        return result.returncode, json.loads(result.stdout)

    def marker(self, enabled):
        return dict(state="pending", request_id=self.request_id, owner_sid=self.SID, desired_enabled=enabled)

    def test_status_compiles_real_relay_without_system_actions(self):
        code, value = self.invoke(request=False)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["state"], "self-test")
        self.assertIs(value["supports_ipv6"], False)

    def test_python_child_ps51_with_inherited_ps7_modules_can_read_acl(self):
        ps7 = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PowerShell/7/Modules"
        if not (ps7 / "Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1").is_file():
            self.skipTest("PowerShell 7 module path fixture is not installed")
        environment = dict(os.environ)
        inherited = environment.get("PSModulePath", "")
        environment["PSModulePath"] = str(ps7) + os.pathsep + inherited
        before = environment["PSModulePath"]
        code, value = self.invoke(request=False, environment=environment)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["state"], "self-test")
        self.assertEqual(environment["PSModulePath"], before)

    def test_enable_current_request_compiles(self):
        code, value = self.invoke("Enable", self.marker(True))
        self.assertEqual(code, 0, value)
        self.assertIs(value["desired_enabled"], True)

    def test_disable_current_request_compiles(self):
        code, value = self.invoke("Disable", self.marker(False))
        self.assertEqual(code, 0, value)
        self.assertIs(value["desired_enabled"], False)

    def test_superseded_request_refused(self):
        marker = self.marker(True)
        marker["request_id"] = str(uuid.uuid4())
        code, value = self.invoke("Enable", marker)
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_opposite_desired_state_refused(self):
        code, value = self.invoke("Disable", self.marker(True))
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_other_owner_refused(self):
        marker = self.marker(True)
        marker["owner_sid"] = "S-1-5-21-11-22-33-1002"
        code, value = self.invoke("Enable", marker)
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_string_desired_state_refused(self):
        marker = self.marker(True)
        marker["desired_enabled"] = "true"
        code, value = self.invoke("Enable", marker)
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_missing_request_refused(self):
        code, value = self.invoke("Enable", request=False)
        self.assertEqual(code, 1)
        self.assertIn("UUID", value["error"])

    def test_wrong_state_directory_refused(self):
        code, value = self.invoke(request=False, state=self.folder)
        self.assertEqual(code, 1)
        self.assertIn("dedicated", value["error"])

    def test_noncanonical_sid_refused(self):
        code, value = self.invoke(sid="S-1-5-18", request=False)
        self.assertEqual(code, 1)
        self.assertIn("user SID", value["error"])

    def status_fixture(self, scenario):
        """Load only function ASTs, then replace every host-state dependency."""
        script = r'''
$ErrorActionPreference='Stop'
$source=$args[0];$scenario=$args[1]
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Parse error'}
$functions=$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)
. ([scriptblock]::Create(($functions | ForEach-Object {$_.Extent.Text}) -join "`n"))
$OwnerSid='S-1-5-21-11-22-33-1001';$RequestId='';$Action='Status'
$script:Data='C:\mock-data';$script:Bin='C:\mock-bin';$script:TaskName='OrangeDesk-WeChatDirect'
$script:RebootRequired=$false
function Assert-Protected([string]$Path){}
function Get-OwnerMarker {if($scenario -eq 'foreign'){return $null};return @{owner_sid=$OwnerSid}}
function Get-ProxiService {return @{State=$(if($scenario -eq 'disabled'){'Stopped'}else{'Running'});StartMode='Auto'}}
function Get-ProxiExe {return 'C:\mock-proxi\ProxiFyre.exe'}
function Get-Physical {return @{Name='Wi-Fi'}}
function Get-ScheduledTask {return [pscustomobject]@{State=$(if($scenario -eq 'disabled'){'Disabled'}else{'Running'});Settings=@{Enabled=($scenario -ne 'disabled')};Actions=@(@{Execute='C:\mock-bin\WeChatDirectRelay.exe';Arguments='"C:\mock-data\relay-config.json"'});Principal=@{UserId='SYSTEM'}}}
function Get-Hash([string]$Path){if($scenario -eq 'hash' -and $Path.EndsWith('app-config.json')){return 'changed'};return 'good'}
function Read-Json([string]$Path){
    if($Path.EndsWith('manifest.json')){return @{owner_sid=$OwnerSid;supports_ipv6=$false;proxi_exe='C:\mock-proxi\ProxiFyre.exe';relay_exe='C:\mock-bin\WeChatDirectRelay.exe';proxi_exe_sha256='good';relay_exe_sha256='good';proxi_config_sha256='good';relay_config_sha256='good';interface_guid='test-guid';activated_at=[DateTime]::UtcNow.AddMinutes(-1).ToString('o');app_path='C:\Tencent\Weixin.exe'}}
    return @{state='running';interface_guid='test-guid';listen_port=17891;supports_ipv6=$false;updated_at=$(if($scenario -eq 'stale'){[DateTime]::UtcNow.AddMinutes(-2).ToString('o')}else{[DateTime]::UtcNow.ToString('o')})}
}
function Get-Content {if($scenario -ne 'log'){'Successfully associated C:\Tencent\Weixin.exe to 127.0.0.1:17891 SOCKS5 proxy with protocols TCP, UDP and address families IPv4!'};'ProxiFyre Service is running...'}
function Get-Item {return @{LastWriteTimeUtc=[DateTime]::UtcNow}}
Get-Actual | ConvertTo-Json -Compress
'''
        runner = self.folder / "mock-status.ps1"
        runner.write_text(script, encoding="utf-8-sig")
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(runner), str(self.folder / "Set-WeChatDirect.ps1"), scenario],
                                capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_status_requires_every_running_evidence(self):
        good = self.status_fixture("running")
        self.assertEqual(good["state"], "running", good)
        self.assertIs(good["enabled"], True)
        for scenario in ("hash", "log", "stale", "foreign"):
            with self.subTest(scenario=scenario):
                value = self.status_fixture(scenario)
                self.assertEqual(value["state"], "error", value)
                self.assertIs(value["enabled"], False)

    def test_status_verifies_disabled_without_trusting_saved_status(self):
        value = self.status_fixture("disabled")
        self.assertEqual(value["state"], "disabled", value)
        self.assertIs(value["enabled"], False)

    def installer_fixture(self, name, exit_code):
        script = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors)
$function=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Install-Package'},$false)
. ([scriptblock]::Create($function.Extent.Text))
$script:Data='C:\mock-data';$script:RebootRequired=$false;$script:Exit=[int]$args[2]
function Assert-Request {}
function Start-Process {param($FilePath,$ArgumentList,[switch]$Wait,[switch]$PassThru,$WindowStyle)
    $script:Start=@{file=$FilePath;arguments=$ArgumentList;wait=[bool]$Wait;pass_thru=[bool]$PassThru;window=$WindowStyle}
    $process=[pscustomobject]@{ExitCode=$script:Exit;Handle=[IntPtr]1}
    $process | Add-Member ScriptMethod WaitForExit {param($milliseconds) $script:Start.wait_milliseconds=$milliseconds;return ($script:Exit -ne -1)}
    $process | Add-Member ScriptMethod Refresh {$script:Start.refreshed=$true}
    $process | Add-Member ScriptMethod Dispose {$script:Start.disposed=$true}
    return $process
}
$errorText=''
try {Install-Package ('C:\mock-cache\'+$args[1]) 'setup.log'}catch{$errorText=$_.Exception.Message}
@{start=$script:Start;reboot=$script:RebootRequired;error=$errorText} | ConvertTo-Json -Compress
'''
        runner = self.folder / "mock-installer.ps1"
        runner.write_text(script, encoding="utf-8-sig")
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(runner), str(self.folder / "Set-WeChatDirect.ps1"), name, str(exit_code)],
                                capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_official_setup_uses_supported_burn_command(self):
        value = self.installer_fixture("ProxiFyre-2.6.4-win-x64-setup.exe", 0)
        self.assertEqual(value["error"], "")
        self.assertEqual(value["start"]["arguments"], '/install /quiet /norestart /log "C:\\mock-data\\setup.log"')
        self.assertFalse(value["start"]["wait"])
        self.assertTrue(value["start"]["pass_thru"])
        self.assertEqual(value["start"]["wait_milliseconds"], 300000)
        self.assertTrue(value["start"]["disposed"])
        self.assertEqual(value["start"]["window"], "Hidden")
        self.assertNotIn("BURNMSIINSTALL", value["start"]["arguments"])
        self.assertNotIn("PROXIFYRE_SETUP_CHAIN", value["start"]["arguments"])

    def test_setup_reboot_request_is_reported_without_reboot(self):
        value = self.installer_fixture("ProxiFyre-2.6.4-win-x64-setup.exe", 3010)
        self.assertTrue(value["reboot"])
        self.assertEqual(value["error"], "")

    def test_setup_failure_is_not_success(self):
        value = self.installer_fixture("ProxiFyre-2.6.4-win-x64-setup.exe", 1603)
        self.assertIn("1603", value["error"])
        self.assertFalse(value["reboot"])

    def test_shared_ndis_msi_still_uses_msiexec(self):
        value = self.installer_fixture("Windows.Packet.Filter.3.6.2.1.x64.msi", 0)
        self.assertTrue(value["start"]["file"].lower().endswith("msiexec.exe"))
        self.assertEqual(value["start"]["arguments"], '/i "C:\\mock-cache\\Windows.Packet.Filter.3.6.2.1.x64.msi" /qn /norestart /L*v "C:\\mock-data\\setup.log"')
        self.assertFalse(value["start"]["wait"])
        self.assertEqual(value["start"]["wait_milliseconds"], 300000)

    def test_installer_timeout_reports_failure_without_killing(self):
        for name in ("ProxiFyre-2.6.4-win-x64-setup.exe", "Windows.Packet.Filter.3.6.2.1.x64.msi"):
            with self.subTest(installer=name):
                value = self.installer_fixture(name, -1)
                self.assertIn("300-second wait limit", value["error"])
                self.assertIn("not terminated", value["error"])
                self.assertTrue(value["start"]["disposed"])
                self.assertFalse(value["reboot"])


if __name__ == "__main__":
    unittest.main()
