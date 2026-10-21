"""Read-only PS5 Enable/SelfTest fixtures; fake PE files are never executed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 fixture")
class ClaudeAppPathsPs51Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[1]
        installed = cls.repo / ".build/guard-kernel-check/installed.json"
        if not installed.is_file():
            raise unittest.SkipTest("Isolated fake executable fixture is unavailable")
        cls.fake = Path(json.loads(installed.read_text(encoding="utf-8-sig"))["exe"])
        if not cls.fake.is_file():
            raise unittest.SkipTest("Isolated fake executable has already been cleaned up")
        cls.powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Adapter metadata only; no network traffic, interface changes or WFP.
        result = subprocess.run([str(cls.powershell), "-NoProfile", "-NonInteractive", "-Command",
                                 "$t=@(Get-NetAdapter -IncludeHidden | Where-Object {$_.Status -eq 'Up' -and $_.InterfaceDescription -like '*TAP-Windows*'});if($t.Count -ne 1){exit 7};([guid]$t[0].InterfaceGuid).ToString('D')"],
                                capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise unittest.SkipTest("A unique Up TAP is required for Enable/SelfTest metadata validation")
        cls.guid = result.stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-app-array-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder / "claude-protection"
        self.state.mkdir()
        self.apps = []
        for name in ("one", "two"):
            destination = self.folder / name / "claude.exe"
            destination.parent.mkdir()
            shutil.copyfile(self.fake, destination)
            self.apps.append(str(destination))
        for name in ("Set-ClaudeProtection.ps1", "ClaudeNetworkGuard.cs"):
            shutil.copyfile(self.repo / "resources" / name, self.folder / name)

    def run_fixture(self, paths):
        request = self.state / "apps.json"
        request.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [str(self.powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.folder / "Set-ClaudeProtection.ps1"), "-Action", "Enable", "-SelfTest",
             "-StateDir", str(self.state), "-OwnerSid", "S-1-5-21-11-22-33-1001",
             "-VpnGuid", self.guid, "-AppPathsJsonPath", str(request)],
            capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual([item.name for item in self.state.iterdir()], ["apps.json"])
        return result.returncode, json.loads(result.stdout)

    def test_single_native_path(self):
        code, value = self.run_fixture(self.apps[:1])
        self.assertEqual(code, 0, value)
        self.assertEqual(value["state"], "self-test")

    def test_multiple_native_paths(self):
        code, value = self.run_fixture(self.apps)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["state"], "self-test")

    def test_nested_array_is_rejected(self):
        code, value = self.run_fixture([self.apps[:1]])
        self.assertEqual(code, 1)
        self.assertIn("strings only", value["error"])


if __name__ == "__main__":
    unittest.main()
