"""Local, Qt-free network evidence lifecycle and redacted ZIP export.

Constructing this object never starts a process. Only ``start`` starts ordinary
collection; only an explicit ``start_deep`` requests administrator elevation.
No application credentials, VPN configurations or Claude content are read.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile


KIND = "orangedesk-network-diagnostics/v1"
MAX_EXPORT_BYTES = 256 * 1024 * 1024
STOP_TIMEOUT_SECONDS = 5.0
DEEP_STOP_TIMEOUT_SECONDS = 8.0
HISTORY_COUNT = 3
COMPANION_FILES = (
    "monitor.log", "split-status.json", "worker-status.json",
    "desktop-worker.log", "recovery.log", "routes.jsonl", "health.json",
)
_TEXT_FILE = re.compile(r"\.(?:json|jsonl|txt)(?:\.(?:[1-4]|previous))?$", re.I)
_FORBIDDEN_FILE = re.compile(r"config|credential|authfile|password|secret|cache|chat", re.I)
_SENSITIVE_KEY = re.compile(r"authorization|(?:^|[_-])(?:api[_-]?key|password|passwd|token|secret)(?:$|[_-])|^(?:apiKey|accessToken|refreshToken)$", re.I)
_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?(?:authorization|api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|token|secret)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)'''
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION = re.compile(r'''(?im)(\bauthorization\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n]+)''')
_URL_AUTH = re.compile(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@")
_QUERY_SECRET = re.compile(r'''(?i)([?&](?:access[_-]?token|refresh[_-]?token|api[_-]?key|token|password|secret|authorization)=)[^&#\s"']*''')
_VPN_LINE = re.compile(r"(?i)\bSTATE\s*:|>STATE:|\[(?:WARN(?:ING)?|ERROR)\]|\b(?:FATAL|disconnect(?:ed|ion)?|reconnect(?:ing)?|connection-reset|connection timed out|SIGTERM|SIGUSR1)\b|\bmanagement:\s*(?:connected|.*(?:closed|socket|сокет))")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"{path.name}: symbolic link is not permitted")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: expected JSON object")
    return value


def _redact_string(value: str) -> str:
    value = _AUTHORIZATION.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
    value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _ASSIGNMENT.sub(lambda match: match.group(1) + '"[REDACTED]"', value)


def _redact_object(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact_object(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_object(item) for item in value]
    return _redact_string(value) if isinstance(value, str) else value


def _redact_text(text: str, name: str) -> str:
    name = re.sub(r"\.(?:previous|[1-4])$", "", name, flags=re.I)
    if name.lower().endswith(".json"):
        try:
            return json.dumps(_redact_object(json.loads(text)), ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            pass
    if re.search(r"\.jsonl(?:\.(?:[1-4]|previous))?$", name, re.I):
        lines = []
        for line in text.splitlines():
            try:
                lines.append(json.dumps(_redact_object(json.loads(line)), ensure_ascii=False))
            except (ValueError, TypeError):
                lines.append(_redact_string(line))
        return "\n".join(lines) + ("\n" if lines else "")
    return _redact_string(text)


class NetworkDiagnostics:
    def __init__(self, root: Path, resources: Path, powershell: str, spawn):
        self.root = Path(root).resolve()
        self.resources = Path(resources).resolve()
        self.powershell = str(powershell)
        self.spawn = spawn
        self._lock = threading.RLock()
        self._session: Path | None = None
        self._process = None
        self._companion: Path | None = None
        self._launch_error = ""
        self._deep_error = ""
        self._deep_requested_at = ""
        self._stop_requested = False
        self._started_at = ""
        # A restarted UI can show evidence from this module's last session.
        try:
            current = _read_json(self.root / "current-session.json")
            candidate = Path(current["session_path"]).resolve()
            if candidate.parent == self.root and self._owned(candidate):
                self._session = candidate
                manifest = _read_json(candidate / "manifest.json")
                self._started_at = manifest.get("started_at_utc", "")
                companion = manifest.get("companion_session")
                self._companion = Path(companion).resolve() if companion else None
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @staticmethod
    def _owned(path: Path) -> bool:
        try:
            return not path.is_symlink() and _read_json(path / "manifest.json").get("kind") == KIND
        except (OSError, ValueError, TypeError):
            return False

    def _status_file(self, name: str) -> tuple[dict, str]:
        if self._session is None:
            return {}, ""
        try:
            return _read_json(self._session / name), ""
        except FileNotFoundError:
            return {}, ""
        except (OSError, ValueError, TypeError) as error:
            return {}, f"{name}: {error}"

    def status(self) -> dict:
        with self._lock:
            data, read_error = self._status_file("status.json")
            trace, trace_error = self._status_file("trace-status.json")
            raw = str(data.get("State", data.get("state", "")))
            state = "stopped"
            active = False
            message = self._launch_error or read_error
            running_states = {"starting", "running", "postincident", "recording"}
            if self._session:
                if raw.lower() in running_states:
                    active = True
                    state = "starting" if raw.lower() == "starting" else "running"
                elif raw.lower() == "failed":
                    state = "failed"
                elif not raw and self._process is not None and not self._launch_error:
                    state, active = "starting", True
                if self._process is not None:
                    try:
                        exit_code = self._process.poll()
                    except Exception as error:
                        exit_code = None
                        message = f"无法检查监测进程：{error}"
                    if exit_code is not None:
                        active = False
                        if not raw or raw.lower() in running_states:
                            state = "stopped" if self._stop_requested else "failed"
                            message = message or f"监测进程已退出（代码 {exit_code}），未收到正常结束状态。"
                elif active:
                    # A recovered session has no owned Popen handle. Do not
                    # present an arbitrarily old Running JSON as a live process.
                    try:
                        age = time.time() - (self._session / "status.json").stat().st_mtime
                        if age > 30:
                            state, active = "unknown", False
                            message = "历史监测状态超过30秒未更新，无法确认采集进程仍在运行。"
                    except OSError:
                        state, active = "unknown", False
                if self._launch_error:
                    state, active = "failed", False
                if active:
                    try:
                        updated = (self._session / "status.json").stat().st_mtime
                    except OSError:
                        updated = _timestamp(self._started_at) or time.time()
                    if time.time() - updated > 30:
                        state, active = "stale", False
                        message = "监测状态超过30秒未更新，采集可能已停滞；没有强制结束任何进程。"
                if raw.lower() == "durationcomplete":
                    message = "本次24小时网络监测已正常到期结束，可启动下一次记录。"
            last_error = data.get("LastError", data.get("lastError"))
            if last_error:
                message = (message + "；" if message else "") + str(last_error)
            if self._launch_error:
                state, active = "failed", False
            raw_deep = str(trace.get("State", trace.get("state", "stopped"))).lower()
            deep_state = ("active" if raw_deep in {"running", "stopping", "recording"} else
                          "starting" if raw_deep == "starting" else
                          "error" if raw_deep in {"failed", "stopfailed", "ownershiplost", "error"} else
                          "unavailable" if not (self.resources / "Trace-NetworkDiagnostics.ps1").is_file() else "stopped")
            if self._deep_requested_at and _timestamp(trace.get("UpdatedAt")) < _timestamp(self._deep_requested_at):
                deep_state = "starting"
                if time.time() - _timestamp(self._deep_requested_at) > 30:
                    deep_state = "error"
                    self._deep_error = "深度记录启动后超过30秒未返回状态，请检查管理员授权与脚本日志。"
            deep_message = trace.get("Error") or trace.get("LastError") or trace_error or self._deep_error
            if deep_message:
                message = (message + "；" if message else "") + str(deep_message)
            incident = data.get("IncidentAt", data.get("incidentAt"))
            if not incident and self._session and (self._session / "incident.json").is_file():
                incident_data, _ = self._status_file("incident.json")
                incident = incident_data.get("At") or incident_data.get("IncidentAt")
            return {"state": state, "active": active,
                    "session_path": str(self._session) if self._session else "",
                    "incident_at": incident, "deep_state": deep_state,
                    "message": message or ("本地网络监测正在运行。" if active else "网络监测未运行。")}

    def start(self, companion_session: Path) -> dict:
        with self._lock:
            requested_companion = Path(companion_session).resolve()
            current = self.status()
            transition_warning = ""
            if current["active"] and requested_companion == self._companion:
                return {"ok": True, **current}
            if current["active"] or current["state"] == "stale":
                if current["state"] == "stale" and requested_companion == self._companion:
                    return {"ok": False, **current, "message": "现有采集器状态已停滞；请先停止该记录，再重新启动。"}
                previous = self.stop()
                if not previous["ok"]:
                    transition_warning = previous["message"]
            script = self.resources / "Collect-NetworkDiagnostics.ps1"
            if not script.is_file():
                self._launch_error = "缺少网络监测脚本，请修复安装。"
                return {"ok": False, **self.status(), "state": "failed"}
            self._process = None
            self._launch_error = self._deep_error = self._deep_requested_at = ""
            self._stop_requested = False
            self._started_at = _utc()
            self._companion = requested_companion
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                session = self.root / ("session-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12])
                session.mkdir(exist_ok=False)
                self._session = session
                manifest = {"kind": KIND, "started_at_utc": self._started_at, "session_id": session.name,
                            "companion_session": str(self._companion), "computer": platform.node(),
                            "platform": platform.platform(), "python_version": platform.python_version(),
                            "app_version": getattr(sys.modules.get("app.engine"), "APP_VERSION", "unknown"),
                            "scope": "Local interface/events/process counts, companion state and filtered VPN connection logs; no public probes, packet payload, credentials or Claude content.",
                            "history_export_count": HISTORY_COUNT, "max_export_bytes": MAX_EXPORT_BYTES}
                if transition_warning:
                    manifest["previous_session_stop_warning"] = transition_warning
                _atomic_json(session / "manifest.json", manifest)
                _atomic_json(self.root / "current-session.json", {"kind": KIND, "session_path": str(session)})
                command = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                           "-SessionDir", str(session), "-DurationMinutes", "1440", "-Continuous",
                           "-CompanionSessionPath", str(self._companion)]
                with (session / "collector-console.txt").open("ab") as console:
                    self._process = self.spawn(command, stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT)
                manifest["collector_pid"] = getattr(self._process, "pid", None)
                _atomic_json(session / "manifest.json", manifest)
                result = self.status()
                return {"ok": result["state"] != "failed", **result}
            except Exception as error:
                self._launch_error = f"网络监测启动失败：{error}"
                return {"ok": False, **self.status(), "state": "failed"}

    def stop(self) -> dict:
        with self._lock:
            if self._session is None:
                return {"ok": True, **self.status()}
            self._stop_requested = True
            try:
                (self._session / "stop.request").write_text(_utc() + " companion stopped", encoding="utf-8")
                if self._process is not None:
                    try:
                        self._process.wait(timeout=STOP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        return {"ok": False, **self.status(), "message": "已请求停止；监测进程仍在保存日志，未强制结束。"}
                else:
                    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
                    while self.status()["active"] and time.monotonic() < deadline:
                        time.sleep(0.1)
                    if self.status()["active"]:
                        return {"ok": False, **self.status(), "message": "已向恢复的监测会话请求停止，尚未收到完成状态；没有终止进程。"}
                return {"ok": True, **self.status()}
            except Exception as error:
                return {"ok": False, **self.status(), "message": f"停止监测失败：{error}"}

    @staticmethod
    def _elevate(command: list[str]) -> int:
        if os.name != "nt":
            raise RuntimeError("深度记录仅支持Windows。")
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        result = shell.ShellExecuteW(None, "runas", command[0], subprocess.list2cmdline(command[1:]), None, 0)
        return int(result or 0)

    def start_deep(self) -> dict:
        with self._lock:
            current = self.status()
            if not current["active"] or self._session is None:
                return {"ok": False, **current, "message": "请先启动伴航和普通网络监测。"}
            if current["deep_state"] in {"starting", "active"}:
                return {"ok": True, **current}
            script = self.resources / "Trace-NetworkDiagnostics.ps1"
            if not script.is_file():
                return {"ok": False, **current, "message": "缺少深度记录脚本，请修复安装。"}
            try:
                (self._session / "trace-stop.request").unlink(missing_ok=True)
                requested = _utc()
                command = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                           "-SessionDir", str(self._session), "-DurationMinutes", "120"]
                result = self._elevate(command)
                if result <= 32:
                    raise RuntimeError(f"管理员启动未获确认（ShellExecute代码 {result}）；可能已取消或未获授权。")
                self._deep_error = ""
                self._deep_requested_at = requested
                _atomic_json(self._session / "deep-launch.json", {"requested_at_utc": requested, "state": "starting"})
                return {"ok": True, **self.status()}
            except Exception as error:
                self._deep_error = str(error)
                return {"ok": False, **self.status(), "message": str(error)}

    def _export_sessions(self) -> list[Path]:
        previous = []
        if self.root.is_dir():
            previous = sorted((path for path in self.root.iterdir()
                               if path.is_dir() and path != self._session and self._owned(path)),
                              key=lambda path: path.name, reverse=True)[:HISTORY_COUNT]
        return ([self._session] if self._session and self._owned(self._session) else []) + previous

    def _finish_deep_for_export(self, errors: list[str]) -> bool:
        trace, _ = self._status_file("trace-status.json")
        if str(trace.get("State", "")).lower() in {"stopfailed", "ownershiplost", "failed", "error"}:
            errors.append("深度记录未确认正常保存，当前会话ETL未导出；错误状态已保留在诊断清单。")
            return False
        if self._session is None or self.status()["deep_state"] not in {"starting", "active"}:
            return True
        (self._session / "trace-stop.request").write_text(_utc() + " export requested", encoding="utf-8")
        deadline = time.monotonic() + DEEP_STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.status()["deep_state"] not in {"starting", "active"}:
                return True
            time.sleep(0.1)
        errors.append("深度记录未在等待时间内确认保存；当前会话ETL未导出，普通监测继续。")
        return False

    def export(self, destination: Path, companion_session: Path | None = None) -> dict:
        with self._lock:
            destination = Path(destination).expanduser().absolute()
            if destination.suffix.lower() != ".zip":
                return {"ok": False, "message": "导出文件必须使用.zip后缀。", "errors": ["invalid ZIP destination"], "omissions": ["invalid ZIP destination"], "files": 0, "path": ""}
            errors: list[str] = []
            temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
            included: list[dict] = []
            used = 0
            try:
                sessions = self._export_sessions()
                if not sessions:
                    return {"ok": False, "message": "还没有网络诊断记录可导出。", "errors": [], "omissions": [], "files": 0, "path": ""}
                include_current_etl = self._finish_deep_for_export(errors)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    def add_bytes(name: str, content: bytes) -> None:
                        nonlocal used
                        if used + len(content) > MAX_EXPORT_BYTES:
                            errors.append(f"超过256MB导出上限，未包括：{name}")
                            return
                        archive.writestr(name, content)
                        used += len(content)
                        included.append({"path": name, "bytes": len(content)})

                    def add_file(path: Path, name: str, binary: bool = False) -> None:
                        try:
                            if path.is_symlink():
                                errors.append(f"文件是链接，未导出：{name}")
                                return
                            if not path.is_file():
                                errors.append(f"文件不存在，未导出：{name}")
                                return
                            if path.stat().st_size > MAX_EXPORT_BYTES - used:
                                errors.append(f"文件超过剩余导出容量，未包括：{name}")
                                return
                            content = path.read_bytes()
                            if not binary:
                                try:
                                    text = content.decode("utf-8-sig")
                                except UnicodeDecodeError:
                                    text = content.decode("utf-8", errors="replace")
                                    errors.append(f"部分文本编码无法完整还原：{name}")
                                content = _redact_text(text, path.name).encode("utf-8")
                            add_bytes(name, content)
                        except (OSError, ValueError) as error:
                            errors.append(f"无法导出 {name}：{error}")

                    companion_candidates = [Path(companion_session)] if companion_session is not None else ([self._companion] if self._companion else [])
                    for session in sessions:
                        try:
                            recorded_companion = _read_json(session / "manifest.json").get("companion_session")
                            if recorded_companion:
                                companion_candidates.append(Path(recorded_companion))
                        except (OSError, ValueError, TypeError) as error:
                            errors.append(f"无法读取会话清单 {session.name}：{error}")
                        prefix = "diagnostics/" + session.name + "/"
                        try:
                            candidates = sorted(session.iterdir(), key=lambda path: (path.suffix.lower() == ".etl", path.name))
                        except OSError as error:
                            errors.append(f"无法列出会话 {session.name}：{error}")
                            continue
                        history_trace_active = False
                        if session != self._session:
                            try:
                                history_trace_active = str(_read_json(session / "trace-status.json").get("State", "")).lower() in {"starting", "running", "stopping", "stopfailed", "ownershiplost", "failed", "error"}
                            except FileNotFoundError:
                                pass
                            except (OSError, ValueError, TypeError) as error:
                                errors.append(f"无法确认历史ETL状态 {session.name}：{error}")
                                history_trace_active = True
                        for path in candidates:
                            if path.absolute() in {temporary, destination}:
                                continue
                            if _FORBIDDEN_FILE.search(path.name):
                                continue
                            if _TEXT_FILE.search(path.name):
                                add_file(path, prefix + path.name)
                            elif path.suffix.lower() == ".etl":
                                if history_trace_active:
                                    errors.append(f"历史深度记录尚未确认结束，未导出：{session.name}/{path.name}")
                                    continue
                                if session == self._session and not include_current_etl:
                                    continue
                                add_file(path, prefix + path.name, binary=True)
                    if not companion_candidates:
                        errors.append("未提供当前伴航会话；未包括伴航运行日志。")
                    seen_companions: set[str] = set()
                    companion_labels: dict[str, int] = {}
                    exported_companions: list[str] = []
                    for companion in companion_candidates:
                        if companion.is_symlink():
                            errors.append(f"伴航会话是链接，未导出：{companion.name}")
                            continue
                        companion = companion.resolve()
                        identity = os.path.normcase(str(companion))
                        if identity in seen_companions:
                            continue
                        seen_companions.add(identity)
                        exported_companions.append(str(companion))
                        companion_labels[companion.name] = companion_labels.get(companion.name, 0) + 1
                        suffix = companion_labels[companion.name]
                        label = companion.name if suffix == 1 else companion.name + "-" + str(suffix)
                        for name in COMPANION_FILES:
                            path = companion / name
                            add_file(path, "companion/" + label + "/" + name)
                    protection_state = self.root.parent / "claude-protection/status.json"
                    if protection_state.is_file():
                        add_file(protection_state, "claude-protection/status.json")
                    for name in ("status.json", "preferences.json"):
                        state_file = self.root.parent / "wechat-direct" / name
                        if state_file.is_file():
                            add_file(state_file, "wechat-direct/" + name)
                    # Only the relay's non-secret health metadata is exported.
                    # Never read either component's authentication configuration.
                    relay_state = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "OrangeDeskWeChatDirect/relay-status.json"
                    if relay_state.is_file():
                        add_file(relay_state, "wechat-direct/relay-status.json")
                    roaming = os.environ.get("APPDATA")
                    vpn_path = (Path(roaming) / "FastOrange" / "Logs" / ("openvpn_" + datetime.now().strftime("%Y%m%d") + ".log")) if roaming else None
                    if vpn_path is None or not vpn_path.is_file():
                        errors.append("未找到快橙当日OpenVPN连接日志。")
                    elif vpn_path.is_symlink():
                        errors.append("快橙日志是链接，已跳过。")
                    else:
                        try:
                            filtered = []
                            filtered_bytes = 0
                            with vpn_path.open("r", encoding="utf-8-sig", errors="replace") as source:
                                for line in source:
                                    if _VPN_LINE.search(line):
                                        if "\ufffd" in line and "快橙连接日志存在无法完整还原的文本编码。" not in errors:
                                            errors.append("快橙连接日志存在无法完整还原的文本编码。")
                                        clean = _redact_string(line)
                                        filtered_bytes += len(clean.encode("utf-8"))
                                        if filtered_bytes > min(16 * 1024 * 1024, MAX_EXPORT_BYTES - used):
                                            errors.append("快橙连接日志超过剩余容量，后续行未导出。")
                                            break
                                        filtered.append(clean)
                            add_bytes("fastorange/" + vpn_path.stem + "-filtered.txt", "".join(filtered).encode("utf-8"))
                        except OSError as error:
                            errors.append(f"无法读取快橙连接日志：{error}")
                    export_manifest = {"kind": KIND, "exported_at_utc": _utc(), "computer": platform.node(),
                                       "platform": platform.platform(), "app_version": getattr(sys.modules.get("app.engine"), "APP_VERSION", "unknown"),
                                       "sessions": [path.name for path in sessions], "files": included,
                                       "companion_sessions": exported_companions,
                                       "uncompressed_bytes": used, "max_export_bytes": MAX_EXPORT_BYTES, "errors": errors,
                                       "redaction": "Text secrets redacted. ETL is binary local provider metadata, not text-redacted. No automatic upload."}
                    archive.writestr("export-manifest.json", json.dumps(_redact_object(export_manifest), ensure_ascii=False, indent=2).encode("utf-8"))
                    readme = ("OrangeDesk network diagnostics\nExport time (UTC): " + export_manifest["exported_at_utc"] +
                              "\nIncludes current diagnostic session and at most three recent owned sessions. No history is automatically deleted.\n"
                              "Text exports redact recognized credentials; ETL is binary provider metadata and can include SSID/BSSID, device identifiers and local paths.\n"
                              "No VPN configuration, auth files, credential cache, Claude conversation, decrypted TLS or packet payload is collected. No automatic upload.\n"
                              "System DNS and applications outside the companion proxy are outside its TCP binding guarantee; country is not verified.\n"
                              "Export issues:\n" + ("\n".join(errors) if errors else "None") + "\n")
                    archive.writestr("README.txt", _redact_string(readme).encode("utf-8"))
                os.replace(temporary, destination)
                return {"ok": True, "path": str(destination), "files": len(included) + 2,
                        "bytes": destination.stat().st_size, "errors": errors, "omissions": errors,
                        "session_path": str(self._session) if self._session else "",
                        "message": "诊断日志已导出。" if not errors else "诊断日志已导出，部分记录缺失，详情见导出清单。"}
            except Exception as error:
                return {"ok": False, "message": f"导出诊断日志失败：{error}", "errors": errors + [str(error)], "omissions": errors + [str(error)], "files": 0, "path": ""}
            finally:
                temporary.unlink(missing_ok=True)
