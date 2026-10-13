"""Validated route rules with no Qt, engine, or network dependencies."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

MAX_RULES = 1000
MAX_FILE_BYTES = 256 * 1024
MAX_REVISION = 2**31 - 1
_LOCK_TIMEOUT = 2.0
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/")
_MARKDOWN_LINK = re.compile(r"\[[^\]\r\n]*\]\(([^\s)]+)\)")
_DOMAIN_SEPARATORS = re.compile(r"[,;，；]+")


class WhitelistError(ValueError):
    """A safe, user-facing validation or persistence failure."""


def _is_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
        return True
    except ipaddress.AddressValueError:
        return False


def normalize_target(value: str) -> str:
    """Return only the canonical host, discarding URL credentials and paths.

    HTTP/HTTPS URLs and browser-style host[:port]/path inputs are supported.
    An ambiguous local path with a single-label host needs an explicit scheme.
    """
    if not isinstance(value, str):
        raise WhitelistError("地址必须是文本。")
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if not value:
        raise WhitelistError("请输入域名、IPv4 地址或 HTTP/HTTPS 链接。")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise WhitelistError("单个地址不能包含空格或控制字符；多个地址请分开粘贴。")
    if "\\" in value:
        raise WhitelistError("不支持本地文件路径或反斜杠；请填写域名或 HTTP/HTTPS 链接。")

    scheme = _SCHEME.match(value)
    explicit_url = scheme is not None
    if scheme and scheme.group(1).lower() not in {"http", "https"}:
        raise WhitelistError("链接只支持 http:// 或 https://，请改填域名。")
    if value.startswith("//") or value.startswith("/"):
        raise WhitelistError("地址缺少域名；请填写域名或完整的 HTTP/HTTPS 链接。")
    # Scheme-less host:port is valid, but a URI such as mailto:user@host is
    # ambiguous with credentials. Explicit URLs can safely carry userinfo.
    prefix = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", value)
    if not explicit_url and prefix:
        rest = prefix.group(2)
        if "@" in rest or prefix.group(1).lower() in {"http", "https", "file", "mailto", "data", "javascript", "about"}:
            raise WhitelistError("链接只支持完整的 http:// 或 https://；域名后可填写数字端口。")
    try:
        parsed = urlsplit(value if explicit_url else "//" + value)
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if "[" in authority or "]" in authority or authority.count(":") > 1:
            raise WhitelistError("暂不支持 IPv6 地址；请填写域名或 IPv4 地址。")
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        if isinstance(error, WhitelistError):
            raise
        raise WhitelistError("地址或端口无效；端口应为 1 到 65535 的整数。") from error
    if not host:
        raise WhitelistError("地址缺少域名；请填写域名或 IPv4 地址。")
    if authority.endswith(":") or (port is not None and not 1 <= port <= 65535):
        raise WhitelistError("端口应为 1 到 65535 的整数，不能留空。")
    if not explicit_url and _is_ipv4(host.rstrip(".")) and re.fullmatch(r"/\d+", parsed.path):
        raise WhitelistError("暂不支持 CIDR 网段；请填写单个 IPv4 地址。")

    host = host.removesuffix(".")
    try:
        canonical = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise WhitelistError("域名格式无效；请检查域名文字和每段长度。") from error
    if not canonical or len(canonical) > 253:
        raise WhitelistError("域名长度无效；完整域名不能超过 253 个字符。")
    if _is_ipv4(canonical):
        return canonical
    if re.fullmatch(r"[0-9.]+", canonical):
        raise WhitelistError("IPv4 地址无效；请填写四段 0 到 255 的数字，不要使用前导零。")
    for label in canonical.split("."):
        if not _LABEL.fullmatch(label):
            raise WhitelistError("域名格式无效；每段只能使用字母、数字和中间的连字符，不支持通配符或下划线。")
        if label.startswith("xn--"):
            try:
                if label.encode("ascii").decode("idna").encode("idna").decode("ascii").lower() != label:
                    raise UnicodeError("noncanonical IDN")
            except UnicodeError as error:
                raise WhitelistError("国际化域名编码无效；请粘贴原始域名或完整链接。") from error
    if not explicit_url and parsed.path and "." not in canonical:
        raise WhitelistError("此输入可能是本地路径；请粘贴完整的 HTTP/HTTPS 链接以确认域名。")
    return canonical


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WhitelistError("白名单文件包含重复字段，请修复文件后重试。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise WhitelistError("白名单文件包含无效的 JSON 数值，请修复文件后重试。")


def _split_targets(text: str) -> list[str]:
    """完整网址按换行或空格分开；纯域名还可用逗号或分号分隔。

    Punctuation in URL paths, queries, fragments, or userinfo belongs to that
    URL. It must never become another rule after its sensitive parts are removed.
    """
    tokens = []
    for token in _MARKDOWN_LINK.sub(r"\1", text).split():
        if _SCHEME.match(token) or any(character in token for character in "/?#@"):
            tokens.append(token)
        else:
            tokens.extend(part for part in _DOMAIN_SEPARATORS.split(token) if part)
    return tokens


@contextmanager
def _configuration_lock(path: Path):
    """Bounded process lock for configuration only, independent of the engine."""
    try:
        stream = path.open("a+b")
    except OSError as error:
        raise WhitelistError("无法打开白名单编辑锁，请检查目录访问权限。") from error
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            def acquire():
                stream.seek(0)
                # Windows permits locking the first byte of an empty file.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            # Keep the persistence module testable without adding dependencies.
            import fcntl

            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                acquire()
                locked = True
                break
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WhitelistError("白名单正在被其他编辑窗口更新，等待约 2 秒后仍未取得编辑锁，请稍后重试。") from error
                time.sleep(min(0.025, remaining))
        yield
    finally:
        if locked:
            try:
                release()
            except OSError:
                # Closing the descriptor also releases this process's byte lock.
                pass
        stream.close()


def _validate_document(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "revision", "rules"}:
        raise WhitelistError("白名单文件结构无效，应只包含版本、修订号和规则。")
    if type(value["version"]) is not int or value["version"] != 1:
        raise WhitelistError("白名单文件版本无效，当前仅支持版本 1。")
    if type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION:
        raise WhitelistError(f"白名单修订号无效，应为 0 到 {MAX_REVISION} 的整数。")
    if not isinstance(value["rules"], list) or len(value["rules"]) > MAX_RULES:
        raise WhitelistError(f"白名单规则无效，最多允许 {MAX_RULES} 条规则。")
    rules = []
    seen = set()
    for index, rule in enumerate(value["rules"], 1):
        if not isinstance(rule, dict) or set(rule) != {"host", "mode", "subdomains"}:
            raise WhitelistError(f"白名单第 {index} 条规则结构无效。")
        if not isinstance(rule["host"], str):
            raise WhitelistError(f"白名单第 {index} 条规则的域名必须是文本。")
        try:
            host = normalize_target(rule["host"])
        except WhitelistError as error:
            raise WhitelistError(f"白名单第 {index} 条规则：{error}") from error
        if host != rule["host"]:
            raise WhitelistError(f"白名单第 {index} 条规则的域名必须是标准的小写 ASCII 域名或 IPv4 地址。")
        if not isinstance(rule["mode"], str) or rule["mode"] not in {"direct", "vpn"}:
            raise WhitelistError(f"白名单第 {index} 条规则的模式无效，只支持 direct 或 vpn。")
        if type(rule["subdomains"]) is not bool:
            raise WhitelistError(f"白名单第 {index} 条规则的子域开关必须是布尔值。")
        if _is_ipv4(host) and rule["subdomains"]:
            raise WhitelistError(f"白名单第 {index} 条规则是 IPv4 地址，不能包含子域。")
        key = host, rule["subdomains"]
        if key in seen:
            raise WhitelistError(f"白名单第 {index} 条规则与前面的规则重复。")
        seen.add(key)
        rules.append({"host": host, "mode": rule["mode"], "subdomains": rule["subdomains"]})
    rules.sort(key=lambda rule: (rule["host"], rule["subdomains"]))
    return {"version": 1, "revision": value["revision"], "rules": rules}


class WhitelistStore:
    """Load and atomically update a caller-owned whitelist.json directory.

    ``writer(path, document)`` must replace the file atomically. ``secure(root)``
    protects/creates that directory before every changed write. A store's lock
    serializes its reads; a bounded whitelist.lock byte lock serializes changed
    read-modify-write operations across editor and service processes.
    """

    def __init__(self, root: Path, writer: Callable[[Path, dict], None], secure: Callable[[Path], None]):
        self.root = Path(root)
        self.path = self.root / "whitelist.json"
        self._writer = writer
        self._secure = secure
        self._lock = threading.RLock()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "revision": 0, "rules": []}
        except OSError as error:
            raise WhitelistError("无法读取白名单文件，请检查文件访问权限。") from error
        if len(raw) > MAX_FILE_BYTES:
            raise WhitelistError("白名单文件超过 256 KiB，请缩减规则后重试。")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_from_pairs, parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError) as error:
            if isinstance(error, WhitelistError):
                raise
            raise WhitelistError("白名单文件不是有效的 UTF-8 JSON，请修复文件后重试。") from error
        return _validate_document(value)

    @staticmethod
    def _validate_options(mode: str | None, subdomains: bool, check_mode: bool = True) -> None:
        if check_mode and (not isinstance(mode, str) or mode not in {"direct", "vpn"}):
            raise WhitelistError("规则模式无效，只支持 direct 或 vpn。")
        if type(subdomains) is not bool:
            raise WhitelistError("包含子域的开关必须是布尔值。")

    def add_many(self, text: str, mode: str, subdomains: bool = True) -> dict:
        """完整网址按换行或空格分开；纯域名还可用逗号或分号分隔。"""
        self._validate_options(mode, subdomains)
        if not isinstance(text, str):
            raise WhitelistError("粘贴的地址必须是文本。")
        try:
            if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise WhitelistError("粘贴的内容过长，请分批添加，每次不超过 256 KiB。")
        except UnicodeError as error:
            raise WhitelistError("粘贴的文本编码无效，请重新复制地址。") from error
        tokens = _split_targets(text)
        if not tokens:
            raise WhitelistError("请粘贴至少一个域名、IPv4 地址或 HTTP/HTTPS 链接。")
        if len(tokens) > MAX_RULES:
            raise WhitelistError(f"一次最多添加 {MAX_RULES} 个地址，请分批粘贴。")
        targets = []
        for index, token in enumerate(tokens, 1):
            try:
                host = normalize_target(token)
            except WhitelistError as error:
                raise WhitelistError(f"第 {index} 项：{error}") from error
            targets.append((host, False if _is_ipv4(host) else subdomains))
        def merge(current):
            rules = {(rule["host"], rule["subdomains"]): rule for rule in current["rules"]}
            for host, include_subdomains in targets:
                rules[host, include_subdomains] = {"host": host, "mode": mode, "subdomains": include_subdomains}
            if len(rules) > MAX_RULES:
                raise WhitelistError(f"白名单最多允许 {MAX_RULES} 条规则，请先删除部分规则。")
            return sorted(rules.values(), key=lambda rule: (rule["host"], rule["subdomains"]))

        return self._mutate(merge)

    def remove(self, host: str, subdomains: bool = True) -> dict:
        self._validate_options(None, subdomains, check_mode=False)
        canonical = normalize_target(host)
        include_subdomains = False if _is_ipv4(canonical) else subdomains
        def merge(current):
            return [rule for rule in current["rules"] if (rule["host"], rule["subdomains"]) != (canonical, include_subdomains)]

        return self._mutate(merge)

    def _mutate(self, merge: Callable[[dict], list[dict]]) -> dict:
        with self._lock:
            current = self._snapshot()
            if self._changed_document(current, merge(current)) is None:
                return current
            try:
                self._secure(self.root)
            except Exception as error:
                raise WhitelistError("无法安全保存白名单，请检查目录权限后重试。") from error
            with _configuration_lock(self.root / "whitelist.lock"):
                # Another process may have updated the file after the first
                # read. Merge against its revision while holding the byte lock.
                current = self._snapshot()
                return self._save_if_changed(current, merge(current))

    @staticmethod
    def _changed_document(current: dict, rules: list[dict]) -> dict | None:
        if rules == current["rules"]:
            return None
        if current["revision"] >= MAX_REVISION:
            raise WhitelistError("白名单修订号已达到上限，无法继续修改，请备份后重建白名单。")
        document = {"version": 1, "revision": current["revision"] + 1, "rules": rules}
        try:
            encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise WhitelistError("白名单内容无法保存，请缩减规则后重试。") from error
        if len(encoded) > MAX_FILE_BYTES:
            raise WhitelistError("白名单文件超过 256 KiB，请缩减规则后重试。")
        return document

    def _save_if_changed(self, current: dict, rules: list[dict]) -> dict:
        document = self._changed_document(current, rules)
        if document is None:
            return current
        try:
            self._writer(self.path, copy.deepcopy(document))
        except Exception as error:
            raise WhitelistError("无法安全保存白名单，请检查目录权限后重试。") from error
        return document
