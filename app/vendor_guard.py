"""Independent, Qt-free recovery for one reversible foreign-window lease.

Protocol: ``--vendor-guard <LocalAppData/OrangeDesk/vendor-host/lease-UUID.json>``.
The GUI publishes a recovery record before touching the vendor HWND. This
process holds the per-user/per-process occupancy mutex until release/recovery.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sys
import time

from .engine import atomic_json, process_identity, service_root, user_sid
from .vendor_windows import VendorError, WindowTarget, _api, lease_matches, normalized_path, restore


def record_root() -> Path:
    return service_root() / "vendor-host"


def validate_record_path(value: str | Path) -> Path:
    path = Path(value)
    if not re.fullmatch(r"lease-[0-9a-f]{32}\.json", path.name):
        raise VendorError("原厂窗口恢复记录名称无效。")
    expected = record_root() / path.name
    # Compare the trusted logical filename, then resolve that file on both
    # sides. A packaged Windows caller may resolve an existing AppData file
    # through LocalCache while resolving its directory through the logical path.
    if normalized_path(str(path)) != normalized_path(str(expected)) or normalized_path(str(path.resolve())) != normalized_path(str(expected.resolve())):
        raise VendorError("原厂窗口恢复记录不在受保护目录。")
    if path.is_symlink():
        raise VendorError("原厂窗口恢复记录不能使用链接。")
    return expected


def read_record(path: Path) -> dict:
    if path.stat().st_size > 16384:
        raise VendorError("原厂窗口恢复记录过大。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 2 or data.get("mode") != "follow" or data.get("state") not in ("prepared", "following", "releasing", "released"):
        raise VendorError("原厂窗口恢复记录版本或状态无效。")
    for identity in (data["target"], data["host"]):
        if not isinstance(identity["pid"], int) or identity["pid"] <= 0:
            raise VendorError("原厂窗口进程记录无效。")
        if not isinstance(identity["ticks"], int) or not isinstance(identity["path"], str):
            raise VendorError("原厂窗口身份记录无效。")
    if not isinstance(data["token"], int) or not 0 < data["token"] < 2 ** 63:
        raise VendorError("原厂窗口占用记录无效。")
    if not isinstance(data["expected_parent"], int) or data["expected_parent"] != 0 or not isinstance(data["anchor"], int) or not data["anchor"]:
        raise VendorError("原厂窗口宿主记录无效。")
    WindowTarget(**data["target"])
    return data


class LeaseMutex:
    def __init__(self, target: WindowTarget):
        self.handle = None
        self.target = target

    def acquire(self) -> bool:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateMutexW(None, False,
            f"Local\\OrangeDesk-VendorHost-{user_sid()}-{self.target.pid}-{self.target.ticks}")
        if not self.handle:
            raise VendorError("无法建立原厂窗口占用锁。")
        result = kernel.WaitForSingleObject(self.handle, 0)
        if result in (0, 0x80):
            return True
        kernel.CloseHandle(self.handle)
        self.handle = None
        return False

    def close(self) -> None:
        if self.handle:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.ReleaseMutex(self.handle)
            kernel.CloseHandle(self.handle)
            self.handle = None


def run_guard(value: str | Path) -> int:
    """No Qt imports, UI controls, account inspection, or network changes."""
    path = validate_record_path(value)
    ready = path.with_suffix(".ready")
    result_path = path.with_suffix(".result")
    mutex = None
    host_handle = None
    previous_dpi = None
    try:
        record = read_record(path)
        target = WindowTarget(**record["target"])
        host = record["host"]
        identity = process_identity(host["pid"])
        if not identity or identity[0] != host["ticks"] or normalized_path(identity[1]) != normalized_path(host["path"]):
            raise VendorError("原厂窗口宿主身份已失效。")
        # The saved expected parent must really belong to this exact GUI.
        from .vendor_windows import window_pid
        if window_pid(record["anchor"]) != host["pid"]:
            raise VendorError("原厂窗口宿主句柄身份不匹配。")
        # Window coordinates were captured by Qt's per-monitor-aware GUI.
        # Match only this guard thread to that awareness so restoration does
        # not virtualize the saved placement at 125%/mixed-monitor scaling.
        user = _api()
        user.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
        user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous_dpi = user.SetThreadDpiAwarenessContext(user.GetWindowDpiAwarenessContext(record["anchor"]))
        if not previous_dpi:
            raise VendorError("无法按原缩放比例守护原厂窗口。")
        mutex = LeaseMutex(target)
        if not mutex.acquire():
            raise VendorError("原厂窗口已被另一个伴航界面占用，请先关闭该界面。")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        host_handle = kernel.OpenProcess(0x100000 | 0x1000, False, host["pid"])
        if not host_handle or process_identity(host["pid"]) != identity:
            raise VendorError("无法监护原厂窗口宿主进程。")
        own_identity = process_identity(os.getpid())
        atomic_json(ready, {"ok": True, "pid": os.getpid(), "ticks": own_identity[0], "path": own_identity[1]})
        while True:
            if kernel.WaitForSingleObject(host_handle, 150) == 0:
                latest = read_record(path)
                # In-place file replacement cannot change this guard's lease.
                if any(latest[key] != record[key] for key in ("token", "target", "host", "original", "expected_parent", "anchor", "mode", "version")):
                    raise VendorError("恢复记录已被替换，已停止自动恢复。")
                recovery_deadline = time.monotonic() + 1.5
                recovery_error = ""
                while True:
                    try:
                        restored = restore(latest)
                        break
                    except VendorError as exc:
                        recovery_error = str(exc)
                        if time.monotonic() >= recovery_deadline:
                            restored = False
                            break
                        time.sleep(.025)
                from .vendor_windows import window_pid, PROPERTY_NAME
                user = _api()
                diagnostic = {"restored": restored, "reason": "host-exited",
                              "window_exists": bool(user.IsWindow(target.hwnd)),
                              "target_process_alive": process_identity(target.pid) is not None,
                              "window_pid": window_pid(target.hwnd),
                              "parent": int(user.GetParent(target.hwnd) or 0),
                              "lease_property": int(user.GetPropW(target.hwnd, PROPERTY_NAME) or 0)}
                if recovery_error:
                    diagnostic["error"] = recovery_error
                atomic_json(result_path, diagnostic)
                return 0 if restored or not lease_matches(latest) else 1
            try:
                latest = read_record(path)
            except FileNotFoundError:
                # The host removes a completed record only after release.
                return 0 if not lease_matches(record) else 1
            if any(latest[key] != record[key] for key in ("token", "target", "host", "original", "expected_parent", "anchor", "mode", "version")):
                raise VendorError("恢复记录已被替换，已停止自动恢复。")
            if latest["state"] == "released":
                return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, VendorError) else "原厂窗口守护进程发生错误。"
        atomic_json(ready if not ready.exists() else result_path, {"ok": False, "error": message})
        return 1
    finally:
        if host_handle:
            kernel = ctypes.WinDLL("kernel32")
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(host_handle)
        if mutex:
            mutex.close()
        if previous_dpi:
            _api().SetThreadDpiAwarenessContext(previous_dpi)


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if len(arguments) == 2 and arguments[0] == "--vendor-guard":
        raise SystemExit(run_guard(arguments[1]))
    raise SystemExit(2)
