"""Only explicit, self-created native stubs are manipulated by these tests."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine import atomic_json, process_identity
from app.vendor_windows import RECT, WindowTarget, _api, snapshot


def native_stub(output: str) -> int:
    user = _api()
    # WPF uses system awareness; this change applies only to this self-created
    # QA process and never to the real vendor or the companion GUI.
    user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
    user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.PostQuitMessage.argtypes = [ctypes.c_int]

    def procedure(hwnd, message, wparam, lparam):
        if message == 0x10:  # Closing our QA stub only.
            user.DestroyWindow(hwnd)
            return 0
        if message == 2:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    callback = callback_type(procedure)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("procedure", callback_type),
                    ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                    ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                    ("cursor", wintypes.HANDLE), ("background", wintypes.HBRUSH),
                    ("menu", wintypes.LPCWSTR), ("name", wintypes.LPCWSTR)]

    instance = kernel.GetModuleHandleW(None)
    window_class = WNDCLASS()
    window_class.procedure = callback
    window_class.instance = instance
    window_class.background = 6
    window_class.name = "OrangeDeskNativeQAStub"
    user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user.RegisterClassW.restype = wintypes.ATOM
    if not user.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = wintypes.HWND
    hwnd = user.CreateWindowExW(0, window_class.name, "OrangeDesk embedding QA stub", 0x00CF0000,
                               120, 140, 360, 600, None, None, instance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    user.ShowWindow(hwnd, 4)  # Only our stub; never take user foreground focus.
    ticks, image = process_identity(os.getpid())
    target = WindowTarget(int(hwnd), os.getpid(), ticks, image)
    atomic_json(Path(output), {"target": target.to_dict(), "original": snapshot(target)})
    user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    message = wintypes.MSG()
    while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user.TranslateMessage(ctypes.byref(message))
        user.DispatchMessageW(ctypes.byref(message))
    return 0


def dpi_state(hwnd: int) -> dict:
    user = _api()
    user.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
    user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    user.GetDpiForWindow.argtypes = [wintypes.HWND]
    user.GetDpiForWindow.restype = wintypes.UINT
    return {"dpi": int(user.GetDpiForWindow(hwnd)),
            "awareness": user.GetAwarenessFromDpiAwarenessContext(user.GetWindowDpiAwarenessContext(hwnd))}


def wait_for(predicate, timeout=6):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(.015)
    return False


def qa_crash_host(stub_path: str, output: str) -> int:
    os.environ["ORANGEDESK_VENDOR_QA"] = "1"
    from PySide6.QtWidgets import QApplication
    from app.vendor_host import VendorHost
    app = QApplication([])
    data = json.loads(Path(stub_path).read_text())
    target = WindowTarget(**data["target"])
    host = VendorHost(_qa_target=target)
    host.resize(300, 500)
    host.show()
    messages = []
    host.changed.connect(lambda text, attached: messages.append(text))
    host.request_attach()
    if not wait_for(lambda: host.is_attached and host._record["state"] == "following"):
        atomic_json(Path(output), {"error": messages})
        return 1
    atomic_json(Path(output), {"record": str(host._record_path), "dpi": dpi_state(target.hwnd)})
    return app.exec()


@unittest.skipUnless(os.name == "nt", "Windows foreign-window lifecycle")
class VendorLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        os.environ["ORANGEDESK_VENDOR_QA"] = "1"
        self.temp = tempfile.TemporaryDirectory(prefix="orange-vendor-qa-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.stub_path = self.directory / "stub.json"
        from app.engine import hidden_spawn
        self.stub = hidden_spawn([sys.executable, __file__, "--stub", str(self.stub_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._close_stub)
        self.assertTrue(wait_for(self.stub_path.exists))
        self.data = json.loads(self.stub_path.read_text())
        self.target = WindowTarget(**self.data["target"])
        self.original = snapshot(self.target)
        from app.vendor_windows import window_pid as real_window_pid
        def own_foreground(hwnd):
            # Exercise the follow branch without activating a test window over
            # whichever application the user is currently using.
            if hwnd == int(_api().GetForegroundWindow() or 0):
                return os.getpid()
            return real_window_pid(hwnd)
        foreground = patch("app.vendor_host.window_pid", side_effect=own_foreground)
        foreground.start()
        self.addCleanup(foreground.stop)

    def _close_stub(self):
        from app.vendor_windows import matches_target
        if self.stub.poll() is None and self.stub_path.exists():
            target = WindowTarget(**json.loads(self.stub_path.read_text())["target"])
            if matches_target(target):
                user = _api()
                user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user.PostMessageW(target.hwnd, 0x10, 0, 0)
        try:
            self.stub.wait(3)
        except subprocess.TimeoutExpired:
            self.stub.kill()  # Only the explicitly self-created QA process.
            self.stub.wait(3)

    def _host(self):
        from app.vendor_host import VendorHost
        host = VendorHost(_qa_target=self.target)
        host.resize(300, 500)
        host.show()
        self.addCleanup(host.close)
        messages = []
        host.changed.connect(lambda text, attached: messages.append(text))
        host.request_attach()
        self.assertTrue(wait_for(lambda: host.is_attached and host._record["state"] == "following"), messages)
        return host

    def assert_restored(self):
        self.assertIsNone(self.stub.poll())
        actual = snapshot(self.target)
        for key in ("parent", "style", "exstyle", "placement", "visible", "client_size"):
            self.assertEqual(actual[key], self.original[key], key)

    def test_resize_detach_restores_exact_original_and_keeps_native_window(self):
        before_dpi = dpi_state(self.target.hwnd)
        host = self._host()
        embedded = snapshot(self.target)
        self.assertEqual(embedded["parent"], 0)
        host.resize(420, 710)
        self.assertTrue(wait_for(lambda: snapshot(self.target)["client_size"] != embedded["client_size"]))
        resized = snapshot(self.target)["window_rect"]
        self.assertAlmostEqual((resized[2] - resized[0]) / (resized[3] - resized[1]), .6, delta=.01)
        embedded_dpi = dpi_state(self.target.hwnd)
        self.assertEqual(embedded_dpi, before_dpi)
        host.hide()
        self.assertTrue(wait_for(lambda: not _api().IsWindowVisible(self.target.hwnd)))
        self.assertEqual(_api().GetParent(self.target.hwnd), None)
        host.show()
        self.assertTrue(wait_for(lambda: bool(_api().IsWindowVisible(self.target.hwnd))))
        self.assertTrue(host.detach())
        self.assertFalse(host.is_attached)
        self.assert_restored()
        self.assertEqual(dpi_state(self.target.hwnd), before_dpi)
        print("QA DPI:", before_dpi, "embedded:", embedded_dpi, "restored:", dpi_state(self.target.hwnd), "Qt scale:", host.devicePixelRatioF())
        self.assertTrue(wait_for(lambda: not any(t.isRunning() for t in __import__("app.vendor_host", fromlist=["_DISCOVERY_THREADS"])._DISCOVERY_THREADS)))

    def test_killed_host_guard_restores_window_and_vendor_process_survives(self):
        from app.engine import hidden_spawn
        ready = self.directory / "host.json"
        host = hidden_spawn([sys.executable, __file__, "--qa-crash-host", str(self.stub_path), str(ready)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: host.kill() if host.poll() is None else None)
        self.assertTrue(wait_for(ready.exists))
        detail = json.loads(ready.read_text())
        self.assertNotIn("error", detail)
        record_path = Path(detail["record"])
        host.kill()  # Explicit QA host crash; never the vendor process.
        host.wait(3)
        result = record_path.with_suffix(".result")
        self.assertTrue(wait_for(result.exists), "guard did not report recovery")
        recovery = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(recovery.get("restored"), recovery)
        self.assert_restored()

    def test_initially_minimized_window_uses_normal_size_and_restores_minimized(self):
        _api().ShowWindow(self.target.hwnd, 2)
        self.original = snapshot(self.target)
        self.assertEqual(self.original["placement"]["showCmd"], 2)
        normal = self.original["placement"]["normal"]
        from app.vendor_windows import window_pid as real_window_pid
        def own_foreground(hwnd):
            # Deterministic activation of our own fixture; do not steal focus
            # from the user's foreground application.
            if hwnd == int(_api().GetForegroundWindow() or 0):
                return os.getpid()
            return real_window_pid(hwnd)
        with patch("app.vendor_host.window_pid", side_effect=own_foreground):
            host = self._host()
            self.assertEqual(host._native_size, (normal[2] - normal[0], normal[3] - normal[1]))
            host.resize(420, 710)
            self.assertTrue(wait_for(lambda: snapshot(self.target)["placement"]["showCmd"] == 1))
            self.assertTrue(host.detach())
        self.assert_restored()

    def test_scroll_viewport_hides_until_original_window_fits(self):
        from PySide6.QtWidgets import QScrollArea
        from app.vendor_host import VendorHost
        area = QScrollArea()
        area.setWidgetResizable(True)
        host = VendorHost(_qa_target=self.target)
        host.setMinimumSize(340, 600)
        area.setWidget(host)
        area.resize(420, 360)
        area.show()
        self.addCleanup(area.close)
        self.addCleanup(host.detach)
        messages = []
        host.changed.connect(lambda text, attached: messages.append((text, attached)))
        host.request_attach()
        self.assertTrue(wait_for(lambda: host.is_attached and host._record["state"] == "following"), messages)
        self.assertTrue(wait_for(lambda: not _api().IsWindowVisible(self.target.hwnd)))
        self.assertTrue(any(attached and "扩大" in message for message, attached in messages), messages)
        area.resize(460, 720)
        self.assertTrue(wait_for(lambda: bool(_api().IsWindowVisible(self.target.hwnd))))
        self.assertTrue(host.detach())
        self.assert_restored()

    def test_detach_cancels_late_discovery_without_touching_stub(self):
        from app import vendor_host
        host = vendor_host.VendorHost(_qa_target=self.target)
        host.show()
        self.addCleanup(host.close)
        prepare = vendor_host._prepare_lease

        def delayed(*args):
            time.sleep(.1)
            return prepare(*args)

        with patch("app.vendor_host._prepare_lease", side_effect=delayed):
            host.request_attach()
            self.assertTrue(host.detach())
            self.assertTrue(wait_for(lambda: not any(t.isRunning() for t in vendor_host._DISCOVERY_THREADS)))
        self.assertFalse(host.is_attached)
        self.assert_restored()


class VendorSafetyTests(unittest.TestCase):
    def test_gui_exit_drains_cancelled_discovery_without_qthread_crash(self):
        from app.engine import hidden_spawn
        code = """import time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from app.vendor_host import VendorHost, finish_pending_discovery
from app.vendor_windows import VendorError
app=QApplication([])
def delayed(*args):
    time.sleep(.15)
    raise VendorError('own QA cancellation')
with patch('app.vendor_host._prepare_lease',side_effect=delayed):
    host=VendorHost()
    host.request_attach()
    host.close()
    QTimer.singleShot(0,app.quit)
    result=app.exec()
    finish_pending_discovery()
raise SystemExit(result)
"""
        process = hidden_spawn([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, error.decode(errors="replace"))

    def test_guard_path_rejects_outside_protected_directory(self):
        from app.vendor_guard import validate_record_path
        from app.vendor_windows import VendorError
        with self.assertRaises(VendorError):
            validate_record_path(Path(tempfile.gettempdir()) / ("lease-" + "a" * 32 + ".json"))

    def test_reused_process_identity_never_changes_window(self):
        from app.vendor_windows import restore
        target = WindowTarget(123, 456, 100, "C:/FastOrange.exe")
        record = {"target": target.to_dict(), "token": 10}
        with patch("app.vendor_windows.process_identity", return_value=(101, "C:/FastOrange.exe")), patch("app.vendor_windows._api") as user:
            self.assertFalse(restore(record))
            user.return_value.SetParent.assert_not_called()

    def test_new_parent_never_restored_even_with_matching_vendor_identity(self):
        from app.vendor_windows import restore
        user = Mock()
        user.GetParent.return_value = 333
        record = {"target": WindowTarget(123, 456, 100, "C:/FastOrange.exe").to_dict(),
                  "token": 10, "expected_parent": 222, "original": {"parent": 0},
                  "host": {"pid": 1, "ticks": 50, "path": "C:/host.exe"}}
        with patch("app.vendor_windows.lease_matches", return_value=True), patch("app.vendor_windows._api", return_value=user), patch("app.vendor_windows.process_identity", return_value=(50, "C:/host.exe")):
            self.assertFalse(restore(record))
            user.SetParent.assert_not_called()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--stub":
        raise SystemExit(native_stub(sys.argv[2]))
    if len(sys.argv) >= 4 and sys.argv[1] == "--qa-crash-host":
        raise SystemExit(qa_crash_host(sys.argv[2], sys.argv[3]))
    unittest.main()
