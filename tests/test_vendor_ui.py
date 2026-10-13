"""Companion interaction tests using only a fake vendor widget.

Never launches or discovers the actual VPN client or changes network settings.
"""
from pathlib import Path
import tempfile
import sys
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
        self.attach_calls = 0
        self.detach_calls = 0
        self.detach_result = True

    def request_attach(self):
        self.attach_calls += 1

    def detach(self):
        self.detach_calls += 1
        if self.detach_result:
            self.is_attached = False
        return self.detach_result


# Import the GUI without even loading native vendor discovery in this test.
fake_vendor_module = ModuleType("app.vendor_host")
fake_vendor_module.VendorHost = FakeVendorHost
fake_vendor_module.finish_pending_discovery = lambda: None
with patch.dict(sys.modules, {"app.vendor_host": fake_vendor_module}):
    from app import ui


class VendorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(ui, "VendorHost", FakeVendorHost)
        self.patch.start()
        self.window = ui.CompanionWindow(
            preview=True, appearance_path=Path(self.temp.name) / "appearance.json"
        )
        self.window.tray.hide()
        self.window._submit = Mock()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.vendor_host.detach_result = True
        self.window._owned = False
        self.window._closed = True
        self.window.close()
        self.window.appearance_pool.waitForDone(5000)
        self.app.processEvents()
        self.patch.stop()
        self.temp.cleanup()

    def activate(self, running=True):
        self.window.preview = False
        self.window._status.update(vpn_running=running, vendor_installed=True)

    def test_preview_vendor_page_never_launches_or_discovers(self):
        self.window._navigate(4)
        self.window._request_vendor_embed()
        self.assertEqual(self.window.vendor_host.attach_calls, 0)
        self.window._submit.assert_not_called()

    def test_running_client_enters_page_without_launching_again(self):
        self.activate()
        self.window._navigate(4)
        self.assertEqual(self.window.vendor_host.attach_calls, 1)
        self.window._submit.assert_not_called()

    def test_missing_client_opens_official_entry_once(self):
        self.activate(running=False)
        self.window._navigate(4)
        self.window._request_vendor_embed()
        self.window._submit.assert_called_once_with("open_vpn", {"embed": True})
        self.assertEqual(self.window.vendor_host.attach_calls, 0)

    def test_popout_cancels_late_launch_response(self):
        self.activate(running=False)
        self.window._navigate(4)
        self.window._detach_vendor()
        self.window._task_succeeded("open_vpn", {"state": "stopped", "vpn_running": True})
        self.assertFalse(self.window._vendor_auto_embed)
        self.assertFalse(self.window._vendor_launch_pending)
        self.assertEqual(self.window.vendor_host.attach_calls, 0)

    def test_close_cancels_pending_launch(self):
        self.activate(running=False)
        self.window._navigate(4)
        self.window.close()
        self.window._task_succeeded("open_vpn", {"state": "stopped", "vpn_running": True})
        self.assertTrue(self.window._closed)
        self.assertEqual(self.window.vendor_host.attach_calls, 0)
        self.assertEqual(self.window.vendor_host.detach_calls, 1)

    def test_failed_popout_does_not_report_success(self):
        self.activate()
        self.window.vendor_host.detach_result = False
        self.window._detach_vendor()
        self.assertIn("暂未恢复", self.window.vendor_notice.text())
        self.assertFalse(self.window._vendor_auto_embed)

    def test_failed_close_preserves_window_without_reattaching(self):
        self.activate()
        self.window.vendor_host.detach_result = False
        self.window.close()
        self.app.processEvents()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._closed)
        self.assertFalse(self.window._vendor_auto_embed)
        self.assertEqual(self.window.pages.currentIndex(), 4)
        self.assertEqual(self.window.vendor_host.attach_calls, 0)

    def test_vendor_entry_cancels_unfinished_page_snapshot(self):
        self.window._navigate(1)
        self.window._navigate(4)
        self.assertFalse(self.window.page_transition.isVisible())
        self.assertEqual(self.window.pages.currentIndex(), 4)
        self.assertEqual(self.window.page_transition.motion.state().value, 0)

    def test_attached_control_state_is_explicit(self):
        self.window._vendor_host_changed("已显示快橙", True)
        self.assertFalse(self.window.vendor_embed_button.isEnabled())
        self.assertTrue(self.window.vendor_detach_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
