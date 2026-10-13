"""Whitelist RPC/UI integration with temporary data and no real VPN session."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import engine
from tests import test_vendor_ui as vendor_test_module


class WhitelistRpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "local"
        self.coordinator = engine.Coordinator(root=self.root)
        self.coordinator.whitelist_store._secure = lambda path: path.mkdir(parents=True, exist_ok=True)
        self.coordinator._whitelist_session = Mock(return_value=None)

    def test_read_only_list_does_not_create_file_or_start_session(self):
        with patch("app.engine.hidden_spawn") as spawn:
            result = self.coordinator.dispatch("whitelist-list")
        self.assertEqual(result["rules"], [])
        self.assertFalse(result["active"])
        self.assertFalse(self.root.exists())
        spawn.assert_not_called()

    def test_batch_add_remove_rpc_persists_normalized_hosts_only(self):
        result = self.coordinator.dispatch("whitelist-add", {
            "text": "https://user:secret@example.com/private?token=hidden\n192.0.2.1:8000", "mode": "vpn", "subdomains": True})
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(result["rules"]), 2)
        self.assertEqual(result["rules"][0], {"host": "192.0.2.1", "mode": "vpn", "subdomains": False})
        result = self.coordinator.dispatch("whitelist-remove", {"host": "http://example.com/path", "subdomains": True})
        self.assertEqual(result["revision"], 2)
        self.assertEqual(len(result["rules"]), 1)
        self.assertNotIn("secret", (self.root / "whitelist.json").read_text())

    def test_invalid_batch_reports_service_error_without_partial_save(self):
        with self.assertRaises(engine.ServiceError):
            self.coordinator.dispatch("whitelist-add", {"text": "valid.example\nnot_a_host", "mode": "direct"})
        self.assertFalse(self.root.exists())

    def test_running_session_reports_accepted_revision_and_recent_destinations(self):
        session = Path(self.temp.name) / "session"
        session.mkdir()
        engine.atomic_json(session / "split-status.json", {"WhitelistRevision": 4})
        (session / "monitor.log").write_text("09:00:00 DIRECT example.cn IP=1.0.1.1\n09:00:01 VPN api.example.test IP=192.0.2.1 WHITELIST=vpn\n", encoding="utf-8")
        self.coordinator._whitelist_session.return_value = session
        result = self.coordinator.dispatch("whitelist-list")
        self.assertTrue(result["active"])
        self.assertEqual(result["applied_revision"], 4)
        self.assertEqual(result["recent"][0]["host"], "api.example.test")
        self.assertTrue(result["recent"][0]["whitelist"])

    def test_recent_failed_sites_can_be_added_and_private_url_parts_are_removed(self):
        rows = engine.recent_destinations([
            "DIRECT https://user:secret@example.cn/private?token=hidden IP=1.0.1.1",
            "ERROR slow.example.test mode=VPN reason=dns-failed",
            "VPN example.cn IP=192.0.2.1 WHITELIST=vpn",
        ])
        self.assertEqual([row["host"] for row in rows], ["example.cn", "slow.example.test"])
        self.assertTrue(rows[1]["failed"])
        self.assertNotIn("secret", str(rows))
        self.assertNotIn("token", str(rows))


class WhitelistCompanionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vendor_test_module.VendorUiTests.setUpClass.__func__(cls)

    setUp = vendor_test_module.VendorUiTests.setUp
    tearDown = vendor_test_module.VendorUiTests.tearDown
    activate = vendor_test_module.VendorUiTests.activate

    def test_editor_preview_does_not_read_clipboard_or_request_service(self):
        with patch("app.ui.QApplication.clipboard") as clipboard:
            self.window._open_whitelist()
        clipboard.assert_not_called()
        self.window._submit.assert_not_called()
        self.assertFalse(self.window._whitelist_dialog.isModal())

    def test_editor_hides_native_slot_and_restores_on_close(self):
        self.window._navigate(4)
        self.assertTrue(self.window.vendor_host.isVisible())
        self.window._open_whitelist()
        self.assertFalse(self.window.vendor_host.isVisible())
        self.window._whitelist_dialog.close()
        self.assertIsNone(self.window._whitelist_dialog)
        self.assertTrue(self.window.vendor_host.isVisible())

    def test_quick_clipboard_action_is_explicit_and_does_not_start_vpn(self):
        self.activate(running=False)
        clipboard = Mock()
        clipboard.text.return_value = "https://example.com/path"
        with patch("app.ui.QApplication.clipboard", return_value=clipboard):
            self.window._clipboard_whitelist("vpn")
        self.window._submit.assert_called_once_with("whitelist-add", {"text": "https://example.com/path", "mode": "vpn", "subdomains": True})

    def test_editor_closes_with_parent_and_late_list_cannot_replace_mutation(self):
        self.window._open_whitelist()
        dialog = self.window._whitelist_dialog
        dialog.apply_document = Mock()
        self.window._whitelist_mutation_epoch = 1
        self.window._whitelist_list_epoch = 0
        self.window._task_succeeded("whitelist-list", {"revision": 0, "rules": []})
        dialog.apply_document.assert_not_called()
        self.window.close()
        self.assertIsNone(self.window._whitelist_dialog)
        self.assertFalse(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
