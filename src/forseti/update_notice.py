"""Discover and render an available Forseti GitHub Release update."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
import tempfile
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_LATEST_RELEASE_URL = "https://api.github.com/repos/pmatos/forseti/releases/latest"
_WHEEL_NAME = "forseti-{version}-py3-none-any.whl"
_REQUEST_TIMEOUT_S = 2.0
_CHECK_INTERVAL_S = 12 * 60 * 60


@dataclass(frozen=True)
class _Candidate:
    version: str
    wheel_url: str


@dataclass(frozen=True)
class _Cache:
    checked_at: float
    candidate: _Candidate | None


def update_notice() -> str | None:
    """Return the user-facing update banner, or ``None`` when already current."""
    current_version = installed_version()
    if current_version is None:
        return None
    now = time.time()
    cache_path = _cache_path()
    cache = _read_cache(cache_path)
    if cache is not None and 0 <= now - cache.checked_at < _CHECK_INTERVAL_S:
        candidate = cache.candidate
    else:
        try:
            candidate = _fetch_candidate(current_version)
        except (OSError, TypeError, ValueError):
            candidate = cache.candidate if cache is not None else None
        with suppress(OSError):
            _write_cache(cache_path, _Cache(checked_at=now, candidate=candidate))

    if candidate is None:
        return None
    current = _parse_version(current_version)
    candidate_parts = _parse_version(candidate.version)
    if current is None or candidate_parts is None or candidate_parts <= current:
        return None

    command = f"uv tool install --force {shlex.quote(candidate.wheel_url)}"
    return (
        f"╭─ Forseti update available: {current_version} → {candidate.version}\n"
        f"│ {command}\n"
        "╰─"
    )


def installed_version() -> str | None:
    """Return installed Forseti metadata, if this source tree is installed."""
    try:
        return importlib.metadata.version("forseti")
    except importlib.metadata.PackageNotFoundError:
        return None


def _fetch_candidate(current_version: str) -> _Candidate | None:
    request = urllib.request.Request(
        _LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"forseti/{current_version}",
        },
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        payload = json.loads(response.read())

    if not isinstance(payload, dict):
        return None
    tag_name = payload.get("tag_name")
    assets = payload.get("assets")
    if not isinstance(tag_name, str) or not isinstance(assets, list):
        return None
    if not tag_name.startswith("v"):
        return None
    candidate_version = tag_name[1:]
    if _parse_version(candidate_version) is None:
        return None

    wheel_name = _WHEEL_NAME.format(version=candidate_version)
    wheel_url: str | None = None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != wheel_name:
            continue
        url = asset.get("browser_download_url")
        if isinstance(url, str) and url:
            wheel_url = url
            break
    if wheel_url is None:
        return None
    return _Candidate(version=candidate_version, wheel_url=wheel_url)


def _cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home) if cache_home else Path.home() / ".cache"
    return root / "forseti" / "update-check.json"


def _read_cache(path: Path) -> _Cache | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at = payload.get("checked_at")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int | float):
        return None
    candidate_payload = payload.get("candidate")
    if candidate_payload is None:
        candidate = None
    elif isinstance(candidate_payload, dict):
        version = candidate_payload.get("version")
        wheel_url = candidate_payload.get("wheel_url")
        if not isinstance(version, str) or not isinstance(wheel_url, str):
            return None
        candidate = _Candidate(version=version, wheel_url=wheel_url)
    else:
        return None
    return _Cache(checked_at=float(checked_at), candidate=candidate)


def _write_cache(path: Path, cache: _Cache) -> None:
    payload = {
        "checked_at": cache.checked_at,
        "candidate": (
            None
            if cache.candidate is None
            else {
                "version": cache.candidate.version,
                "wheel_url": cache.candidate.wheel_url,
            }
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            json.dump(payload, temporary)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parse_version(value: str) -> tuple[int, int, int] | None:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    major, minor, patch = parts
    return int(major), int(minor), int(patch)
