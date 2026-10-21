"""Explicit, per-program Windows network protection; never starts Claude."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit


class ProtectionError(RuntimeError):
    pass


def _read(path: Path) -> dict:
    if path.stat().st_size > 65536:
        raise ValueError("protection state is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid protection state")
    return value


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ClaudeProtection:
    def __init__(self, root: Path, resources: Path, powershell: str, owner_sid,
                 run=None, elevate=None, interface_guid=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "claude-protection"
        self.resources = Path(resources)
        self.powershell = powershell
        self.owner_sid = owner_sid
        self.run = run or subprocess.run
        self.elevate = elevate or self._elevate
        self.interface_guid = interface_guid or self._interface_guid
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._query_active = False
        self._last_query = 0.0
        self._pending = None
        self._cache = dict(enabled=False, state="disabled", apps=[], interface_name="",
                           message="仅限制已识别的原生 Claude 程序，不会启动 Claude。")
        try:
            self._cache.update(_read(self.directory / "status.json"))
            # Saved JSON is a lookup hint, not proof that Windows still has
            # the filters. Query the actual objects before showing success.
            self._cache.update(state="checking", message="正在向 Windows 核验已保存的出口保护规则。")
            request = _read(self.directory / "last-request.json")
            if (request.get("state") == "pending"
                    and re.fullmatch(r"[a-f0-9-]{36}", str(request.get("request_id", "")))
                    and type(request.get("started_at")) in (int, float)
                    and not (self.directory / ("request-" + request["request_id"] + ".result.json")).is_file()):
                self._pending = request
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _elevate(command: list[str]) -> int:
        if os.name != "nt":
            raise ProtectionError("Claude 出口保护需要 Windows。")
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        return int(shell.ShellExecuteW(None, "runas", command[0],
                                      subprocess.list2cmdline(command[1:]), None, 0) or 0)

    @staticmethod
    def _interface_guid(index: int) -> str:
        native = ctypes.WinDLL("iphlpapi", use_last_error=True)
        native.ConvertInterfaceIndexToLuid.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)]
        native.ConvertInterfaceLuidToGuid.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p]
        luid = ctypes.c_uint64()
        guid = (ctypes.c_ubyte * 16)()
        if native.ConvertInterfaceIndexToLuid(index, ctypes.byref(luid)) or not luid.value:
            raise ProtectionError("无法确认当前 VPN 网卡身份。")
        if native.ConvertInterfaceLuidToGuid(ctypes.byref(luid), ctypes.byref(guid)):
            raise ProtectionError("无法确认当前 VPN 网卡身份。")
        return str(uuid.UUID(bytes_le=bytes(guid)))

    def _sid(self) -> str:
        sid = self.owner_sid() if callable(self.owner_sid) else self.owner_sid
        if not isinstance(sid, str) or re.fullmatch(r"S-1-(?:\d+-)*\d+", sid) is None:
            raise ProtectionError("无法确认当前 Windows 用户。")
        return sid

    def _scripts(self) -> Path:
        names = ("Set-ClaudeProtection.ps1", "ClaudeNetworkGuard.cs")
        try:
            contents = {name: (self.resources / name).read_bytes() for name in names}
            if any(not data for data in contents.values()):
                raise ValueError("empty helper")
            digest = hashlib.sha256(b"".join(contents[name] for name in names)).hexdigest()
            folder = self.directory / "helpers" / digest
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in contents.items():
                target = folder / name
                if not target.is_file() or target.read_bytes() != data:
                    temporary = folder / (name + "." + uuid.uuid4().hex + ".tmp")
                    temporary.write_bytes(data)
                    os.replace(temporary, target)
            return folder / names[0]
        except (OSError, ValueError) as exc:
            raise ProtectionError("出口保护组件缺失或无法保存，请修复伴航安装。") from exc

    def _proof(self, session: Path) -> dict:
        session = Path(session).resolve()
        if session.parent != self.root / "network/sessions" or re.fullmatch("[a-f0-9]{32}", session.name) is None:
            raise ProtectionError("请先启动本窗口的伴航会话。")
        try:
            status_path = session / "split-status.json"
            if time.time() - status_path.stat().st_mtime > 30:
                raise ValueError("stale proxy state")
            state = _read(status_path)
            if state.get("StrictVpnBinding") is not True or state.get("PacDirectFallback") is not False:
                raise ValueError("old proxy")
            pac = urlsplit(_read(session / "proxy-before.json")["PacUrl"])
            if (pac.scheme != "http" or pac.hostname != "127.0.0.1" or not pac.port
                    or pac.username is not None or pac.password is not None or pac.query or pac.fragment
                    or pac.path != f"/fastorange/{session.name}/proxy.pac"):
                raise ValueError("invalid local proxy")
            connection = http.client.HTTPConnection("127.0.0.1", pac.port, timeout=2)
            try:
                connection.request("GET", pac.path.replace("/proxy.pac", "/vpn-status.json"))
                response = connection.getresponse()
                raw = response.read(4097)
                if response.status != 200 or len(raw) > 4096:
                    raise ValueError("invalid local health response")
                proof = json.loads(raw)
            finally:
                connection.close()
            if (proof.get("vpnBindingEnforced") is not True or proof.get("pacDirectFallback") is not False
                    or proof.get("vpnReady") is not True or proof.get("claudeRouteIsVpn") is not True
                    or type(proof.get("vpnInterfaceIndex")) is not int or proof["vpnInterfaceIndex"] <= 0
                    or not isinstance(ipaddress.ip_address(proof["vpnAddress"]), ipaddress.IPv4Address)):
                raise ValueError("VPN is not ready")
            return proof
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as exc:
            raise ProtectionError("当前 VPN 未就绪或伴航出口检查未通过，现有保护规则保持原状。") from exc

    @staticmethod
    def _native_apps() -> list[str]:
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
        candidates = [home / ".local/bin/claude.exe",
                      appdata / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
                      Path(os.environ.get("SystemDrive", "C:") + "/tmp/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe")]
        found = shutil.which("claude")
        if found:
            path = Path(found).absolute()
            candidates += [path, path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"]
        apps = []
        for path in candidates:
            if path.name.lower() != "claude.exe" or not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    if stream.read(2) != b"MZ":
                        continue
                value = str(path.absolute())
                if value.casefold() not in {item.casefold() for item in apps}:
                    apps.append(value)
            except OSError:
                continue
        if not apps:
            raise ProtectionError("未找到可保护的原生 claude.exe。当前开关不覆盖 Node 脚本版 CLI。")
        return apps

    def _command(self, script: Path, action: str) -> list[str]:
        return [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-Action", action, "-StateDir", str(self.directory), "-OwnerSid", self._sid()]

    def change(self, enabled: bool, session: Path | None = None, singapore_confirmed: bool = False) -> dict:
        if not self._operation.acquire(blocking=False):
            raise ProtectionError("出口保护正在处理中，请等待本次操作完成。")
        try:
            self.status()
            with self._lock:
                if self._pending:
                    raise ProtectionError("请先完成或取消 Windows 管理员授权。")
            if enabled and singapore_confirmed is not True:
                raise ProtectionError("请先在快橙中确认当前选择的是新加坡节点。")
            proof = self._proof(session) if enabled and session is not None else None
            if enabled and proof is None:
                raise ProtectionError("请先启动伴航并连接指定的新加坡节点。")
            apps = self._native_apps() if enabled else []
            script = self._scripts()
            request_id = str(uuid.uuid4())
            command = self._command(script, "Enable" if enabled else "Disable") + ["-RequestId", request_id]
            if enabled:
                paths = self.directory / ("request-" + request_id + ".apps.json")
                _write(paths, apps)
                command += ["-VpnGuid", self.interface_guid(proof["vpnInterfaceIndex"]), "-AppPathsJsonPath", str(paths)]
            with self._lock:
                previous = dict(self._cache)
                self._pending = dict(request_id=request_id, started_at=time.time(), state="pending")
                _write(self.directory / "last-request.json", self._pending)
                self._cache.update(state="pending", message="等待 Windows 授权及规则核验；尚未将本次请求标记为成功。")
            try:
                result = self.elevate(command)
                if result <= 32:
                    raise ProtectionError("Windows 管理员授权已取消或未能启动，现有保护规则保持原状。")
            except (OSError, ProtectionError):
                with self._lock:
                    if self._pending and self._pending["request_id"] == request_id:
                        self._pending = None
                        self._cache = previous
                        intent = _read(self.directory / "last-request.json")
                        if intent.get("request_id") == request_id:
                            _write(self.directory / "last-request.json", {"state": "complete", "request_id": request_id})
                raise
            return self.status()
        finally:
            self._operation.release()

    def status(self) -> dict:
        with self._lock:
            if self._pending:
                try:
                    result = _read(self.directory / ("request-" + self._pending["request_id"] + ".result.json"))
                    if result.get("request_id") != self._pending["request_id"] or result.get("owner_sid") != self._sid():
                        raise ValueError("mismatched protection receipt")
                    self._cache.update(result)
                    if result.get("state") == "failed":
                        self._cache["state"] = "error"
                    self._pending = None
                    # The per-request receipt records completion. Do not
                    # overwrite the shared latest intent here: a recovery CLI
                    # may already have submitted a newer request in parallel.
                except FileNotFoundError:
                    if time.time() - self._pending["started_at"] > 120:
                        self._cache.update(state="error", message="授权或规则核验超过两分钟未完成，保护状态尚未确认。")
                        # A later explicit request supersedes this request ID.
                        # Helpers check that ID under a per-owner mutation lock,
                        # preventing a delayed Enable from undoing a recovery.
                        self._pending = None
                except (OSError, ValueError, TypeError, ProtectionError) as exc:
                    self._cache.update(state="error", message="无法核对出口保护结果：" + str(exc))
            return dict(self._cache)

    def refresh(self) -> None:
        self.status()
        with self._lock:
            if self._pending or self._query_active or time.monotonic() - self._last_query < 30:
                return
            self._query_active = True
            self._last_query = time.monotonic()
        threading.Thread(target=self._query, name="OrangeDesk-ClaudeProtection", daemon=True).start()

    def _query(self) -> None:
        try:
            result = self.run(self._command(self._scripts(), "Status"), capture_output=True, encoding="utf-8-sig",
                              errors="replace", timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            value = json.loads(result.stdout)
            if result.returncode or value.get("owner_sid") != self._sid():
                raise ValueError("规则读取未获确认")
            with self._lock:
                if self._pending is None:
                    self._cache.update(value)
                    if value.get("state") == "failed":
                        self._cache["state"] = "error"
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, ProtectionError):
            with self._lock:
                if self._pending is None:
                    self._cache.update(state="error", message="无法读取系统规则；上次记录仍保留，请勿据此假定保护已生效。")
        finally:
            with self._lock:
                self._query_active = False
