"""Small Windows coordinator for the proven temporary FastOrange runtime.

Starting this service is read-only. Only an explicit start RPC starts the worker
that owns the temporary proxy lease; the worker owns all proxy restoration.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .whitelist import WhitelistStore, WhitelistError, normalize_target
from .storage import state_root, migrate_preferences
from .diagnostics import NetworkDiagnostics
from .claude_protection import ClaudeProtection, ProtectionError
from .wechat_direct import WeChatDirect, WeChatDirectError

APP_VERSION = "1.3.3"
FILETIME_TO_DOTNET = 504911232000000000
IDLE_SECONDS = 120


class ServiceError(RuntimeError):
    pass


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).resolve().parent.parent / "resources"


def service_root() -> Path:
    return state_root()


def initialize_storage() -> None:
    migrate_preferences(service_root(), writer=atomic_json, secure=secure_directory)


def gui_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable) if getattr(sys, "orangedesk_single_file", False) else Path(sys.executable).with_name("OrangeDesk.exe")
        if not executable.is_file():
            raise ServiceError("找不到 OrangeDesk.exe，请修复安装。")
        return [str(executable), *args]
    return [sys.executable, "-m", "app.main", *args]


def powershell_path() -> str:
    if os.name != "nt":
        raise ServiceError("OrangeDesk 目前支持 Windows。")
    path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not path.is_file():
        raise ServiceError("找不到 Windows PowerShell 5.1。")
    return str(path)


def hidden_spawn(args: list[str], **kwargs) -> subprocess.Popen:
    if getattr(sys, "orangedesk_single_file", False) and args and os.path.normcase(os.path.abspath(args[0])) == os.path.normcase(os.path.abspath(sys.executable)):
        # Daemon and recovery guard can outlive their invoking UI/CLI. Give
        # each an independent extraction lifetime instead of its parent's files.
        kwargs["env"] = dict(kwargs.get("env", os.environ), PYINSTALLER_RESET_ENVIRONMENT="1")
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        kwargs.setdefault("startupinfo", info)
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen(args, **kwargs)


def process_identity(pid: int) -> tuple[int, str] | None:
    """Return creation identity and full image path without invoking a shell."""
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
            return None
        length = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            return None
        ticks = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        return ticks + FILETIME_TO_DOTNET, image.value
    finally:
        kernel.CloseHandle(handle)


def process_parent_id(pid: int) -> int | None:
    """Verify the direct child relationship of a one-file bootloader."""
    if os.name != "nt":
        return None
    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t), ("parent", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    native.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    native.NtQueryInformationProcess.restype = wintypes.LONG
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        value, length = BasicInformation(), wintypes.ULONG()
        if native.NtQueryInformationProcess(handle, 0, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length)) < 0:
            return None
        return int(value.parent)
    finally:
        kernel.CloseHandle(handle)


def _matching_gui_windows(executable: str) -> list[tuple[int, int, int]]:
    """Enumerate Qt main windows belonging to this exact installed GUI image."""
    if os.name != "nt":
        raise ServiceError("关闭安装界面目前仅支持 Windows。")
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    expected = os.path.normcase(os.path.abspath(executable))
    matches = []

    def inspect_window(handle, parameter):
        length = user.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(min(length + 1, 4096))
        user.GetWindowTextW(handle, title, len(title))
        if not title.value.startswith("快橙伴航"):
            return True
        window_class = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle, window_class, len(window_class))
        if not window_class.value.startswith("Qt"):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        identity = process_identity(pid.value)
        if identity and os.path.normcase(os.path.abspath(identity[1])) == expected:
            matches.append((int(handle), pid.value, identity[0]))
        return True

    callback = callback_type(inspect_window)
    if not user.EnumWindows(callback, 0):
        raise ServiceError("无法检查当前安装的界面窗口，请手动关闭后重试。")
    return matches


def _post_gui_close(handle: int, expected_pid: int) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(4096)
    window_class = ctypes.create_unicode_buffer(256)
    user.GetWindowTextW(handle, title, len(title))
    user.GetClassNameW(handle, window_class, len(window_class))
    # A HWND can be recycled while waiting; check its owner and main-window
    # identity again immediately before posting the normal close message.
    if pid.value != expected_pid or not title.value.startswith("快橙伴航") or not window_class.value.startswith("Qt"):
        return
    if not user.PostMessageW(handle, 0x0010, 0, 0):
        raise ServiceError("无法正常关闭快橙伴航界面，请手动关闭后重试。")


def close_installed_ui(timeout: float = 20) -> None:
    """Installer-only graceful closure. Never terminate or close other images."""
    executable = gui_command()[0]
    if not getattr(sys, "orangedesk_single_file", False) and Path(executable).name.casefold() != "orangedesk.exe":
        raise ServiceError("--close-ui 用于已安装的 OrangeDesk，请先手动关闭源码运行的界面。")
    expected = os.path.normcase(os.path.abspath(executable))
    windows = _matching_gui_windows(executable)
    pending = {}
    for handle, pid, ticks in windows:
        identity = process_identity(pid)
        if identity is None or identity[0] != ticks or os.path.normcase(os.path.abspath(identity[1])) != expected:
            continue
        _post_gui_close(handle, pid)
        pending[pid] = ticks
    deadline = time.monotonic() + max(0, timeout)
    while pending:
        for pid, ticks in list(pending.items()):
            identity = process_identity(pid)
            if identity is None or identity[0] != ticks or os.path.normcase(os.path.abspath(identity[1])) != expected:
                pending.pop(pid)
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise ServiceError("快橙伴航界面仍未退出，可能正在恢复网络设置。更新或卸载已停止；请等待或查看界面提示后重试。")
        time.sleep(0.1)


def user_sid() -> str:
    if os.name != "nt":
        return str(os.getuid())
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ServiceError("无法读取当前 Windows 用户身份。")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise ServiceError("无法读取当前 Windows 用户身份。")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ServiceError("无法读取当前 Windows 用户身份。")
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
    finally:
        kernel.CloseHandle(token)


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    descriptor = ctypes.c_void_p()
    # Protect the bearer token from other local users. Children inherit this ACL.
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{user_sid()})"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ServiceError("无法保护本地控制目录。")
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ServiceError("无法保护本地控制目录。")
    finally:
        kernel.LocalFree(descriptor)


class UserMutex:
    def __init__(self):
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            raise ServiceError("OrangeDesk 目前支持 Windows。")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateMutexW(None, False, "Global\\OrangeDesk-" + user_sid())
        if not self.handle:
            raise ServiceError("无法创建当前用户的后台控制锁。")
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


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
        # Windows readers briefly hold the destination without delete sharing.
        # Keep replacement atomic and bound retries to under half a second.
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 11:
                    raise
                time.sleep(min(.01 * (attempt + 1), .05))
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def tail_log(path: Path, lines: int = 20) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 65536))
            text = stream.read().decode("utf-8-sig", errors="replace")
        result = text.splitlines()
        if size > 65536:
            result = result[1:]
        return result[-max(1, min(int(lines), 200)):]
    except OSError:
        return []


def recent_destinations(lines: list[str], limit: int = 60) -> list[dict]:
    """Return only destination hosts, never URL paths, headers or credentials."""
    result, seen = [], set()
    for line in reversed(lines[-300:]):
        match = re.search(r"\b(DIRECT|VPN)\s+([^\s]+)(?:\s|$)", str(line)[:4096])
        failure = re.search(r"\bERROR\s+([^\s]+)\s+mode=(DIRECT|VPN)\b", str(line)[:4096])
        if not match and not failure:
            continue
        try:
            host = normalize_target(failure.group(1) if failure else match.group(2))
        except WhitelistError:
            continue
        if host in seen:
            continue
        seen.add(host)
        result.append({"host": host, "mode": (failure.group(2) if failure else match.group(1)).lower(),
                       "whitelist": "WHITELIST=" in line, "failed": failure is not None})
        if len(result) >= limit:
            break
    return result


def vendor_shortcut() -> Path | None:
    roots = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs"]
    for root in roots:
        expected = root / "FastOrange/FastOrange.appref-ms"
        if expected.is_file():
            return expected
        if root.is_dir():
            for candidate in root.glob("**/FastOrange.appref-ms"):
                if candidate.is_file():
                    return candidate
    return None


def vpn_running() -> bool:
    if os.name != "nt":
        return False
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.name.casefold() == "fastorange.exe":
                return True
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


class Coordinator:
    def __init__(self, resources: Path | None = None, root: Path | None = None):
        self.resources = resources or resource_root()
        self.root = root or service_root()
        self.whitelist_store = WhitelistStore(self.root, writer=atomic_json, secure=secure_directory)
        self._lock = threading.RLock()
        self._operations = threading.Lock()
        self._worker = None
        self._output = None
        self._session = None
        self._start_thread = None
        self._recovery = None
        self._start_deadline = 0.0
        self._stop_requested = threading.Event()
        self._shutdown_requested = False
        self._stop_timeout = False
        self._state = "stopped"
        self._message = "准备就绪。点击启动后才会启用保活与分流。"
        self._last_rpc = time.monotonic()
        self._diagnostics = NetworkDiagnostics(
            self.root / "diagnostics", self.resources,
            str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            lambda *args, **kwargs: hidden_spawn(*args, **kwargs))
        self._claude_protection = ClaudeProtection(
            self.root, self.resources,
            str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"), user_sid)
        self._wechat_direct = WeChatDirect(
            self.root, self.resources,
            str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"), user_sid)
        self._cached = self._base_status()

    def _base_status(self) -> dict:
        return dict(state="stopped", connected=False, vpn_running=False, paused=False,
                    version=APP_VERSION, direct=0, vpn=0, errors=0, active=0,
                    physical="", dns="DoH", message=self._message, logs=[],
                    vendor_installed=False, session_path="", proxy_restored=None,
                    whitelist_applied_revision=None, whitelist_count=0,
                    diagnostics=self._diagnostics.status(), claude_protection=self._claude_protection.status(),
                    wechat_direct=self._wechat_direct.status())

    def status(self) -> dict:
        with self._lock:
            self._last_rpc = time.monotonic()
            return dict(self._cached, state=self._state, message=self._message)

    def _set(self, state: str, message: str) -> None:
        with self._lock:
            self._state, self._message = state, message

    def refresh(self) -> None:
        # Files and process enumeration are read outside the status lock.
        self._claude_protection.refresh()
        protection = self._claude_protection.status()
        self._wechat_direct.refresh()
        wechat_direct = self._wechat_direct.status()
        running = vpn_running()
        installed = vendor_shortcut() is not None
        with self._lock:
            session, worker, recovery = self._session, self._worker, self._recovery
        # Evidence collection owns a separate local process, so a disconnected
        # VPN or a closed UI cannot interrupt its files. A normal companion stop
        # requests its graceful exit; a failed collector never stops the proxy.
        if worker is not None and worker.poll() is not None:
            self._diagnostics.stop()
        diagnostics = self._diagnostics.status()
        if worker is not None and worker.poll() is None and not diagnostics["active"] and diagnostics["state"] == "stopped":
            record = read_json(Path(diagnostics["session_path"]) / "status.json") if diagnostics["session_path"] else {}
            if record.get("State") == "DurationComplete" and session is not None:
                diagnostics = self._diagnostics.start(session)
        worker_status = read_json(session / "worker-status.json") if session else {}
        split = read_json(session / "split-status.json") if session else {}
        ui = read_json(session / "ui-response.json") if session else {}
        logs = tail_log(session / "monitor.log") if session else []
        paused = bool(session and (session / "pause").is_file())
        ui_value = ui.get("Value") or {}
        if not isinstance(ui_value, dict):
            ui_value = {}
        try:
            ui_fresh = session is not None and time.time() - (session / "ui-response.json").stat().st_mtime < 20
        except OSError:
            ui_fresh = False
        with self._lock:
            self._cached.update(vpn_running=running, vendor_installed=installed, diagnostics=diagnostics, claude_protection=protection,
                                wechat_direct=wechat_direct,
                                paused=paused, session_path=str(session or ""), logs=logs,
                                connected=bool(worker is not None and worker.poll() is None and running and split.get("VpnReady") and ui_fresh and ui_value.get("State") == "Connected" and not ui.get("Error")),
                                physical=str(split.get("Physical") or ""))
            self._cached.update(whitelist_applied_revision=split.get("WhitelistRevision"),
                                whitelist_count=int(split.get("WhitelistCount") or 0))
            for key, source in (("direct", "Direct"), ("vpn", "Vpn"), ("errors", "Errors"), ("active", "Active")):
                self._cached[key] = int(split.get(source) or 0)
            if recovery is not None and recovery.poll() is not None:
                recovery.communicate()
                self._recovery = None
                if recovery.returncode == 0:
                    self._state = "stopped"
                    self._message = "恢复检查完成；本工具遗留的代理设置已清理。"
                    self._cached["proxy_restored"] = True
                else:
                    self._state = "error"
                    self._message = "恢复未完成；请先关闭仍在运行的旧工具，再执行恢复。"
            if worker is None:
                return
            exited = worker.poll() is not None
            if exited:
                ok = worker_status.get("State") == "Stopped" and int(worker_status.get("Result", 1)) == 0 and not worker_status.get("Error")
                self._cached["connected"] = False
                self._cached["active"] = 0
                self._cached["proxy_restored"] = bool(worker_status.get("ProxyRestored")) if worker_status else None
                if self._output:
                    self._output.close()
                    self._output = None
                self._worker = None
                if ok:
                    restored = bool(worker_status.get("ProxyRestored"))
                    self._state = "stopped"
                    self._message = "已停止，启动前的代理设置已恢复；快橙继续保持原状态。" if restored else "已停止；快橙继续保持原状态，外部修改的网络设置已保留。"
                else:
                    self._state = "error"
                    self._message = str(worker_status.get("Error") or self._failure_message(logs) or "后台未能正常结束，请保留日志并使用恢复功能。")
            elif worker_status.get("State") == "Running" and self._state == "starting":
                self._state = "running"
                self._message = "保活与分流已启用。" if not paused else "分流已启用，自动重连已暂停。"
            elif self._state == "starting" and self._start_deadline and time.monotonic() > self._start_deadline:
                (session / "stop").write_text("stop", encoding="utf-8")
                self._state = "error"
                self._message = "后台准备超时，已请求正常停止；尚未确认代理已恢复，请查看日志或重试停止。"

    @staticmethod
    def _failure_message(logs: list[str]) -> str:
        for line in reversed(logs):
            if "工具停止：" in line or "恢复网络设置遇到错误" in line:
                return line.partition("工具停止：")[2] or line
        return ""

    def start(self, paused: bool = False) -> dict:
        with self._lock:
            if self._shutdown_requested:
                raise ServiceError("本地后台正在退出，请稍后重试。")
            if self._state in ("starting", "running"):
                return dict(self.status(), started=False)
            if self._worker is not None or self._recovery is not None or self._state == "stopping":
                raise ServiceError("上一轮后台仍在结束，请先等待停止完成。")
            self._stop_requested.clear()
            self._stop_timeout = False
            self._session = None
            self._start_deadline = 0.0
            self._cached = self._base_status()
            self._state, self._message = "starting", "正在打开快橙并准备临时分流…"
            self._start_thread = threading.Thread(target=self._start_job, args=(paused,), name="OrangeDesk-start", daemon=True)
            self._start_thread.start()
            return dict(self.status(), started=True)

    def _start_job(self, paused: bool) -> None:
        with self._operations:
            try:
                runtime = self.resources / "runtime.ps1"
                bundle = self.resources / "快橙一键启动.cmd"
                if not runtime.is_file() or not bundle.is_file():
                    raise ServiceError("安装文件不完整，请重新安装 OrangeDesk。")
                identity = process_identity(os.getpid())
                if not identity:
                    raise ServiceError("无法确认后台控制进程身份，未修改代理设置。")
                if self._stop_requested.is_set():
                    self._set("stopped", "启动已取消，未修改代理设置。")
                    return
                # ClickOnce activation can service/restart an existing vendor
                # instance. Starting a companion must reuse the live instance.
                if not vpn_running():
                    self.open_vpn()
                if self._stop_requested.is_set():
                    self._set("stopped", "启动已取消，未修改代理设置。")
                    return
                sessions = self.root / "network/sessions"
                session = sessions / uuid.uuid4().hex
                session.mkdir(parents=True)
                atomic_json(session / "bundle.json", {"Path": str(bundle)})
                if paused:
                    (session / "pause").write_text("pause", encoding="utf-8")
                self._diagnostics.start(session)
                output = (session / "desktop-worker.log").open("ab", buffering=0)
                args = [powershell_path(), "-NoLogo", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(runtime),
                        "-Role", "Worker", "-SessionPath", str(session), "-ControllerId", str(os.getpid()), "-ControllerTicks", str(identity[0]),
                        "-WhitelistPath", str(self.root / "whitelist.json"), "-StateRoot", str(self.root)]
                try:
                    worker = hidden_spawn(args, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                except Exception:
                    output.close()
                    raise
                with self._lock:
                    self._session, self._worker, self._output = session, worker, output
                    self._start_deadline = time.monotonic() + 45
                worker_identity = process_identity(worker.pid)
                atomic_json(self.root / "last-session.json", {"session_path": str(session), "worker_pid": worker.pid,
                            "worker_ticks": worker_identity[0] if worker_identity else 0, "controller_pid": os.getpid(), "controller_ticks": identity[0]})
                if self._stop_requested.is_set():
                    (session / "stop").write_text("stop", encoding="utf-8")
            except Exception as exc:
                self._diagnostics.stop()
                self._set("error", str(exc))

    def stop(self, timeout: float = 15) -> dict:
        self._stop_requested.set()
        with self._operations:
            with self._lock:
                worker, session = self._worker, self._session
            if worker is None:
                self._diagnostics.stop()
                with self._lock:
                    self._cached["diagnostics"] = self._diagnostics.status()
                self._set("stopped", "当前没有本程序启动的保活或分流。")
                return self.status()
            self._set("stopping", "正在停止保活并恢复启动前的代理设置…")
            (session / "stop").write_text("stop", encoding="utf-8")
            try:
                worker.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop_timeout = True
                self._set("error", "后台停止超时，尚未确认代理已恢复。恢复记录已保留，请查看日志后重试停止。")
                raise ServiceError(self._message)
            self.refresh()
            result = self.status()
            if result["state"] != "stopped":
                raise ServiceError(result["message"])
            return result

    def set_paused(self, paused: bool) -> dict:
        with self._operations:
            with self._lock:
                worker, session = self._worker, self._session
            if worker is None or worker.poll() is not None:
                raise ServiceError("请先启动保活与分流。")
            sentinel = session / "pause"
            if paused:
                sentinel.write_text("pause", encoding="utf-8")
            else:
                sentinel.unlink(missing_ok=True)
            with self._lock:
                self._cached["paused"] = paused
                self._message = "自动重连已暂停，国内分流继续运行。" if paused else "自动重连已恢复，国内分流继续运行。"
            return self.status()

    def open_vpn(self, for_embed: bool = False) -> dict:
        # Embedding an existing client must not trigger ClickOnce servicing.
        if for_embed and vpn_running():
            return self.status()
        shortcut = vendor_shortcut()
        if shortcut is None:
            if vpn_running():
                return self.status()
            raise ServiceError("还未安装快橙。请点击「安装快橙」，完成安装并登录后再启动。")
        # Shell activation preserves ClickOnce servicing and never executes a raw cache binary.
        os.startfile(str(shortcut))
        return self.status()

    def install_vpn(self) -> dict:
        setup = self.resources / "vendor/FastOrangeSetup.exe"
        if not setup.is_file():
            raise ServiceError("未找到随附的快橙官方安装程序，请修复 OrangeDesk 安装。")
        os.startfile(str(setup))
        with self._lock:
            self._message = "已打开快橙官方安装程序；请完成安装并登录。"
        return self.status()

    def restore(self) -> dict:
        with self._operations:
            with self._lock:
                if self._worker is not None or self._recovery is not None or self._state in ("starting", "stopping"):
                    raise ServiceError("请先停止本程序的保活与分流，再执行恢复。")
            runtime = self.resources / "runtime.ps1"
            if not runtime.is_file():
                raise ServiceError("恢复文件缺失，请修复 OrangeDesk 安装。")
            # This is the same packaged recovery routine as CMD --restore. It refuses
            # another live owner, and restores only the PAC profile owned by this tool.
            process = hidden_spawn([powershell_path(), "-NoLogo", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(runtime), "-Role", "Restore", "-StateRoot", str(self.root)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                output, _ = process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                with self._lock:
                    self._recovery = process
                self._set("error", "恢复操作仍在执行，暂时无法确认结果。请保留日志后再查看状态。")
                # No forced termination during a registry restoration.
                raise ServiceError(self._message)
            # Runtime errors can be emitted using the Windows console code page.
            import locale
            text = output.decode(locale.getpreferredencoding(False), errors="replace").strip()
            if process.returncode:
                raise ServiceError(text or "恢复未完成；请先关闭仍在运行的旧工具。")
            self._set("stopped", "恢复检查完成；本工具遗留的代理设置已清理。")
            with self._lock:
                self._cached["proxy_restored"] = True
            return self.status()

    def _whitelist_session(self) -> Path | None:
        with self._lock:
            if self._worker is not None and self._worker.poll() is None:
                return self._session
        # The original CMD and GUI share the same rules. Recognize a live CMD
        # for display only; this never changes or takes ownership of its session.
        if os.name != "nt":
            return None
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                url = winreg.QueryValueEx(key, "AutoConfigURL")[0]
            match = re.fullmatch(r"http://127\.0\.0\.1:[0-9]+/fastorange/([a-f0-9]{32})/proxy\.pac", url)
            if not match:
                return None
            session = self.root / "network/sessions" / match.group(1)
            status = read_json(session / "worker-status.json")
            identity = process_identity(int(status.get("ProcessId", 0)))
            if status.get("State") == "Running" and identity and os.path.normcase(identity[1]) == os.path.normcase(powershell_path()):
                return session
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def whitelist(self, action: str, payload: dict) -> dict:
        try:
            if action == "whitelist-add":
                doc = self.whitelist_store.add_many(payload.get("text", ""), payload.get("mode", "direct"), payload.get("subdomains", True))
                message = "白名单已保存。新连接将按指定路线访问。"
            elif action == "whitelist-remove":
                doc = self.whitelist_store.remove(payload.get("host", ""), payload.get("subdomains", True))
                message = "白名单已移除，新连接恢复自动分流。"
            else:
                doc = self.whitelist_store.snapshot()
                message = ""
        except (WhitelistError, OSError) as exc:
            raise ServiceError(str(exc)) from exc
        session = self._whitelist_session()
        applied = read_json(session / "split-status.json") if session else {}
        lines = tail_log(session / "monitor.log", 200) if session else []
        return dict(doc, message=message, active=session is not None,
                    applied_revision=applied.get("WhitelistRevision"), recent=recent_destinations(lines))

    def dispatch(self, action: str, payload: dict | None = None) -> dict:
        action = {"open_vpn": "open-vpn", "install_vpn": "install-vpn"}.get(action, action)
        payload = payload or {}
        with self._lock:
            self._last_rpc = time.monotonic()
            if self._shutdown_requested and action not in ("status", "logs"):
                raise ServiceError("本地后台正在退出，请稍后重试。")
        if action == "status":
            return self.status()
        if action in ("wechat-direct-enable", "wechat-direct-disable"):
            try:
                result = self._wechat_direct.change(action == "wechat-direct-enable")
                with self._lock:
                    self._cached["wechat_direct"] = result
                return {"wechat_direct": result, "message": result.get("message", "")}
            except WeChatDirectError as exc:
                raise ServiceError(str(exc)) from exc
        if action in ("claude-protection-enable", "claude-protection-disable"):
            enabled = action == "claude-protection-enable"
            with self._lock:
                session, worker = self._session, self._worker
                if enabled and (self._state != "running" or session is None or worker is None or worker.poll() is not None or self._stop_requested.is_set()):
                    raise ServiceError("请先启动伴航并连接新加坡节点，再开启 Claude 出口保护。")
            try:
                result = self._claude_protection.change(enabled, session if enabled else None, payload.get("singapore_confirmed") is True)
                with self._lock:
                    self._cached["claude_protection"] = result
                return {"claude_protection": result, "message": result.get("message", "")}
            except ProtectionError as exc:
                raise ServiceError(str(exc)) from exc
        if action == "diagnostics-status":
            return self._diagnostics.status()
        if action == "diagnostics-deep-start":
            result = self._diagnostics.start_deep()
            if not result.get("ok"):
                raise ServiceError(result.get("message", "深度记录未能启动。"))
            return result
        if action == "diagnostics-export":
            destination = payload.get("path")
            if not isinstance(destination, str) or not destination.strip() or not Path(destination).is_absolute():
                raise ServiceError("请选择诊断ZIP的完整保存路径。")
            with self._lock:
                session = self._session
            result = self._diagnostics.export(Path(destination), session)
            if not result.get("ok"):
                raise ServiceError(result.get("message", "诊断日志导出失败。"))
            return result
        if action in ("whitelist-list", "whitelist-add", "whitelist-remove"):
            return self.whitelist(action, payload)
        if action == "start":
            return self.start(bool(payload.get("paused", False)))
        if action in ("stop", "shutdown"):
            with self._lock:
                if action == "shutdown" and self._recovery is not None:
                    raise ServiceError("恢复操作仍在执行，请等待完成后再退出后台。")
                if action == "shutdown":
                    self._shutdown_requested = True
            try:
                return self.stop()
            except Exception:
                if action == "shutdown":
                    with self._lock:
                        self._shutdown_requested = False
                raise
        if action in ("pause", "resume"):
            return self.set_paused(action == "pause")
        if action == "logs":
            with self._lock:
                session = self._session
            lines = max(1, min(int(payload.get("lines", 40)), 200))
            return {"logs": tail_log(session / "monitor.log", lines) if session else [], "session_path": str(session or "")}
        if action in ("open-vpn", "install-vpn"):
            with self._operations:
                return self.open_vpn(for_embed=bool(payload.get("embed", False))) if action == "open-vpn" else self.install_vpn()
        if action == "restore":
            return self.restore()
        raise ServiceError("不支持的操作。")

    def idle(self) -> bool:
        with self._lock:
            return self._worker is None and self._recovery is None and self._state not in ("starting", "stopping") and time.monotonic() - self._last_rpc > IDLE_SECONDS


class RpcServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, coordinator: Coordinator, token: str):
        self.coordinator, self.token = coordinator, token
        super().__init__(("127.0.0.1", 0), RpcHandler)


class RpcHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, fmt, *args):
        pass  # Never log authorization tokens or RPC bodies.

    def _reply(self, code: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_POST(self):
        self.connection.settimeout(5)
        expected_host = f"127.0.0.1:{self.server.server_address[1]}"
        expected_token = "Bearer " + self.server.token
        if self.path != "/rpc" or self.headers.get("Host") != expected_host or not hmac.compare_digest(self.headers.get("Authorization", ""), expected_token):
            self._reply(403, {"ok": False, "error": "未授权的本地请求。"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384 or self.headers.get("Transfer-Encoding"):
                raise ServiceError("请求格式错误。")
            request = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(request, dict) or not isinstance(request.get("action"), str) or not isinstance(request.get("payload", {}), dict):
                raise ServiceError("请求格式错误。")
            result = self.server.coordinator.dispatch(request["action"], request.get("payload"))
            self._reply(200, {"ok": True, "result": result})
            if request["action"] == "shutdown":
                # Send the successful restoration result before terminating the
                # loop. shutdown() must be called outside serve_forever's thread.
                self.wfile.flush()
                threading.Thread(target=self.server.shutdown, name="OrangeDesk-shutdown", daemon=True).start()
        except (ServiceError, ValueError, OSError) as exc:
            self._reply(400, {"ok": False, "error": str(exc)})
        except Exception:
            self._reply(500, {"ok": False, "error": "操作未完成，请查看本次日志。"})


def serve() -> int:
    mutex = UserMutex()
    if not mutex.acquire():
        return 0
    root = service_root()
    server = None
    endpoint = root / "service.json"
    token = secrets.token_urlsafe(32)
    coordinator = Coordinator(root=root)
    done = threading.Event()
    try:
        secure_directory(root)
        initialize_storage()
        identity = process_identity(os.getpid())
        if not identity:
            raise ServiceError("无法确认后台进程身份。")
        coordinator.refresh()
        server = RpcServer(coordinator, token)
        atomic_json(endpoint, {"pid": os.getpid(), "ticks": identity[0], "executable": identity[1],
                              "port": server.server_address[1], "token": token, "version": APP_VERSION})

        def monitor():
            while not done.wait(1):
                try:
                    coordinator.refresh()
                    if coordinator.idle():
                        server.shutdown()
                        return
                except Exception:
                    # A malformed transient status file must not kill a proxy owner.
                    continue

        thread = threading.Thread(target=monitor, name="OrangeDesk-status", daemon=True)
        thread.start()
        server.serve_forever(poll_interval=0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    except ServiceError:
        return 1
    finally:
        done.set()
        try:
            coordinator.stop()
        except Exception:
            # Killing this controller still triggers the runtime's own lease watcher.
            pass
        if server:
            server.server_close()
        saved = read_json(endpoint)
        if saved.get("pid") == os.getpid() and saved.get("token") == token:
            endpoint.unlink(missing_ok=True)
        mutex.close()
