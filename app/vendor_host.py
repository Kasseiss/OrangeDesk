"""Safely align an independent FastOrange window with a Qt viewport.

The vendor remains a top-level window: no SetParent, owner, DPI, region, or
topmost changes. A separate guard restores original placement on host exit.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from PySide6.QtCore import QThread, QTimer, Qt, Signal, QSize, QEvent
from PySide6.QtWidgets import QWidget

from .engine import atomic_json, gui_command, hidden_spawn, process_identity, process_parent_id, secure_directory
from .vendor_guard import record_root
from .vendor_windows import (VendorError, WindowTarget, POINT, RECT, _api, _checked, discover_target,
                             lease_matches, mark_lease, new_token,
                             require_target, restore, snapshot, window_pid)


def _finish_record(path: Path, record: dict) -> None:
    record["state"] = "released"
    atomic_json(path, record)


def _reap_guard(process, path: Path) -> None:
    if process is None:
        return

    def finish():
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            return  # Never terminate an independent recovery process.
        for suffix in (".json", ".ready", ".result"):
            try:
                path.with_suffix(suffix).unlink()
            except OSError:
                pass
    threading.Thread(target=finish, name="vendor-guard-cleanup", daemon=True).start()


def _prepare_lease(parent_hwnd: int, qa_target: WindowTarget | None = None) -> tuple:
    def check_cancelled():
        if QThread.currentThread().isInterruptionRequested():
            raise VendorError("窗口打开已取消。")
    check_cancelled()
    target = qa_target if qa_target is not None else discover_target()
    check_cancelled()
    require_target(target)
    original = snapshot(target)
    if original["parent"]:
        raise VendorError("原厂窗口已被其他界面占用，请先释放后重试。")
    host_identity = process_identity(os.getpid())
    if not host_identity or window_pid(parent_hwnd) != os.getpid():
        raise VendorError("无法确认伴航宿主窗口身份。")
    directory = record_root()
    secure_directory(directory)
    path = directory / ("lease-" + uuid.uuid4().hex + ".json")
    record = {"version": 2, "mode": "follow", "state": "prepared", "target": target.to_dict(),
              "host": {"pid": os.getpid(), "ticks": host_identity[0], "path": host_identity[1]},
              "expected_parent": original["parent"], "anchor": parent_hwnd,
              "token": new_token(), "original": original}
    atomic_json(path, record)
    process = None
    try:
        check_cancelled()
        # main handles this flag before importing any Qt/UI modules.
        command = gui_command("--vendor-guard", str(path))
        if not getattr(sys, "frozen", False):
            # Windows venv python.exe can be a redirector which creates a
            # second PID. This Qt-free guard uses the already verified real
            # interpreter image so its Popen PID and identity are the same.
            command[0] = host_identity[1]
        process = hidden_spawn(command,
                               cwd=str(Path(__file__).resolve().parent.parent),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready_path = path.with_suffix(".ready")
        deadline = time.monotonic() + (15 if getattr(sys, "orangedesk_single_file", False) else 5)
        while time.monotonic() < deadline:
            check_cancelled()
            if ready_path.exists():
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
                if not ready.get("ok"):
                    raise VendorError(ready.get("error", "原厂窗口守护进程未能启动。"))
                guard_pid = ready.get("pid")
                independent_child = False
                if getattr(sys, "orangedesk_single_file", False) and type(guard_pid) is int and guard_pid != process.pid:
                    launcher = process_identity(process.pid)
                    child = process_identity(guard_pid)
                    independent_child = bool(launcher and child and process.poll() is None
                        and process_parent_id(guard_pid) == process.pid
                        and launcher[1] == child[1] == command[0] and child[0] >= launcher[0])
                identity = process_identity(guard_pid) if independent_child else process_identity(process.pid)
                if (not independent_child and guard_pid != process.pid or identity is None
                        or ready.get("ticks") != identity[0] or ready.get("path") != identity[1]):
                    raise VendorError("原厂窗口守护进程身份不匹配。")
                return target, record, path, process
            if process.poll() is not None:
                raise VendorError("原厂窗口守护进程未能启动；请保持快橙独立使用并重试。")
            time.sleep(.05)
        raise VendorError("原厂窗口守护进程启动超时；请保持快橙独立使用并重试。")
    except Exception:
        _finish_record(path, record)
        _reap_guard(process, path)
        raise


class _DiscoverThread(QThread):
    prepared = Signal(object)
    failed = Signal(str)

    def __init__(self, parent_hwnd: int, qa_target: WindowTarget | None):
        super().__init__()
        self.parent_hwnd = parent_hwnd
        self.qa_target = qa_target

    def run(self):
        try:
            self.prepared.emit(_prepare_lease(self.parent_hwnd, self.qa_target))
        except Exception as exc:
            self.failed.emit(str(exc) if isinstance(exc, VendorError) else "原厂窗口准备失败；快橙仍可独立使用。")


# Discovery threads outlive a disappearing page safely; no QThread is destroyed
# while running, and a pending lease is released if the page cancels attachment.
_DISCOVERY_THREADS: set[_DiscoverThread] = set()


def finish_pending_discovery() -> None:
    """Drain cancelled callbacks before Qt/Python tear down QThread objects."""
    from PySide6.QtWidgets import QApplication
    for worker in tuple(_DISCOVERY_THREADS):
        worker.requestInterruption()
    while _DISCOVERY_THREADS:
        for worker in tuple(_DISCOVERY_THREADS):
            worker.wait(50)
        QApplication.processEvents()


class VendorHost(QWidget):
    changed = Signal(str, bool)

    def __init__(self, parent=None, *, _qa_target: WindowTarget | None = None):
        super().__init__(parent)
        if _qa_target is not None and os.environ.get("ORANGEDESK_VENDOR_QA") != "1":
            raise VendorError("测试窗口注入仅用于明确启用的本地 QA。")
        self._qa_target = _qa_target
        self._target = None
        self._record = None
        self._record_path = None
        self._guard = None
        self._worker = None
        self._generation = 0
        self._shown = None
        self._last_geometry = None
        self._last_foreground = None
        self._size_warning = False
        self._preferred = QSize(360, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setMinimumSize(300, 500)
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._check_guard)

    @property
    def is_attached(self) -> bool:
        return self._target is not None

    def sizeHint(self) -> QSize:
        return self._preferred

    def request_attach(self) -> None:
        if self.is_attached:
            self.changed.emit("原厂窗口已与伴航页面联动。", True)
            return
        if self._worker is not None:
            return
        self._generation += 1
        generation = self._generation
        parent_hwnd = int(self.winId())
        worker = _DiscoverThread(parent_hwnd, self._qa_target)
        self._worker = worker
        _DISCOVERY_THREADS.add(worker)
        worker.finished.connect(lambda: _DISCOVERY_THREADS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.prepared.connect(lambda result: self._prepared(generation, result))
        worker.failed.connect(lambda message: self._failed(generation, message))
        self.changed.emit("正在准备原厂窗口…", False)
        worker.start()

    def _failed(self, generation: int, message: str) -> None:
        if generation == self._generation:
            self._worker = None
            self.changed.emit(message, False)

    def _prepared(self, generation: int, result: tuple) -> None:
        target, record, path, guard = result
        if generation != self._generation:
            _finish_record(path, record)
            _reap_guard(guard, path)
            return
        self._worker = None
        self._target, self._record, self._record_path, self._guard = target, record, path, guard
        try:
            require_target(target)
            mark_lease(target, record["token"])
            if int(_api().GetParent(target.hwnd) or 0) != record["expected_parent"]:
                raise VendorError("原厂窗口关系不匹配；已停止联动。")
            record["state"] = "following"
            atomic_json(path, record)
            rectangle = record["original"]["window_rect"]
            placement = record["original"]["placement"]
            if placement["showCmd"] in (2, 3, 6, 7, 11):
                # Minimized windows report an icon-sized outer rectangle.
                # Use their normal dimensions for the portrait viewport.
                rectangle = placement["normal"]
            self._native_size = (rectangle[2] - rectangle[0], rectangle[3] - rectangle[1])
            self.window().installEventFilter(self)
            self._watch.start()
            self.changed.emit("原厂窗口已与伴航页面联动，可直接使用快橙原有功能。", True)
            self._sync_window()
        except Exception as exc:
            message = str(exc) if isinstance(exc, VendorError) else "原厂窗口联动失败；快橙仍可独立使用。"
            try:
                self.detach()
            except VendorError as restore_error:
                message += " " + str(restore_error)
            self.changed.emit(message, False)

    def _sync_window(self):
        if not self.is_attached or not lease_matches(self._record):
            return
        user = _api()
        foreground = int(user.GetForegroundWindow() or 0)
        foreground_pid = window_pid(foreground)
        active = foreground_pid in (os.getpid(), self._target.pid)
        rectangle = RECT()
        _checked(user.GetClientRect(int(self.winId()), ctypes.byref(rectangle)), "读取原厂页面区域")
        logical_visible = self.visibleRegion().boundingRect()
        dpi = max(1, self.devicePixelRatioF())
        left = max(0, round(logical_visible.left() * dpi))
        top = max(0, round(logical_visible.top() * dpi))
        viewport_width = max(0, min(rectangle.right - left, round(logical_visible.width() * dpi)))
        viewport_height = max(0, min(rectangle.bottom - top, round(logical_visible.height() * dpi)))
        native_width, native_height = self._native_size
        fits = viewport_width >= native_width and viewport_height >= native_height
        visible = self.isVisible() and not self.window().isMinimized() and active and fits
        if visible:
            scale = min(viewport_width / native_width, viewport_height / native_height, 1.15)
            width, height = round(native_width * scale), round(native_height * scale)
            origin = POINT(left + (viewport_width - width) // 2, top + (viewport_height - height) // 2)
            _checked(user.ClientToScreen(int(self.winId()), ctypes.byref(origin)), "定位原厂页面区域")
            geometry = (origin.x, origin.y, width, height)
            if self._shown is not True:
                # Restore a minimized client before positioning it; doing this
                # afterwards would move it back to its old normal placement.
                user.ShowWindow(self._target.hwnd, 4)  # SW_SHOWNOACTIVATE
            if geometry != self._last_geometry or self._shown is not True or foreground != self._last_foreground:
                # HWND_TOP affects only ordinary z-order. SWP_NOACTIVATE keeps
                # the user's focus; this window never becomes globally topmost.
                _checked(user.SetWindowPos(self._target.hwnd, None, *geometry, 0x0010), "定位原厂窗口")
                self._last_geometry = geometry
            self._shown = True
        else:
            if self._shown is not False:
                user.ShowWindow(self._target.hwnd, 0)
            self._shown = False
        if not fits and self.isVisible() and not self._size_warning:
            self.changed.emit("请扩大伴航窗口，以完整显示快橙原厂页面。", True)
        self._size_warning = not fits and self.isVisible()
        self._last_foreground = foreground

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.is_attached:
            self._sync_window()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.is_attached:
            self._sync_window()

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_attached:
            self._sync_window()

    def eventFilter(self, watched, event):
        if self.is_attached and event.type() in (QEvent.Type.Move, QEvent.Type.Resize,
                                                QEvent.Type.WindowStateChange,
                                                QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate):
            QTimer.singleShot(0, self._sync_window)
        return super().eventFilter(watched, event)

    def detach(self) -> bool:
        self._generation += 1
        if self._worker is not None:
            self._worker.requestInterruption()
        self._worker = None
        if not self.is_attached:
            return True
        self._watch.stop()
        target, record, path = self._target, self._record, self._record_path
        if not lease_matches(record):
            _finish_record(path, record)
            self._clear_lease()
            self.changed.emit("原厂窗口已退出或已释放。", False)
            return True
        current_parent = int(_api().GetParent(target.hwnd) or 0)
        if current_parent != record["expected_parent"]:
            raise VendorError("原厂窗口已被其他宿主重新占用，未改动该窗口。请释放该宿主后重试。")
        record["state"] = "releasing"
        atomic_json(path, record)
        if not restore(record):
            raise VendorError("原厂窗口恢复校验失败，已保留守护记录。请保持伴航打开并重试。")
        _finish_record(path, record)
        self._clear_lease()
        self.changed.emit("原厂窗口已恢复为独立窗口。", False)
        return True

    def _clear_lease(self):
        _reap_guard(self._guard, self._record_path)
        self._target = self._record = self._record_path = self._guard = None
        self._shown = None
        self._last_geometry = None
        self._last_foreground = None

    def _check_guard(self):
        if self._guard is not None and self._guard.poll() is not None:
            try:
                self.detach()
                self.changed.emit("原厂窗口守护已停止，窗口已恢复为独立窗口。", False)
            except VendorError as exc:
                self.changed.emit(str(exc), self.is_attached)
                self._watch.stop()
        elif self.is_attached:
            try:
                if not lease_matches(self._record):
                    self.detach()
                    return
                self._sync_window()
            except VendorError as exc:
                self.changed.emit(str(exc), self.is_attached)
                self._watch.stop()

    def closeEvent(self, event):
        try:
            self.detach()
        except VendorError as exc:
            self.changed.emit(str(exc), self.is_attached)
            event.ignore()
            return
        event.accept()
