"""Offline diagnostic controls: no service, vendor discovery or network changes."""
import os
if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget


class FakeVendorHost(QWidget):
    changed = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.is_attached = False

    def detach(self):
        return True


fake_vendor_module = ModuleType("app.vendor_host")
fake_vendor_module.VendorHost = FakeVendorHost
fake_vendor_module.finish_pending_discovery = lambda: None
with patch.dict(sys.modules, {"app.vendor_host": fake_vendor_module}):
    from app import ui


class DiagnosticsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-diagnostics-ui-test-")
        self.addCleanup(self.temp.cleanup)
        vendor_patch = patch.object(ui, "VendorHost", FakeVendorHost)
        vendor_patch.start()
        self.addCleanup(vendor_patch.stop)
        request_patch = patch.object(ui, "request", side_effect=AssertionError("unexpected real service request"))
        self.request = request_patch.start()
        self.addCleanup(request_patch.stop)
        service_patch = patch.object(ui, "ensure_service", side_effect=AssertionError("unexpected real service start"))
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.window = ui.CompanionWindow(preview=True, appearance_path=Path(self.temp.name) / "appearance.json")
        self.window.tray.hide()

    def tearDown(self):
        self.window._owned = False
        self.window._closed = True
        self.window.close()
        self.window.pool.waitForDone(5000)
        self.window.appearance_pool.waitForDone(5000)
        self.app.processEvents()

    def status(self, *, active=False, state="stopped", **diagnostics):
        self.window._apply_status({"state": state, "diagnostics": {"active": active, **diagnostics}})

    def test_export_remains_available_when_companion_is_stopped(self):
        self.status(active=False, session_path="retained-session")
        self.assertTrue(self.window.diagnostics_export_button.isEnabled())
        self.assertIn("已保留记录", self.window.diagnostics_status.text())
        self.assertFalse(self.window.diagnostics_deep_button.isEnabled())

    def test_active_monitor_and_incident_are_visible(self):
        self.status(active=True, incident_at="2026-10-20T12:34:56Z", message="已捕获断线片段", session_path="diagnostic-session")
        self.assertIn("记录中", self.window.diagnostics_status.text())
        self.assertIn("已捕获异常", self.window.diagnostics_status.text())
        self.assertIn("2026-10-20T12:34:56Z", self.window.diagnostics_message.text())
        self.assertIn("已捕获断线片段", self.window.diagnostics_message.text())
        self.assertEqual(self.window.diagnostics_message.toolTip(), "diagnostic-session")

    def test_preview_never_opens_export_dialog_or_requests_elevation(self):
        self.status(active=True)
        with patch.object(ui.QFileDialog, "getSaveFileName") as chooser, patch.object(self.window, "_submit") as submit:
            self.window._export_diagnostics()
            self.window._start_deep_diagnostics()
        chooser.assert_not_called()
        submit.assert_not_called()
        self.assertIn("预览", self.window.diagnostics_feedback.text())

    def test_export_submits_absolute_zip_path_and_cancel_does_nothing(self):
        self.window.preview = False
        target = Path(self.temp.name) / "诊断 bundle"
        with patch.object(ui.QFileDialog, "getSaveFileName", return_value=(str(target), "")), patch.object(self.window, "_submit") as submit:
            self.window._export_diagnostics()
            submit.assert_called_once_with("diagnostics-export", {"path": str(target.absolute()) + ".zip"})
        with patch.object(ui.QFileDialog, "getSaveFileName", return_value=("", "")), patch.object(self.window, "_submit") as submit:
            self.window._export_diagnostics()
            submit.assert_not_called()
        self.assertFalse(target.exists())

    def test_deep_recording_is_explicit_and_states_disable_duplicate_start(self):
        self.window.preview = False
        self.status(active=True)
        self.assertIn("管理员", self.window.diagnostics_deep_button.toolTip())
        with patch.object(self.window, "_submit") as submit:
            self.window.diagnostics_deep_button.click()
            submit.assert_called_once_with("diagnostics-deep-start")
        for deep_state in ("active", "starting", "Running"):
            with self.subTest(deep_state=deep_state):
                self.status(active=True, deep_state=deep_state)
                self.assertFalse(self.window.diagnostics_deep_button.isEnabled())
        self.status(active=True, deep_state="error")
        self.assertTrue(self.window.diagnostics_deep_button.isEnabled())

    def test_diagnostic_task_is_queued_and_does_not_block_stop(self):
        self.window.preview = False
        self.status(active=True, state="running")
        with patch.object(self.window.pool, "start") as queue:
            self.window._submit("diagnostics-export", {"path": str(Path(self.temp.name) / "out.zip")})
            self.assertEqual(queue.call_count, 1)
            self.assertIn("diagnostics-export", self.window._tasks)
            self.assertEqual(self.window._busy_action, "")
            self.assertFalse(self.window.diagnostics_export_button.isEnabled())
            self.assertTrue(self.window.primary.isEnabled())
            self.window._submit("stop")
            self.assertEqual(queue.call_count, 2)
            self.assertEqual(self.window._busy_action, "stop")
        self.request.assert_not_called()

    def test_diagnostic_task_can_queue_during_start_or_stop(self):
        self.window.preview = False
        self.window._busy_action = "stop"
        with patch.object(self.window.pool, "start") as queue:
            self.window._submit("diagnostics-export", {"path": str(Path(self.temp.name) / "out.zip")})
            self.assertEqual(queue.call_count, 1)
            self.assertEqual(self.window._busy_action, "stop")

    def test_export_success_feedback_survives_next_status_poll(self):
        self.window._task_succeeded("diagnostics-export", {"path": "C:/saved/logs.zip"})
        self.status(active=False)
        self.assertIn("C:/saved/logs.zip", self.window.diagnostics_feedback.text())
        self.assertEqual(self.window._status["state"], "stopped")

    def test_export_reports_omitted_records(self):
        self.window._task_succeeded("diagnostics-export", {"path": "C:/saved/logs.zip", "omissions": ["optional trace unavailable"]})
        self.assertIn("部分记录未包含", self.window.diagnostics_feedback.text())

    def test_deep_response_updates_monitor_without_replacing_engine_state(self):
        self.status(active=True, state="running")
        self.window._task_succeeded("diagnostics-deep-start", {"diagnostics": {"active": True, "deep_state": "active"}, "message": "深度记录已启动"})
        self.window._task_finished("diagnostics-deep-start")
        self.assertEqual(self.window._status["state"], "running")
        self.assertEqual(self.window.diagnostics_feedback.text(), "深度记录已启动")
        self.assertFalse(self.window.diagnostics_deep_button.isEnabled())

    def test_diagnostic_error_does_not_claim_service_or_vpn_failed(self):
        self.status(active=True, state="running")
        before_errors = self.window._errors
        before_chip = self.window.service_chip.text()
        self.window._task_failed("diagnostics-deep-start", "用户取消管理员授权")
        self.assertEqual(self.window._errors, before_errors)
        self.assertEqual(self.window.service_chip.text(), before_chip)
        self.assertEqual(self.window._status["state"], "running")
        self.assertIn("取消管理员授权", self.window.diagnostics_feedback.text())

    def test_service_task_uses_local_rpc_and_longer_diagnostics_timeout(self):
        payload = {"path": str(Path(self.temp.name) / "out.zip")}
        self.request.side_effect = None
        self.request.return_value = {"path": payload["path"]}
        task = ui.ServiceTask("diagnostics-export", payload)
        successes, completed = [], []
        task.signals.succeeded.connect(lambda action, result: successes.append((action, result)))
        task.signals.finished.connect(completed.append)
        task.run()
        self.request.assert_called_once_with("diagnostics-export", payload, timeout=120)
        self.assertEqual(successes, [("diagnostics-export", {"path": payload["path"]})])
        self.assertEqual(completed, ["diagnostics-export"])


if __name__ == "__main__":
    unittest.main()
