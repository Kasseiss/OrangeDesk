"""Network protection lifecycle with mocked UAC/native policy; no real rules."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.claude_protection import ClaudeProtection, ProtectionError

SID = "S-1-5-21-123-456-789-1001"


class ClaudeProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-protection-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources"
        self.resources.mkdir()
        for name in ("Set-ClaudeProtection.ps1", "ClaudeNetworkGuard.cs"):
            (self.resources / name).write_text("fake helper; never executed", encoding="utf-8")
        self.run = Mock()
        self.elevate = Mock(return_value=42)
        self.manager = ClaudeProtection(self.root / "data", self.resources, "powershell.exe", SID,
                                        run=self.run, elevate=self.elevate, interface_guid=lambda _: "12345678-1234-1234-1234-123456789abc")
        self.session = self.manager.root / "network/sessions" / ("a" * 32)
        self.session.mkdir(parents=True)
        self.proof = dict(vpnInterfaceIndex=17)

    def enable(self):
        with patch.object(self.manager, "_proof", return_value=self.proof), patch.object(self.manager, "_native_apps", return_value=[r"C:\fake\claude.exe"]):
            return self.manager.change(True, self.session, True)

    def receipt(self, **values):
        request = self.manager._pending["request_id"]
        result = dict(owner_sid=SID, request_id=request, enabled=True, state="enabled", apps=[r"C:\fake\claude.exe"], filter_count=4)
        result.update(values)
        (self.manager.directory / ("request-" + request + ".result.json")).write_text(json.dumps(result), encoding="utf-8")

    def test_constructing_and_status_never_start_any_process(self):
        self.assertFalse(self.manager.status()["enabled"])
        self.run.assert_not_called()
        self.elevate.assert_not_called()
        self.assertFalse(self.manager.directory.exists())

    def test_enable_needs_region_confirmation_before_native_work(self):
        with self.assertRaises(ProtectionError):
            self.manager.change(True, self.session, False)
        self.elevate.assert_not_called()

    def test_enable_refuses_unready_vpn_and_missing_native_install(self):
        with patch.object(self.manager, "_proof", side_effect=ProtectionError("VPN not ready")):
            with self.assertRaises(ProtectionError): self.manager.change(True, self.session, True)
        with patch.object(self.manager, "_proof", return_value=self.proof), patch.object(self.manager, "_native_apps", side_effect=ProtectionError("No native Claude")):
            with self.assertRaises(ProtectionError): self.manager.change(True, self.session, True)
        self.elevate.assert_not_called()

    def test_enable_only_requests_uac_and_never_launches_claude(self):
        state = self.enable()
        self.assertEqual(state["state"], "pending")
        self.assertFalse(state["enabled"])
        args = self.elevate.call_args.args[0]
        self.assertEqual(args[args.index("-Action") + 1], "Enable")
        self.assertIn("-VpnGuid", args)
        self.assertNotIn("-ClaudePath", args)
        self.run.assert_not_called()
        self.assertEqual(json.loads(Path(args[args.index("-AppPathsJsonPath") + 1]).read_text()), [r"C:\fake\claude.exe"])

    def test_success_requires_matching_receipt_and_persists_completion(self):
        self.enable()
        self.receipt()
        self.assertTrue(self.manager.status()["enabled"])
        self.assertIsNone(self.manager._pending)
        self.assertEqual(len(list(self.manager.directory.glob("request-*.result.json"))), 1)

    def test_mismatched_receipt_is_not_success(self):
        self.enable()
        self.receipt(owner_sid="S-1-5-18")
        result = self.manager.status()
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["enabled"])

    def test_native_failure_is_visible_and_does_not_remove_prior_protection(self):
        self.manager._cache.update(enabled=True, state="enabled")
        self.enable()
        self.receipt(state="failed", enabled=True, message="transaction aborted")
        result = self.manager.status()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], "error")
        self.assertEqual(self.elevate.call_count, 1)

    def test_cancelled_uac_preserves_existing_enabled_state(self):
        self.manager._cache.update(enabled=True, state="enabled")
        self.elevate.return_value = 5
        with self.assertRaises(ProtectionError): self.enable()
        self.assertTrue(self.manager.status()["enabled"])
        self.assertIsNone(self.manager._pending)

    def test_disable_is_available_without_vpn_and_only_removes_own_policy(self):
        with patch.object(self.manager, "_proof") as proof, patch.object(self.manager, "_native_apps") as find:
            self.manager.change(False)
        args = self.elevate.call_args.args[0]
        self.assertEqual(args[args.index("-Action") + 1], "Disable")
        self.assertEqual(args[args.index("-OwnerSid") + 1], SID)
        self.assertNotIn("-VpnGuid", args)
        proof.assert_not_called(); find.assert_not_called()

    def test_pending_operation_cannot_be_repeated(self):
        self.enable()
        with self.assertRaises(ProtectionError): self.enable()
        self.assertEqual(self.elevate.call_count, 1)

    def test_pending_timeout_is_uncertain_not_enabled(self):
        self.enable()
        old_request = self.manager._pending["request_id"]
        self.manager._pending["started_at"] = time.time() - 130
        result = self.manager.status()
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["enabled"])
        self.assertIsNone(self.manager._pending)
        self.manager.change(False)
        self.assertNotEqual(self.manager._pending["request_id"], old_request)
        self.receipt(state="disabled", enabled=False)
        self.assertFalse(self.manager.status()["enabled"])

    def test_request_is_persisted_before_elevated_helper_can_start(self):
        def elevation(_):
            request = json.loads((self.manager.directory / "last-request.json").read_text())
            self.assertEqual(request["state"], "pending")
            self.assertEqual(request["request_id"], self.manager._pending["request_id"])
            return 42
        self.elevate.side_effect = elevation
        self.enable()
        self.receipt()
        self.assertTrue(self.manager.status()["enabled"])

    def test_query_failure_does_not_silently_clear_prior_state(self):
        self.manager._cache.update(enabled=True, state="enabled")
        self.run.side_effect = subprocess.TimeoutExpired("powershell.exe", 15)
        self.manager._query()
        result = self.manager.status()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], "error")
        self.elevate.assert_not_called()

    def test_source_copy_survives_packaged_resource_lifetime(self):
        self.enable()
        script = Path(self.elevate.call_args.args[0][self.elevate.call_args.args[0].index("-File") + 1])
        self.assertEqual(script.parent.parent.name, "helpers")
        for name in ("Set-ClaudeProtection.ps1", "ClaudeNetworkGuard.cs"):
            (self.resources / name).unlink()
            self.assertTrue((script.parent / name).is_file())

    def test_restart_requires_system_verification_before_enabled_display(self):
        self.manager.directory.mkdir(parents=True)
        (self.manager.directory / "status.json").write_text(json.dumps({
            "state": "enabled", "enabled": True, "owner_sid": SID,
        }), encoding="utf-8")
        restarted = ClaudeProtection(self.manager.root, self.resources, "powershell.exe", SID,
                                     run=self.run, elevate=self.elevate)
        self.assertEqual(restarted.status()["state"], "checking")
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps({
            "state": "disabled", "enabled": False, "owner_sid": SID,
        }))
        restarted._query()
        self.assertEqual(restarted.status()["state"], "disabled")
        self.assertFalse(restarted.status()["enabled"])
        self.elevate.assert_not_called()

    def test_missing_state_file_still_checks_for_orphaned_system_rules(self):
        with patch("app.claude_protection.threading.Thread") as thread:
            self.manager.refresh()
        thread.return_value.start.assert_called_once()
        self.elevate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
