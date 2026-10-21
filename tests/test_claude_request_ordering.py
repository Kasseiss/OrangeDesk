"""PS5 fixture checks only: SelfTest never takes the mutex or invokes WFP."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 fixture")
class ClaudeRequestOrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-request-order-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder / "claude-protection"
        self.state.mkdir()
        resources = Path(__file__).resolve().parents[1] / "resources"
        for name in ("Set-ClaudeProtection.ps1", "ClaudeNetworkGuard.cs"):
            shutil.copyfile(resources / name, self.folder / name)
        self.request_id = str(uuid.uuid4())
        self.marker = self.state / "last-request.json"

    def run_fixture(self, marker):
        if marker is not None:
            self.marker.write_text(json.dumps(marker), encoding="utf-8")
        before = {item.name: item.read_bytes() for item in self.state.iterdir()}
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.folder / "Set-ClaudeProtection.ps1"), "-Action", "Status", "-SelfTest",
             "-StateDir", str(self.state), "-OwnerSid", "S-1-5-21-11-22-33-1001", "-RequestId", self.request_id],
            capture_output=True, encoding="utf-8-sig", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        after = {item.name: item.read_bytes() for item in self.state.iterdir()}
        self.assertEqual(before, after, "SelfTest must not write receipts or status")
        return result.returncode, json.loads(result.stdout)

    def test_current_pending_request_validates_without_mutation(self):
        code, value = self.run_fixture(dict(request_id=self.request_id, state="pending"))
        self.assertEqual(code, 0)
        self.assertEqual(value["state"], "self-test")
        self.assertIsNone(value["enabled"])

    def test_superseded_request_is_refused(self):
        code, value = self.run_fixture(dict(request_id=str(uuid.uuid4()), state="pending"))
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_completed_request_is_refused(self):
        code, value = self.run_fixture(dict(request_id=self.request_id, state="complete"))
        self.assertEqual(code, 1)
        self.assertIn("no longer", value["error"])

    def test_missing_request_marker_is_refused(self):
        code, value = self.run_fixture(None)
        self.assertEqual(code, 1)
        self.assertEqual(value["state"], "failed")

    def test_oversized_marker_is_refused(self):
        code, value = self.run_fixture(dict(request_id=self.request_id, state="pending", padding="x" * 65536))
        self.assertEqual(code, 1)
        self.assertIn("64KB", value["error"])

    def test_other_marker_fields_cannot_choose_policy(self):
        code, value = self.run_fixture(dict(request_id=self.request_id, state="pending", enabled=True,
                                            apps=["untrusted.exe"], interface_luid=1, action="Enable"))
        self.assertEqual(code, 0)
        self.assertEqual(value["apps"], [])
        self.assertIsNone(value["interface_luid"])


if __name__ == "__main__":
    unittest.main()
