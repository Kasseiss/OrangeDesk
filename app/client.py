"""Authenticated loopback client shared by the console and Qt interfaces."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .engine import APP_VERSION, ServiceError, gui_command, hidden_spawn, process_identity, read_json, service_root

_launch_lock = threading.Lock()


def _endpoint() -> dict | None:
    value = read_json(service_root() / "service.json")
    try:
        pid, ticks, port = int(value["pid"]), int(value["ticks"]), int(value["port"])
        token, executable = value["token"], value["executable"]
        if pid <= 0 or ticks <= 0 or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) < 32 or not isinstance(executable, str):
            return None
        identity = process_identity(pid)
        if not identity or identity[0] != ticks or os.path.normcase(os.path.abspath(identity[1])) != os.path.normcase(os.path.abspath(executable)):
            return None
        expected = gui_command("--daemon")[0]
        if os.path.normcase(os.path.abspath(identity[1])) != os.path.normcase(os.path.abspath(expected)):
            return None
        return value
    except (KeyError, TypeError, ValueError, ServiceError):
        return None


def _send(endpoint: dict, action: str, payload: dict | None, timeout: float) -> dict:
    body = json.dumps({"action": action, "payload": payload or {}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{int(endpoint['port'])}/rpc", body,
                                     {"Authorization": "Bearer " + endpoint["token"], "Content-Type": "application/json"}, method="POST")
    # A local control request must never be sent to the user's system/VPN proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read(32768).decode("utf-8"))
        except (ValueError, OSError):
            raise ServiceError("本地控制请求失败。") from None
    if not isinstance(value, dict) or not value.get("ok"):
        raise ServiceError(value.get("error", "后台返回了无效响应。") if isinstance(value, dict) else "后台返回了无效响应。")
    result = value.get("result")
    if not isinstance(result, dict):
        raise ServiceError("后台返回了无效响应。")
    return result


def ensure_service() -> None:
    if os.name != "nt":
        raise ServiceError("OrangeDesk 目前支持 Windows。")
    with _launch_lock:
        endpoint = _endpoint()
        if endpoint:
            try:
                _send(endpoint, "status", None, 1)
                return
            except (OSError, urllib.error.URLError, ServiceError):
                pass
        command = gui_command("--daemon")
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not getattr(sys, "frozen", False):
            kwargs["cwd"] = str(Path(__file__).resolve().parent.parent)
        try:
            process = hidden_spawn(command, **kwargs)
        except OSError as exc:
            raise ServiceError("无法启动本地后台，请修复 OrangeDesk 安装。") from exc
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            endpoint = _endpoint()
            if endpoint:
                try:
                    _send(endpoint, "status", None, 1)
                    return
                except (OSError, urllib.error.URLError, ServiceError):
                    pass
            # A duplicate daemon may exit after the other process wins the mutex.
            if process.poll() not in (None, 0):
                break
            time.sleep(0.15)
        raise ServiceError("本地后台未能启动。请检查安装目录和当前用户权限。")


def request(action: str, payload: dict | None = None, timeout: float = 5) -> dict:
    if action in ("stop", "shutdown"):
        timeout = max(timeout, 22)
    elif action == "restore":
        timeout = max(timeout, 50)
    if action != "shutdown":
        ensure_service()
    endpoint = _endpoint()
    if not endpoint:
        if action == "shutdown":
            return {"state": "stopped", "version": APP_VERSION, "message": "本地后台未运行，无需退出。", "proxy_restored": None}
        raise ServiceError("本地后台已退出，请重试。")
    try:
        result = _send(endpoint, action, payload, timeout)
        if action == "shutdown":
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                identity = process_identity(int(endpoint["pid"]))
                if identity is None or identity[0] != int(endpoint["ticks"]):
                    result["message"] = result.get("message", "") + " 本地后台已退出。"
                    return result
                time.sleep(0.1)
            raise ServiceError("保活与分流已停止，但后台退出超时；请等待后台退出后再更新或卸载。")
        return result
    except (OSError, urllib.error.URLError) as exc:
        # Never retry a mutation automatically after an unknown outcome.
        if action in ("status", "logs"):
            ensure_service()
            endpoint = _endpoint()
            if endpoint:
                try:
                    return _send(endpoint, action, payload, timeout)
                except (OSError, urllib.error.URLError):
                    pass
        raise ServiceError("与本地后台的连接中断，请查看状态确认操作结果。") from exc
