"""One per-user state location shared across desktop and virtualized launchers."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .whitelist import WhitelistStore, WhitelistError, MAX_RULES


def state_root() -> Path:
    override = os.environ.get("ORANGEDESK_DATA_HOME")
    if override:
        return Path(override).expanduser().absolute()
    # AppData can be redirected differently for a packaged launcher and a
    # desktop launch. This location is outside the virtualized AppData tree.
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".OrangeDesk"


def legacy_roots(local: Path | None = None) -> list[Path]:
    base = local or Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local")))
    result = [base / "OrangeDesk"]
    try:
        packages = sorted((base / "Packages").iterdir(), key=lambda path: path.name)
    except OSError:
        packages = []
    for package in packages[:512]:
        candidate = package / "LocalCache/Local/OrangeDesk"
        if candidate.is_dir():
            result.append(candidate)
    return result


def migrate_preferences(root: Path, writer, secure, candidates: list[Path] | None = None) -> None:
    """Import only validated settings, never bearer tokens or process records."""
    if os.environ.get("ORANGEDESK_DATA_HOME") and candidates is None:
        return
    candidates = candidates if candidates is not None else legacy_roots()
    source_documents = []
    for source in candidates:
        try:
            if not (source / "whitelist.json").is_file():
                continue
            document = WhitelistStore(source, writer=writer, secure=secure).snapshot()
            source_documents.append(((source / "whitelist.json").stat().st_mtime_ns, str(source), document))
        except (OSError, WhitelistError):
            continue
    if not (root / "whitelist.json").exists() and source_documents:
        merged = {}
        # A newer saved choice wins for the same host/scope. Distinct rules
        # from the old launch environments are all retained.
        for _, _, document in sorted(source_documents):
            for rule in document["rules"]:
                merged[rule["host"], rule["subdomains"]] = rule
        if len(merged) > MAX_RULES:
            raise WhitelistError("旧版白名单合并后超过 1000 条，请保留旧配置并缩减后重试。")
        store = WhitelistStore(root, writer=writer, secure=secure)
        for mode in ("direct", "vpn"):
            for scope in (False, True):
                hosts = [r["host"] for r in merged.values() if r["mode"] == mode and r["subdomains"] == scope]
                if hosts:
                    store.add_many("\n".join(hosts), mode, scope)
    if not (root / "appearance.json").exists():
        appearances = []
        for source in candidates:
            path = source / "appearance.json"
            try:
                with path.open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    continue
                value = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(value, dict):
                    continue
                backgrounds = {"night-lake", "sunset-coast", "anime-rain-city", "anime-seaside", "cosmic-nebula", "abstract-aurora"}
                if value.get("theme") not in {"dark", "light"} or value.get("background") not in backgrounds:
                    continue
                appearances.append((path.stat().st_mtime_ns, str(path), {"theme": value["theme"], "background": value["background"]}))
            except (OSError, UnicodeError, ValueError):
                continue
        if appearances:
            secure(root)
            writer(root / "appearance.json", sorted(appearances)[-1][2])
