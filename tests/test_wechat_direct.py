"""Mocked setup lifecycle. No PowerShell, UAC, service or network is used."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.wechat_direct import WeChatDirect, WeChatDirectError


SID = "S-1-5-21-123-456-789-1001"


class WeChatDirectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-wechat-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources"
        self.resources.mkdir()
        (self.resources / "Set-WeChatDirect.ps1").write_text("mock; never executed", encoding="utf-8")
        (self.resources / "WeChatDirectRelay.cs").write_text("mock relay; never executed", encoding="utf-8")
        self.run = Mock()
        self.elevate = Mock(return_value=42)
        self.manager = self.new_manager()

    def new_manager(self):
        return WeChatDirect(self.root / "data", self.resources, "powershell.exe", SID,
                            run=self.run, elevate=self.elevate)

    def write(self, name, value):
        self.manager.directory.mkdir(parents=True, exist_ok=True)
        (self.manager.directory / name).write_text(json.dumps(value), encoding="utf-8")

    def receipt(self, requested=True, **extra):
        request = self.manager._pending["request_id"]
        value = dict(owner_sid=SID, request_id=request, desired_enabled=requested,
                     enabled=requested, state="running" if requested else "disabled",
                     interface_name="WLAN", message="verified")
        value.update(extra)
        self.write("request-" + request + ".result.json", value)

    def result(self, state="running", enabled=True, **extra):
        value = dict(owner_sid=SID, state=state, enabled=enabled, message="verified", interface_name="WLAN")
        value.update(extra)
        self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(value))

    def test_default_on_is_not_proof_or_automatic_install(self):
        state = self.manager.status()
        self.assertTrue(state["desired_enabled"])
        self.assertFalse(state["enabled"])
        self.assertEqual(state["state"], "needs-setup")
        self.run.assert_not_called()
        self.elevate.assert_not_called()
        self.assertFalse(self.manager.directory.exists())

    def test_pending_is_persisted_before_explicit_elevation(self):
        def elevate(command):
            value = json.loads((self.manager.directory / "last-request.json").read_text())
            self.assertEqual(value, self.manager._pending)
            self.assertEqual(value["state"], "pending")
            self.assertEqual(value["owner_sid"], SID)
            self.assertEqual(command[command.index("-RequestId") + 1], value["request_id"])
            self.assertEqual(command[command.index("-Action") + 1], "Enable")
            return 42
        self.elevate.side_effect = elevate
        state = self.manager.change(True)
        self.assertEqual(state["state"], "pending")
        self.assertFalse(state["enabled"])
        self.run.assert_not_called()

    def test_matching_receipt_is_required_before_enabled(self):
        self.manager.change(True)
        self.receipt()
        self.assertTrue(self.manager.status()["enabled"])
        self.assertIsNone(self.manager._pending)
        self.assertTrue(json.loads((self.manager.directory / "preferences.json").read_text())["desired_enabled"])

    def test_disable_pref_is_saved_only_after_verified_receipt(self):
        self.manager.change(False)
        self.assertTrue(self.manager.status()["desired_enabled"])
        self.assertFalse((self.manager.directory / "preferences.json").exists())
        self.receipt(False)
        state = self.manager.status()
        self.assertFalse(state["desired_enabled"])
        self.assertEqual(state["state"], "disabled")
        self.assertFalse(self.new_manager().status()["desired_enabled"])

    def test_cancel_keeps_old_pref_and_does_not_leave_pending(self):
        self.elevate.return_value = 5
        with self.assertRaises(WeChatDirectError):
            self.manager.change(False)
        self.assertTrue(self.manager.status()["desired_enabled"])
        self.assertEqual(self.manager.status()["state"], "needs-setup")
        self.assertIsNone(self.manager._pending)
        self.assertEqual(json.loads((self.manager.directory / "last-request.json").read_text())["state"], "cancelled")

    def test_elevation_exception_does_not_lock_out_retry(self):
        self.elevate.side_effect = OSError("blocked")
        with self.assertRaises(OSError):
            self.manager.change(True)
        self.assertIsNone(self.manager._pending)
        self.elevate.side_effect = None
        self.manager.change(True)

    def test_successful_receipt_can_arrive_during_elevation(self):
        def elevate(_):
            self.receipt()
            return 42
        self.elevate.side_effect = elevate
        self.assertTrue(self.manager.change(True)["enabled"])

    def test_pending_blocks_concurrent_requests(self):
        self.manager.change(True)
        with self.assertRaises(WeChatDirectError):
            self.manager.change(False)
        self.assertEqual(self.elevate.call_count, 1)

    def test_progress_requires_same_request_and_does_not_claim_success(self):
        self.manager.change(True)
        self.write("progress.json", dict(state="pending", request_id="wrong", message="wrong"))
        self.assertNotEqual(self.manager.status()["message"], "wrong")
        self.write("progress.json", dict(state="pending", request_id=self.manager._pending["request_id"], message="downloading"))
        self.assertEqual(self.manager.status()["message"], "downloading")
        self.assertFalse(self.manager.status()["enabled"])

    def test_ten_minute_timeout_allows_new_request_and_supersedes_old(self):
        self.manager.change(True)
        old = self.manager._pending["request_id"]
        self.manager._pending["started_at"] = time.time() - 601
        state = self.manager.status()
        self.assertEqual(state["state"], "error")
        self.assertIsNone(self.manager._pending)
        self.manager.change(False)
        self.assertNotEqual(self.manager._pending["request_id"], old)
        self.assertEqual(json.loads((self.manager.directory / "last-request.json").read_text())["request_id"], self.manager._pending["request_id"])
        self.write("request-" + old + ".result.json", dict(owner_sid=SID, request_id=old, state="running", enabled=True))
        self.assertEqual(self.manager.status()["state"], "pending")

    def test_mismatched_identity_or_receipt_cannot_enable(self):
        for extra in ({"owner_sid": "S-1-5-18"}, {"request_id": "wrong"}, {"desired_enabled": False}):
            with self.subTest(extra=extra):
                self.manager.change(True)
                self.receipt(**extra)
                result = self.manager.status()
                self.assertEqual(result["state"], "error")
                self.assertFalse(result["enabled"])
                self.assertTrue(result["desired_enabled"])
                self.assertIsNone(self.manager._pending)

    def test_error_receipt_preserves_preference(self):
        self.manager.change(False)
        self.receipt(False, state="error", message="install failed")
        state = self.manager.status()
        self.assertEqual(state["state"], "error")
        self.assertTrue(state["desired_enabled"])
        self.assertFalse((self.manager.directory / "preferences.json").exists())

    def test_failed_disable_can_report_verified_old_service_running(self):
        self.manager.change(False)
        self.receipt(False, state="error", enabled=True, message="old service remains verified")
        state = self.manager.status()
        self.assertEqual(state["state"], "error")
        self.assertTrue(state["enabled"])
        self.assertTrue(state["desired_enabled"])

    def test_saved_status_does_not_prove_service_running(self):
        self.write("status.json", dict(owner_sid=SID, enabled=True, desired_enabled=False, state="running"))
        restarted = self.new_manager()
        self.assertFalse(restarted.status()["enabled"])
        self.assertTrue(restarted.status()["desired_enabled"])
        self.assertEqual(restarted.status()["state"], "checking")

    def test_active_request_survives_restart(self):
        self.manager.change(True)
        restarted = self.new_manager()
        self.assertEqual(restarted.status()["state"], "pending")
        self.assertEqual(restarted._pending["request_id"], self.manager._pending["request_id"])
        self.receipt()
        self.assertTrue(restarted.status()["enabled"])

    def test_expired_or_foreign_pending_does_not_block_restart(self):
        self.manager.change(True)
        request = dict(self.manager._pending, started_at=time.time() - 601)
        self.write("last-request.json", request)
        self.assertIsNone(self.new_manager()._pending)
        self.write("last-request.json", dict(request, started_at=time.time(), owner_sid="S-1-5-18"))
        self.assertIsNone(self.new_manager()._pending)

    def test_refresh_is_read_only_throttled_background_status(self):
        with patch("app.wechat_direct.threading.Thread") as thread:
            self.manager.refresh()
            self.manager.refresh()
        self.assertEqual(thread.call_count, 1)
        thread.return_value.start.assert_called_once()
        self.elevate.assert_not_called()
        self.run.assert_not_called()

    def test_actual_status_checks_service_without_changing_saved_preference(self):
        self.result()
        self.manager._query()
        self.assertTrue(self.manager.status()["enabled"])
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index("-Action") + 1], "Status")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 15)
        self.elevate.assert_not_called()
        self.assertFalse((self.manager.directory / "preferences.json").exists())

    def test_not_installed_means_setup_needed_for_default_on(self):
        self.result("not-installed", False)
        self.manager._query()
        self.assertEqual(self.manager.status()["state"], "needs-setup")
        self.assertTrue(self.manager.status()["desired_enabled"])

    def test_query_failure_cannot_claim_running(self):
        self.manager._cache.update(enabled=True, state="running")
        self.run.side_effect = subprocess.TimeoutExpired("powershell.exe", 15)
        self.manager._query()
        self.assertFalse(self.manager.status()["enabled"])
        self.assertEqual(self.manager.status()["state"], "error")

    def test_stale_query_cannot_overwrite_new_operation(self):
        def run(*args, **kwargs):
            self.manager.change(False)
            return subprocess.CompletedProcess([], 0, json.dumps(dict(owner_sid=SID, state="running", enabled=True)))
        self.run.side_effect = run
        self.manager._query()
        self.assertEqual(self.manager.status()["state"], "pending")
        self.assertFalse(self.manager.status()["enabled"])

    def test_invalid_or_inconsistent_status_is_an_error(self):
        for value in ([], {"owner_sid": SID, "state": "running", "enabled": False},
                      {"owner_sid": "wrong", "state": "running", "enabled": True}):
            self.run.return_value = subprocess.CompletedProcess([], 0, json.dumps(value))
            self.manager._query()
            self.assertEqual(self.manager.status()["state"], "error")

    def test_helper_copy_is_stable_and_does_not_launch_apps(self):
        self.manager.change(True)
        command = self.elevate.call_args.args[0]
        script = Path(command[command.index("-File") + 1])
        self.assertEqual(script.parent.parent.name, "helpers")
        self.assertEqual(script.read_bytes(), (self.resources / script.name).read_bytes())
        self.assertEqual((script.parent / "WeChatDirectRelay.cs").read_bytes(), (self.resources / "WeChatDirectRelay.cs").read_bytes())
        (self.resources / script.name).unlink()
        self.assertTrue(script.is_file())
        self.assertNotIn("wechat.exe", " ".join(command).lower())
        self.assertNotIn("claude", " ".join(command).lower())

    def test_boolean_type_and_missing_component_are_checked(self):
        with self.assertRaises(WeChatDirectError):
            self.manager.change("yes")
        (self.resources / "Set-WeChatDirect.ps1").unlink()
        with self.assertRaises(WeChatDirectError):
            self.manager.change(True)
        self.elevate.assert_not_called()
        self.assertIsNone(self.manager._pending)


if __name__ == "__main__":
    unittest.main()
