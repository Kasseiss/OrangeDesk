"""Qt-free Win32 identity, lease and reversible foreign-window operations."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets

from .engine import process_identity, resource_root


class VendorError(RuntimeError):
    pass


def normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    pid: int
    ticks: int
    path: str

    def to_dict(self) -> dict:
        return asdict(self)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                ("name", wintypes.WCHAR * 260)]


def _api():
    if os.name != "nt":
        raise VendorError("原厂窗口联动目前仅支持 Windows。")
    user = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "IsWindow": ([wintypes.HWND], wintypes.BOOL),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "IsHungAppWindow": ([wintypes.HWND], wintypes.BOOL),
        "GetParent": ([wintypes.HWND], wintypes.HWND),
        "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
        "SetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "GetWindowPlacement": ([wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)], wintypes.BOOL),
        "SetWindowPlacement": ([wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)], wintypes.BOOL),
        "GetClientRect": ([wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(POINT)], wintypes.BOOL),
        "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "SetPropW": ([wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE], wintypes.BOOL),
        "GetPropW": ([wintypes.HWND, wintypes.LPCWSTR], wintypes.HANDLE),
        "RemovePropW": ([wintypes.HWND, wintypes.LPCWSTR], wintypes.HANDLE),
        "GetWindowDpiAwarenessContext": ([wintypes.HWND], ctypes.c_void_p),
        "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
    }
    for name, (args, result) in signatures.items():
        function = getattr(user, name)
        function.argtypes = args
        function.restype = result
    return user


def _checked(value, operation: str):
    if not value:
        raise VendorError(f"{operation}失败（Windows {ctypes.get_last_error()}）。请保持快橙独立打开后重试。")
    return value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _api().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def matches_target(target: WindowTarget) -> bool:
    identity = process_identity(target.pid)
    return bool(identity and identity[0] == target.ticks
                and normalized_path(identity[1]) == normalized_path(target.path)
                and _api().IsWindow(target.hwnd) and window_pid(target.hwnd) == target.pid)


def require_target(target: WindowTarget) -> None:
    if not matches_target(target):
        raise VendorError("快橙窗口已退出或发生变化；已停止联动，请重新打开原厂页面。")


def _vendor_processes() -> list[WindowTarget]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise VendorError("无法检查快橙进程，请稍后重试。")
    entries = []
    try:
        entry = PROCESSENTRY32()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.name.casefold() == "fastorange.exe":
                identity = process_identity(entry.pid)
                if identity and Path(identity[1]).name.casefold() == "fastorange.exe":
                    entries.append(WindowTarget(0, entry.pid, identity[0], identity[1]))
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return entries


def _official_hashes() -> set[str]:
    try:
        manifest = json.loads((resource_root() / "vendor/PUBLIC-PAYLOAD.json").read_text(encoding="utf-8-sig"))
        return {item["sha256"].casefold() for item in manifest["files"]
                if item["name"].casefold() == "fastorange.exe"}
    except (OSError, ValueError, KeyError, TypeError):
        raise VendorError("缺少原厂版本校验资料。请修复伴航安装；快橙仍可独立使用。") from None


def discover_target() -> WindowTarget:
    """Runs in a worker; no title/account/UIAutomation inspection is needed."""
    user = _api()
    processes = _vendor_processes()
    if not processes:
        raise VendorError("快橙尚未打开。请先打开快橙，再进入原厂页面。")
    verified = {}
    hashes = _official_hashes()
    for target in processes:
        try:
            with open(target.path, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            continue
        if digest in hashes:
            verified[target.pid] = target
    if not verified:
        raise VendorError("当前快橙版本尚未通过窗口校验。请更新伴航版本；快橙仍可独立使用。")
    if len(verified) != 1:
        raise VendorError("检测到多个快橙实例。请保留一个实例，再重试原厂页面。")
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    matches = []

    def inspect(hwnd, _):
        pid = window_pid(hwnd)
        if pid not in verified or user.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        class_name = ctypes.create_unicode_buffer(512)
        user.GetClassNameW(hwnd, class_name, len(class_name))
        # WPF creates helper/message windows too. A substantial, unowned
        # HwndWrapper with a real client area is the main window, even when its
        # caption is empty or it is hidden in the tray.
        if not class_name.value.startswith("HwndWrapper["):
            return True
        rectangle = RECT()
        if user.GetClientRect(hwnd, ctypes.byref(rectangle)) and rectangle.right >= 200 and rectangle.bottom >= 250:
            original = verified[pid]
            matches.append(WindowTarget(int(hwnd), pid, original.ticks, original.path))
        return True

    callback = callback_type(inspect)
    _checked(user.EnumWindows(callback, 0), "检查原厂窗口")
    if len(matches) != 1:
        raise VendorError("未找到唯一的原厂主窗口。请回到快橙主页、关闭弹窗后重试。")
    require_target(matches[0])
    if user.IsHungAppWindow(matches[0].hwnd):
        raise VendorError("快橙主窗口暂时没有响应。请等待原厂窗口恢复后重试。")
    return matches[0]


def snapshot(target: WindowTarget) -> dict:
    require_target(target)
    user = _api()
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    _checked(user.GetWindowPlacement(target.hwnd, ctypes.byref(placement)), "记录原厂窗口位置")
    rectangle = RECT()
    _checked(user.GetClientRect(target.hwnd, ctypes.byref(rectangle)), "记录原厂窗口大小")
    window_rectangle = RECT()
    _checked(user.GetWindowRect(target.hwnd, ctypes.byref(window_rectangle)), "记录原厂窗口边框")
    return {
        "parent": int(user.GetParent(target.hwnd) or 0),
        "style": int(user.GetWindowLongPtrW(target.hwnd, -16)) & 0xFFFFFFFF,
        "exstyle": int(user.GetWindowLongPtrW(target.hwnd, -20)) & 0xFFFFFFFF,
        "placement": {
            "flags": placement.flags, "showCmd": placement.showCmd,
            "minimum": [placement.ptMinPosition.x, placement.ptMinPosition.y],
            "maximum": [placement.ptMaxPosition.x, placement.ptMaxPosition.y],
            "normal": [placement.rcNormalPosition.left, placement.rcNormalPosition.top,
                       placement.rcNormalPosition.right, placement.rcNormalPosition.bottom],
        },
        "visible": bool(user.IsWindowVisible(target.hwnd)),
        "client_size": [rectangle.right - rectangle.left, rectangle.bottom - rectangle.top],
        "window_rect": [window_rectangle.left, window_rectangle.top, window_rectangle.right, window_rectangle.bottom],
    }


PROPERTY_NAME = "OrangeDesk.VendorLease"


def mark_lease(target: WindowTarget, token: int) -> None:
    require_target(target)
    user = _api()
    if user.GetPropW(target.hwnd, PROPERTY_NAME):
        raise VendorError("原厂窗口正被另一个伴航界面占用，请先退出该界面。")
    _checked(user.SetPropW(target.hwnd, PROPERTY_NAME, token), "标记原厂窗口占用")


def lease_matches(record: dict) -> bool:
    target = WindowTarget(**record["target"])
    return matches_target(target) and int(_api().GetPropW(target.hwnd, PROPERTY_NAME) or 0) == record["token"]


def set_long(hwnd: int, index: int, value: int) -> None:
    ctypes.set_last_error(0)
    result = _api().SetWindowLongPtrW(hwnd, index, value)
    if not result and ctypes.get_last_error():
        _checked(False, "恢复原厂窗口样式")


def restore(record: dict) -> bool:
    """Restore only this top-level lease; never change a parent or DPI context."""
    if not lease_matches(record):
        return False
    target = WindowTarget(**record["target"])
    user = _api()
    current_parent = int(user.GetParent(target.hwnd) or 0)
    expected = record["expected_parent"]
    original = record["original"]
    if current_parent != expected or current_parent != original["parent"]:
        return False
    parent = original["parent"]
    if parent and not user.IsWindow(parent):
        raise VendorError("原厂窗口原有父窗口已退出，无法按原状态释放。")
    set_long(target.hwnd, -16, original["style"])
    set_long(target.hwnd, -20, original["exstyle"])
    _checked(user.SetWindowPos(target.hwnd, None, 0, 0, 0, 0, 0x0037), "恢复原厂窗口边框")
    data = original["placement"]
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    # WPF_SETMINPOSITION is needed for the saved minimized coordinates.
    # GetWindowPlacement still reports flags=0 after restoration.
    placement.flags = data["flags"] | 1
    # Normal/minimized restoration can preserve placement without taking
    # foreground focus from an unrelated application.
    placement.showCmd = 4 if data["showCmd"] == 1 else 7 if data["showCmd"] == 2 else data["showCmd"]
    placement.ptMinPosition = POINT(*data["minimum"])
    placement.ptMaxPosition = POINT(*data["maximum"])
    placement.rcNormalPosition = RECT(*data["normal"])
    _checked(user.SetWindowPlacement(target.hwnd, ctypes.byref(placement)), "恢复原厂窗口位置")
    user.ShowWindow(target.hwnd, 8 if original["visible"] else 0)
    user.RemovePropW(target.hwnd, PROPERTY_NAME)
    return True


def new_token() -> int:
    # Windows property HANDLE is pointer-sized; avoid negative signed values.
    return secrets.randbits(ctypes.sizeof(ctypes.c_void_p) * 8 - 1) or 1
