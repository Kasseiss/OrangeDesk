"""Explicit WeChat direct-routing setup; importing or checking never elevates."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


class WeChatDirectError(RuntimeError):
    pass


def _read(path: Path) -> dict:
    if path.stat().st_size > 65536:
        raise ValueError("WeChat direct state is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid WeChat direct state")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class WeChatDirect:
    PENDING_SECONDS = 600
    REFRESH_SECONDS = 15

    def __init__(self, root: Path, resources: Path, powershell: str, owner_sid,
                 run=None, elevate=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "wechat-direct"
        self.resources = Path(resources)
        self.powershell = powershell
        self.owner_sid = owner_sid
        self.run = run or subprocess.run
        self.elevate = elevate or self._elevate
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._query_active = False
        self._last_query = float("-inf")
        self._generation = 0
        self._pending = None
        desired = True
        try:
            preference = _read(self.directory / "preferences.json")
            if preference.get("owner_sid") == self._sid() and type(preference.get("desired_enabled")) is bool:
                desired = preference["desired_enabled"]
        except (OSError, ValueError, TypeError, WeChatDirectError):
            pass
        self._cache = dict(state="needs-setup" if desired else "checking", enabled=False,
                           desired_enabled=desired, owner_sid="", request_id="",
                           interface_name="", message="微信直连默认开启，尚需安装并核验应用分流组件。"
                           if desired else "正在核验已保存的微信直连状态。")
        try:
            # Saved status is evidence that a query is needed, never live proof.
            saved = _read(self.directory / "status.json")
            if saved.get("owner_sid") == self._sid():
                self._cache.update(state="checking", message="正在核验应用分流服务及本地监听。")
        except (OSError, ValueError, TypeError, WeChatDirectError):
            pass
        try:
            request = _read(self.directory / "last-request.json")
            if (request.get("state") == "pending" and self._valid_request(request)
                    and 0 <= time.time() - request["started_at"] < self.PENDING_SECONDS):
                self._pending = request
                self._cache.update(state="pending", request_id=request["request_id"],
                                   message="正在等待微信直连安装及管理员操作结果。")
        except (OSError, ValueError, TypeError, WeChatDirectError):
            pass

    def _sid(self) -> str:
        sid = self.owner_sid() if callable(self.owner_sid) else self.owner_sid
        if not isinstance(sid, str) or re.fullmatch(r"S-1-(?:\d+-)*\d+", sid) is None:
            raise WeChatDirectError("无法确认当前 Windows 用户。")
        return sid

    def _valid_request(self, value: dict) -> bool:
        try:
            request_id = value["request_id"]
            return (str(uuid.UUID(request_id)) == request_id
                    and value.get("owner_sid") == self._sid()
                    and type(value.get("desired_enabled")) is bool
                    and type(value.get("started_at")) in (int, float)
                    and math.isfinite(value["started_at"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _elevate(command: list[str]) -> int:
        if os.name != "nt":
            raise WeChatDirectError("微信应用分流需要 Windows。")
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        return int(shell.ShellExecuteW(None, "runas", command[0],
                                      subprocess.list2cmdline(command[1:]), None, 0) or 0)

    def _script(self) -> Path:
        try:
            names = ("Set-WeChatDirect.ps1", "WeChatDirectRelay.cs")
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
                    try:
                        temporary.write_bytes(data)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            return folder / names[0]
        except (OSError, ValueError) as exc:
            raise WeChatDirectError("微信直连组件缺失或无法保存，请修复伴航安装。") from exc

    def _command(self, action: str) -> list[str]:
        return [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(self._script()), "-Action", action, "-StateDir", str(self.directory),
                "-OwnerSid", self._sid()]

    def change(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise WeChatDirectError("微信直连开关值无效。")
        if not self._operation.acquire(blocking=False):
            raise WeChatDirectError("微信直连正在处理中，请等待本次操作完成。")
        try:
            self.status()
            with self._lock:
                if self._pending:
                    raise WeChatDirectError("请先完成或取消当前管理员操作，首次安装可能需要几分钟。")
                command = self._command("Enable" if enabled else "Disable")
                request_id = str(uuid.uuid4())
                request = dict(state="pending", request_id=request_id, started_at=time.time(),
                               owner_sid=self._sid(), desired_enabled=enabled)
                old = dict(self._cache)
                # The elevated helper validates this ID before any mutation.
                _write(self.directory / "last-request.json", request)
                self._pending = request
                self._generation += 1
                self._cache.update(state="pending", request_id=request_id,
                                   message="等待管理员授权及组件安装，首次安装可能需要几分钟。")
            try:
                result = self.elevate(command + ["-RequestId", request_id])
                if not isinstance(result, int) or result <= 32:
                    raise WeChatDirectError("管理员授权已取消或未能启动，微信直连设置保持原状。")
            except Exception:
                with self._lock:
                    _write(self.directory / "last-request.json", dict(request, state="cancelled"))
                    self._pending = None
                    self._generation += 1
                    self._cache = old
                raise
            return self.status()
        finally:
            self._operation.release()

    def _actual(self, value: dict) -> dict:
        if (not isinstance(value, dict) or value.get("owner_sid") != self._sid()
                or type(value.get("enabled")) is not bool):
            raise ValueError("mismatched WeChat direct status")
        state = value.get("state")
        if state not in {"not-installed", "checking", "pending", "running", "disabled", "error"}:
            raise ValueError("invalid WeChat direct state")
        # A failed change can leave an independently verified old service
        # running. Keep that fact separate from operation success.
        if ((state == "running" and not value["enabled"])
                or (state not in {"running", "error"} and value["enabled"])):
            raise ValueError("inconsistent WeChat direct verification")
        return dict(state="needs-setup" if state == "not-installed" and self._cache["desired_enabled"] else state,
                    enabled=value["enabled"], owner_sid=value["owner_sid"],
                    interface_name=str(value.get("interface_name", ""))[:256],
                    message=str(value.get("message", ""))[:2000])

    def status(self) -> dict:
        with self._lock:
            if not self._pending:
                return dict(self._cache)
            request = self._pending
            try:
                receipt = _read(self.directory / ("request-" + request["request_id"] + ".result.json"))
                if receipt.get("request_id") != request["request_id"]:
                    raise ValueError("mismatched request receipt")
                if receipt.get("desired_enabled") is not request["desired_enabled"]:
                    raise ValueError("receipt does not match requested preference")
                actual = self._actual(receipt)
                success = receipt["state"] == ("running" if request["desired_enabled"] else "disabled")
                if not success:
                    actual["state"] = "error"
                    actual["message"] = actual["message"] or "微信直连操作未通过系统核验，原开关偏好已保留。"
                else:
                    _write(self.directory / "preferences.json", dict(owner_sid=self._sid(),
                           desired_enabled=request["desired_enabled"]))
                    actual["desired_enabled"] = request["desired_enabled"]
                _write(self.directory / "last-request.json", dict(request, state="complete"))
                self._cache.update(actual)
                self._pending = None
                self._generation += 1
            except FileNotFoundError:
                if time.time() - request["started_at"] >= self.PENDING_SECONDS:
                    _write(self.directory / "last-request.json", dict(request, state="expired"))
                    self._pending = None
                    self._generation += 1
                    self._cache.update(state="error", enabled=False,
                                       message="十分钟内未收到安装核验结果，可重新操作；当前是否生效尚未确认。")
                else:
                    try:
                        progress = _read(self.directory / "progress.json")
                        if (progress.get("request_id") == request["request_id"]
                                and progress.get("state") == "pending"):
                            self._cache["message"] = str(progress.get("message", "正在安装微信直连组件。"))[:2000]
                    except (OSError, ValueError, TypeError):
                        pass
            except (OSError, ValueError, TypeError, WeChatDirectError) as exc:
                self._cache.update(state="error", enabled=False, message="无法核对微信直连结果：" + str(exc))
                # A bad receipt must not permanently lock out a new request.
                _write(self.directory / "last-request.json", dict(request, state="invalid"))
                self._pending = None
                self._generation += 1
            return dict(self._cache)

    def refresh(self) -> None:
        self.status()
        with self._lock:
            if self._pending or self._query_active or time.monotonic() - self._last_query < self.REFRESH_SECONDS:
                return
            self._query_active = True
            self._last_query = time.monotonic()
            generation = self._generation
        threading.Thread(target=self._query, args=(generation,), name="OrangeDesk-WeChatDirect", daemon=True).start()

    def _query(self, generation=None) -> None:
        with self._lock:
            if generation is None:
                generation = self._generation
        try:
            result = self.run(self._command("Status"), capture_output=True, encoding="utf-8-sig",
                              errors="replace", timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ValueError("微信直连状态核验失败")
            if len(result.stdout) > 65536:
                raise ValueError("status output is too large")
            value = json.loads(result.stdout)
            with self._lock:
                if self._pending is None and generation == self._generation:
                    self._cache.update(self._actual(value))
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, WeChatDirectError):
            with self._lock:
                if self._pending is None and generation == self._generation:
                    self._cache.update(state="error", enabled=False,
                                       message="无法核验微信直连服务及本地监听，请勿据此认为已生效。")
        finally:
            with self._lock:
                self._query_active = False
