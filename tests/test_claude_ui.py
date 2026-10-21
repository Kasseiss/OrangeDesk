"""Offline exit-protection UI tests; no Claude, service, UAC or VPN is started."""
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


class ClaudeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orange-claude-ui-test-")
        self.addCleanup(self.temp.cleanup)
        for target, replacement in (("VendorHost", FakeVendorHost), ("request", None), ("ensure_service", None)):
            patcher = patch.object(ui, target, replacement) if replacement is not None else patch.object(ui, target, side_effect=AssertionError("unexpected real service access"))
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "request":
                self.request = mocked
        self.window = ui.CompanionWindow(preview=True, appearance_path=Path(self.temp.name) / "appearance.json")
        self.window.tray.hide()

    def tearDown(self):
        self.window._owned = False
        self.window._closed = True
        self.window.close()
        self.window.pool.waitForDone(5000)
        self.window.appearance_pool.waitForDone(5000)
        self.app.processEvents()

    def protection(self, enabled=False, state=None, engine_state="running", **extra):
        self.window.preview = False
        self.window._apply_status({"state": engine_state, "connected": False,
            "claude_protection": {"enabled": enabled, "state": state or ("enabled" if enabled else "disabled"), **extra}})

    def test_status_drives_switch_without_starting_anything(self):
        self.protection(enabled=True, message="保护有效", apps=["claude.exe"], interface_name="Test VPN")
        self.assertTrue(self.window.claude_protection_switch.isChecked())
        self.assertEqual(self.window.claude_protection_status.text(), "已开启")
        self.assertIn("保护有效", self.window.claude_protection_message.text())
        self.assertIn("原项目终端手动运行 claude", self.window.claude_protection_message.text())
        self.assertIn("Test VPN", self.window.claude_protection_message.text())
        self.assertIn("1 个程序", self.window.claude_protection_message.text())
        self.request.assert_not_called()
        self.protection(enabled=False, engine_state="stopped")
        self.assertFalse(self.window.claude_protection_switch.isChecked())
        self.assertTrue(self.window.claude_protection_switch.isEnabled())

    def test_preview_never_prompts_or_requests_authorization(self):
        with patch.object(ui.QMessageBox, "question") as prompt, patch.object(self.window, "_submit") as submit:
            self.window._toggle_claude_protection()
        prompt.assert_not_called()
        submit.assert_not_called()
        self.assertIn("预览", self.window.claude_protection_feedback.text())

    def test_enable_confirmation_explains_singapore_uac_and_manual_workflow(self):
        self.protection()
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as prompt, patch.object(self.window, "_submit") as submit:
            self.window.claude_protection_switch.click()
        submit.assert_called_once_with("claude-protection-enable", {"singapore_confirmed": True})
        text = prompt.call_args.args[2]
        for required in ("新加坡", "不会自动验证节点国家", "管理员授权", "不会启动 Claude", "读取凭据", "切换节点", "原项目终端",
                         "原生 claude.exe", "不覆盖 Node 脚本版", "其它本地代理", "无法保证代理程序的出口",
                         "先退出所有已有 Claude 会话", "已建立的连接不一定能追溯拦截", "关闭伴航不会解除",
                         "不会更改命令路径或项目目录", "不能保证账号风控结果"):
            self.assertIn(required, text)
        self.assertEqual(prompt.call_args.args[4], QMessageBox.StandardButton.Cancel)
        self.assertFalse(self.window.claude_protection_switch.isChecked(), "no optimistic protection claim")

    def test_cancelling_enable_restores_actual_unchecked_state(self):
        self.protection()
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel), patch.object(self.window, "_submit") as submit:
            self.window.claude_protection_switch.click()
        submit.assert_not_called()
        self.assertFalse(self.window.claude_protection_switch.isChecked())

    def test_disable_only_requests_removal_of_companion_rules(self):
        self.protection(enabled=True)
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as prompt, patch.object(self.window, "_submit") as submit:
            self.window.claude_protection_switch.click()
        submit.assert_called_once_with("claude-protection-disable", None)
        self.assertIn("仅移除伴航自建", prompt.call_args.args[2])
        self.assertIn("管理员授权", prompt.call_args.args[2])
        self.assertIn("不会随伴航关闭而自动移除", prompt.call_args.args[2])
        self.assertTrue(self.window.claude_protection_switch.isChecked())

    def test_changed_protection_state_during_confirmation_does_not_submit(self):
        self.protection()
        def changed(*_args):
            self.protection(enabled=True)
            return QMessageBox.StandardButton.Yes
        with patch.object(ui.QMessageBox, "question", side_effect=changed), patch.object(self.window, "_submit") as submit:
            self.window._toggle_claude_protection()
        submit.assert_not_called()
        self.assertIn("状态已变化", self.window.claude_protection_feedback.text())

    def test_pending_rpc_blocks_duplicates_without_blocking_companion_stop(self):
        self.protection()
        with patch.object(self.window.pool, "start") as queue, patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as prompt:
            self.window.claude_protection_switch.click()
            self.window.claude_protection_switch.click()
            self.window._toggle_claude_protection()
            self.assertEqual(queue.call_count, 1)
            self.assertEqual(prompt.call_count, 1)
            self.assertEqual(self.window._busy_action, "")
            self.assertFalse(self.window.claude_protection_switch.isEnabled())
            self.assertFalse(self.window.claude_protection_switch.isChecked())
            self.assertTrue(self.window.primary.isEnabled())
            self.window._submit("stop")
            self.assertEqual(queue.call_count, 2)
        self.request.assert_not_called()

    def test_pending_and_error_states_are_not_presented_as_enabled(self):
        self.protection(state="pending")
        self.assertFalse(self.window.claude_protection_switch.isEnabled())
        self.assertIn("处理中", self.window.claude_protection_status.text())
        self.protection(state="error", message="系统保护不可用")
        self.assertEqual(self.window.claude_protection_status.text(), "需要检查")
        self.assertFalse(self.window.claude_protection_switch.isChecked())
        self.protection(enabled=False, state="enabled")
        self.assertEqual(self.window.claude_protection_status.text(), "状态待确认")

    def test_checking_hides_cached_enabled_but_error_can_still_disable(self):
        self.protection(enabled=True, state="checking")
        self.assertFalse(self.window.claude_protection_switch.isChecked())
        self.assertFalse(self.window.claude_protection_switch.isEnabled())
        self.assertEqual(self.window.claude_protection_status.text(), "正在核验")
        with patch.object(ui.QMessageBox, "question") as prompt:
            self.window._toggle_claude_protection()
            prompt.assert_not_called()
        self.protection(enabled=True, state="error", message="无法确认现有规则")
        self.assertTrue(self.window.claude_protection_switch.isChecked())
        self.assertTrue(self.window.claude_protection_switch.isEnabled())
        self.assertEqual(self.window.claude_protection_status.text(), "需要检查")
        with patch.object(ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), patch.object(self.window, "_submit") as submit:
            self.window.claude_protection_switch.click()
            submit.assert_called_once_with("claude-protection-disable", None)

    def test_rpc_result_and_status_poll_update_confirmed_state(self):
        self.protection()
        with patch.object(self.window.pool, "start"):
            self.window._submit("claude-protection-enable", {"singapore_confirmed": True})
        self.window._task_succeeded("claude-protection-enable", {"claude_protection": {"enabled": True, "state": "enabled"}, "message": "规则已安装"})
        self.window._task_finished("claude-protection-enable")
        self.assertTrue(self.window.claude_protection_switch.isChecked())
        self.assertTrue(self.window.claude_protection_switch.isEnabled())
        self.assertEqual(self.window._status["state"], "running")
        self.assertEqual(self.window.claude_protection_feedback.text(), "规则已安装")
        self.protection(enabled=False, engine_state="stopped")
        self.assertFalse(self.window.claude_protection_switch.isChecked())

    def test_failed_enable_does_not_claim_enabled_or_mark_service_failed(self):
        self.protection()
        errors, chip = self.window._errors, self.window.service_chip.text()
        with patch.object(self.window.pool, "start"):
            self.window._submit("claude-protection-enable", {"singapore_confirmed": True})
        self.window._task_failed("claude-protection-enable", "用户取消管理员授权")
        self.window._task_finished("claude-protection-enable")
        self.assertFalse(self.window.claude_protection_switch.isChecked())
        self.assertTrue(self.window.claude_protection_switch.isEnabled())
        self.assertIn("取消管理员授权", self.window.claude_protection_feedback.text())
        self.assertEqual(self.window._errors, errors)
        self.assertEqual(self.window.service_chip.text(), chip)

    def test_service_task_only_sends_protection_rpc(self):
        payload = {"singapore_confirmed": True}
        self.request.side_effect = None
        self.request.return_value = {"enabled": False, "state": "error"}
        task = ui.ServiceTask("claude-protection-enable", payload)
        task.run()
        self.request.assert_called_once_with("claude-protection-enable", payload, timeout=120)

    def test_protection_switch_fits_minimum_window_and_launcher_is_removed(self):
        self.protection()
        self.window.resize(920, 600)
        self.window.show()
        self.app.processEvents()
        switch = self.window.claude_protection_switch
        self.assertTrue(switch.isVisible())
        corner = switch.mapTo(self.window.pages, switch.rect().topLeft())
        self.assertTrue(self.window.pages.rect().contains(corner))
        self.assertTrue(self.window.pages.rect().contains(corner + switch.rect().bottomRight()))
        self.assertFalse(hasattr(self.window, "claude_button"))
        self.assertFalse(hasattr(self.window, "_start_claude"))

    def test_long_production_messages_keep_switch_and_stop_visible(self):
        message = "出口保护规则已生效，终端工作目录保持原样。\n" * 110
        apps = [f"C:/test/application-{index}.exe" for index in range(20)]
        self.protection(enabled=True, message=message, apps=apps, interface_name="快橙测试接口")
        self.window.resize(920, 600)
        self.window.show()
        self.app.processEvents()
        for label in (self.window.claude_protection_message,):
            self.assertLessEqual(len(label.text()), 120)
            self.assertNotIn("\n", label.text())
            self.assertIn(message, label.toolTip())
            self.assertLessEqual(label.height(), 44)
        self.assertEqual(self.window._status["claude_protection"]["message"], message)
        self.assertTrue(self.window.claude_protection_switch.isChecked())
        self.window._task_failed("claude-protection-enable", message)
        self.app.processEvents()
        feedback = self.window.claude_protection_feedback
        self.assertLessEqual(len(feedback.text()), 120)
        self.assertIn(message, feedback.toolTip())
        self.assertLessEqual(feedback.height(), 44)
        self.assertEqual(self.window.size().width(), 920)
        self.assertEqual(self.window.size().height(), 600)
        for widget in (self.window.claude_protection_switch, self.window.primary):
            self.assertTrue(widget.isVisible())
            self.assertTrue(widget.isEnabled())
            top_left = widget.mapTo(self.window, widget.rect().topLeft())
            self.assertTrue(self.window.rect().contains(top_left))
            self.assertTrue(self.window.rect().contains(top_left + widget.rect().bottomRight()))


if __name__ == "__main__":
    unittest.main()
