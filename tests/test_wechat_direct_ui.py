"""Offline per-app routing UI checks; no installer, VPN or real service is used."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget


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


class WechatDirectUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-wechat-ui-test-")
        self.addCleanup(self.temp.cleanup)
        for name in ("VendorHost", "request", "ensure_service"):
            patcher = patch.object(ui, name, FakeVendorHost) if name == "VendorHost" else patch.object(ui, name, side_effect=AssertionError("unexpected real service access"))
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "request":
                self.request = value
        self.window = ui.CompanionWindow(preview=True, appearance_path=Path(self.temp.name) / "appearance.json")
        self.window.tray.hide()

    def tearDown(self):
        self.window._owned = False
        self.window._closed = True
        self.window.close()
        self.window.pool.waitForDone(5000)
        self.window.appearance_pool.waitForDone(5000)
        self.app.processEvents()

    def status(self, desired=True, enabled=False, state="not-installed", **extra):
        self.window.preview = False
        self.window._apply_status({"state": "running", "connected": False,
            "wechat_direct": {"desired_enabled": desired, "enabled": enabled, "state": state, **extra}})

    def test_default_on_is_a_preference_without_claiming_or_activating_routing(self):
        self.assertTrue(self.window.wechat_direct_switch.isChecked())
        self.assertEqual(self.window.wechat_direct_switch.text(), "微信直连")
        self.assertIn("需要安装", self.window.wechat_direct_status.text())
        self.assertIn("未生效", self.window.wechat_direct_status.text())
        self.assertFalse(self.window.wechat_direct_install.isHidden())
        self.request.assert_not_called()

    def test_actual_routing_requires_both_running_state_and_enabled(self):
        for state, enabled in (("not-installed", False), ("needs-setup", False), ("checking", True), ("pending", True), ("disabled", False), ("error", True), ("running", False)):
            with self.subTest(state=state, enabled=enabled):
                self.status(state=state, enabled=enabled)
                self.assertTrue(self.window.wechat_direct_switch.isChecked())
                self.assertNotIn("已生效", self.window.wechat_direct_status.text())
                if state == "needs-setup":
                    self.assertIn("需要安装", self.window.wechat_direct_status.text())
                    self.assertFalse(self.window.wechat_direct_install.isHidden())
        self.status(state="running", enabled=True, interface_name="Test Ethernet")
        self.assertIn("已生效", self.window.wechat_direct_status.text())
        self.assertIn("Test Ethernet", self.window.wechat_direct_status.toolTip())

    def test_preview_never_requests_installer_or_network_changes(self):
        with patch.object(ui.QMessageBox, "question") as prompt, patch.object(self.window, "_submit") as submit:
            self.window._request_wechat_direct(True)
            self.window._toggle_wechat_direct()
        prompt.assert_not_called()
        submit.assert_not_called()
        self.assertIn("预览", self.window.wechat_direct_message.text())

    def test_cancelling_first_install_keeps_desired_on_but_not_effective(self):
        self.status()
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel), patch.object(self.window, "_submit") as submit:
            self.window.wechat_direct_install.click()
        submit.assert_not_called()
        self.assertTrue(self.window.wechat_direct_switch.isChecked())
        self.assertIn("未生效", self.window.wechat_direct_status.text())

    def test_install_button_works_with_default_on_and_explains_component_scope(self):
        self.status()
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as prompt, patch.object(self.window.pool, "start") as queue:
            self.window.wechat_direct_install.click()
            self.window.wechat_direct_install.click()
            self.assertEqual(queue.call_count, 1)
        self.assertIn("wechat-direct-enable", self.window._tasks)
        self.assertEqual(self.window._busy_action, "")
        for phrase in ("管理员授权", "短暂断网", "只为微信", "原 VPN 保持不变", "不会启动或退出微信", "不会启动 Claude"):
            self.assertIn(phrase, prompt.call_args.args[2])
        self.assertFalse(self.window.wechat_direct_switch.isEnabled())
        self.assertTrue(self.window.primary.isEnabled())
        self.assertNotIn("已生效", self.window.wechat_direct_status.text())

    def test_checkbox_restores_service_desired_state_until_enable_reply(self):
        self.status(desired=False, state="disabled")
        with patch.object(self.window.pool, "start"):
            self.window.wechat_direct_switch.click()
        self.assertFalse(self.window.wechat_direct_switch.isChecked())
        self.assertIn("wechat-direct-enable", self.window._tasks)
        self.window._task_succeeded("wechat-direct-enable", {"wechat_direct": {"desired_enabled": True, "enabled": False, "state": "pending"}})
        self.window._task_finished("wechat-direct-enable")
        self.assertTrue(self.window.wechat_direct_switch.isChecked())
        self.assertFalse(self.window.wechat_direct_switch.isEnabled())
        self.assertNotIn("已生效", self.window.wechat_direct_status.text())

    def test_disable_is_independent_and_only_reply_changes_preference(self):
        self.status(enabled=True, state="running")
        with patch.object(self.window.pool, "start") as queue:
            self.window.wechat_direct_switch.click()
            self.assertTrue(self.window.wechat_direct_switch.isChecked())
            self.assertIn("wechat-direct-disable", self.window._tasks)
            self.window._submit("stop")
            self.assertEqual(queue.call_count, 2)
        self.window._task_succeeded("wechat-direct-disable", {"wechat_direct": {"desired_enabled": False, "enabled": False, "state": "disabled"}, "message": "微信已恢复跟随系统路线"})
        self.window._task_finished("wechat-direct-disable")
        self.assertFalse(self.window.wechat_direct_switch.isChecked())
        self.assertIn("已关闭", self.window.wechat_direct_status.text())
        self.assertIn("跟随系统", self.window.wechat_direct_message.text())

    def test_failure_keeps_known_preference_and_does_not_mark_vpn_failed(self):
        self.status(desired=False, state="disabled")
        errors, chip = self.window._errors, self.window.service_chip.text()
        with patch.object(self.window.pool, "start"):
            self.window._request_wechat_direct(True)
        self.window._task_failed("wechat-direct-enable", "组件暂不可用")
        self.window._task_finished("wechat-direct-enable")
        self.assertFalse(self.window.wechat_direct_switch.isChecked())
        self.assertTrue(self.window.wechat_direct_switch.isEnabled())
        self.assertIn("组件暂不可用", self.window.wechat_direct_message.text())
        self.assertEqual(self.window._errors, errors)
        self.assertEqual(self.window.service_chip.text(), chip)

    def test_service_task_only_uses_local_requested_action(self):
        self.request.side_effect = None
        self.request.return_value = {"wechat_direct": {"enabled": False, "desired_enabled": True, "state": "checking"}}
        ui.ServiceTask("wechat-direct-enable").run()
        self.request.assert_called_once_with("wechat-direct-enable", None, timeout=120)

    def test_both_cards_and_primary_fit_minimum_window_with_long_errors(self):
        message = "状态暂未确认，详细诊断记录保留。\n" * 160
        self.status(state="error", message=message)
        self.window._status["claude_protection"] = {"enabled": True, "state": "enabled", "message": message}
        self.window._render_controls()
        self.window._task_failed("claude-protection-enable", message)
        self.window.resize(920, 600)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.size().width(), 920)
        self.assertEqual(self.window.size().height(), 600)
        self.assertLessEqual(len(self.window.wechat_direct_message.text()), 60)
        self.assertIn(message, self.window.wechat_direct_message.toolTip())
        for widget in (self.window.wechat_direct_switch, self.window.claude_protection_switch, self.window.primary):
            self.assertTrue(widget.isVisible())
            self.assertTrue(widget.isEnabled())
            point = widget.mapTo(self.window, widget.rect().topLeft())
            self.assertTrue(self.window.rect().contains(point))
            self.assertTrue(self.window.rect().contains(point + widget.rect().bottomRight()))


if __name__ == "__main__":
    unittest.main()
