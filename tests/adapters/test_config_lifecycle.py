"""Shared installer behavior, exercised through the harness entry points."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from forseti.adapters import _config_file
from forseti.adapters._config_file import FileChange, edit_file
from forseti.adapters.claude_code import install as claude
from forseti.adapters.codex import install as codex
from forseti.adapters.oh_my_pi import install as omp


@dataclass(frozen=True)
class Installer:
    install: Callable[[Path], tuple[Path, Enum]]
    remove: Callable[[Path], tuple[Path, Enum]]
    relative_path: str
    error: type[Exception]
    description: str


INSTALLERS = (
    Installer(
        claude.install,
        claude.remove,
        ".claude/settings.local.json",
        claude.ProjectSettingsError,
        "settings",
    ),
    Installer(
        codex.install,
        codex.remove,
        ".codex/config.toml",
        codex.ProjectConfigError,
        "config",
    ),
    Installer(
        omp.install,
        omp.remove,
        ".omp/extensions/forseti-gate.ts",
        omp.ProjectConfigError,
        "extension",
    ),
    Installer(
        omp.install,
        omp.remove,
        ".omp/mcp.json",
        omp.ProjectConfigError,
        "MCP config",
    ),
)


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.relative_path)
def test_unchanged_install_preserves_bytes_inode_and_mode(
    tmp_path: Path, installer: Installer
) -> None:
    installer.install(tmp_path)
    path = tmp_path / installer.relative_path
    if path.suffix == ".json":
        # Equality is semantic for JSON: rerunning must not pretty-print it.
        path.write_text(json.dumps(json.loads(path.read_text()), indent=None))
    path.chmod(0o640)
    before = path.stat()
    contents = path.read_bytes()

    _, outcome = installer.install(tmp_path)

    assert outcome.name == "UNCHANGED"
    assert path.read_bytes() == contents
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_mode == before.st_mode


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.relative_path)
@pytest.mark.parametrize("action", ["install", "remove"])
@pytest.mark.parametrize("broken", [False, True])
def test_installers_refuse_live_and_broken_symlinks(
    tmp_path: Path, installer: Installer, action: str, broken: bool
) -> None:
    path = tmp_path / installer.relative_path
    path.parent.mkdir(parents=True)
    target = tmp_path / "user-config"
    if not broken:
        target.write_text("user-owned content")
    path.symlink_to(target)

    with pytest.raises(installer.error, match="is a symlink -- refusing"):
        operation = installer.install if action == "install" else installer.remove
        operation(tmp_path)

    assert path.is_symlink()
    assert target.exists() is not broken
    if not broken:
        assert target.read_text() == "user-owned content"


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda i: i.relative_path)
@pytest.mark.parametrize("action", ["install", "remove"])
def test_installers_translate_utf8_failures_without_touching_the_file(
    tmp_path: Path, installer: Installer, action: str
) -> None:
    path = tmp_path / installer.relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    with pytest.raises(installer.error) as caught:
        operation = installer.install if action == "install" else installer.remove
        operation(tmp_path)

    assert str(caught.value).startswith(
        f"{path}: cannot read existing {installer.description} ("
    )
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)
    assert path.read_bytes() == b"\xff"


def test_codex_removal_can_strip_its_block_from_otherwise_invalid_toml(
    tmp_path: Path,
) -> None:
    path, _ = codex.install(tmp_path)
    path.write_text("invalid TOML = [\n\n" + path.read_text())

    _, outcome = codex.remove(tmp_path)

    assert outcome is codex.RemoveOutcome.REMOVED
    assert path.read_text() == "invalid TOML = [\n"


@pytest.mark.parametrize("action", ["install", "remove"])
def test_omp_keeps_extension_progress_when_the_mcp_config_is_invalid(
    tmp_path: Path, action: str
) -> None:
    if action == "remove":
        omp.install(tmp_path)
    path = tmp_path / ".omp/mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid JSON")

    with pytest.raises(omp.ProjectConfigError, match="cannot read existing MCP"):
        operation = omp.install if action == "install" else omp.remove
        operation(tmp_path)

    extension = tmp_path / ".omp/extensions/forseti-gate.ts"
    assert extension.exists() is (action == "install")
    assert path.read_text() == "invalid JSON"


class ConfigError(Exception):
    """An adapter's read error, distinct from transform and write failures."""


def test_edit_file_owns_create_semantic_update_noop_and_deletion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested/config.json"

    def change(transform: Callable[[Any], Any]) -> FileChange:
        return edit_file(
            path,
            decode=json.loads,
            transform=transform,
            encode=json.dumps,
            error_type=ConfigError,
            description="config",
        )

    assert change(lambda old: old) is FileChange.UNCHANGED
    assert not path.parent.exists()
    assert change(lambda _: {"owned": 1}) is FileChange.CREATED
    assert json.loads(path.read_text()) == {"owned": 1}
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text('{ "owned" : 1 }\n')
    path.chmod(0o640)
    original = path.stat()
    assert change(lambda old: dict(old)) is FileChange.UNCHANGED
    assert path.read_text() == '{ "owned" : 1 }\n'
    assert path.stat().st_ino == original.st_ino

    assert change(lambda old: {**old, "extra": 2}) is FileChange.UPDATED
    assert json.loads(path.read_text()) == {"owned": 1, "extra": 2}
    assert path.stat().st_mode == original.st_mode
    assert change(lambda _: None) is FileChange.REMOVED
    assert not path.exists()


def test_edit_file_rejects_symlinks_before_decoding_or_transforming(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config"
    path.symlink_to(tmp_path / "missing")

    def unexpected(value: Any) -> Any:
        pytest.fail("a symlink must be rejected before adapter code runs")

    with pytest.raises(ConfigError, match="is a symlink -- refusing"):
        edit_file(
            path,
            decode=unexpected,
            transform=unexpected,
            encode=unexpected,
            error_type=ConfigError,
            description="config",
        )
    assert path.is_symlink()


@pytest.mark.parametrize("contents", [b"\xff", b"not json"])
def test_edit_file_translates_read_and_decode_errors(
    tmp_path: Path, contents: bytes
) -> None:
    path = tmp_path / "config"
    path.write_bytes(contents)
    with pytest.raises(ConfigError) as caught:
        edit_file(
            path,
            decode=json.loads,
            transform=lambda old: old,
            encode=json.dumps,
            error_type=ConfigError,
            description="settings",
        )
    assert str(caught.value).startswith(f"{path}: cannot read existing settings (")
    assert isinstance(caught.value.__cause__, ValueError)
    assert path.read_bytes() == contents


def test_edit_file_does_not_translate_transform_errors(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("original")
    error = ValueError("adapter rejected ownership")

    def reject(old: str | None) -> str | None:
        raise error

    with pytest.raises(ValueError) as caught:
        edit_file(
            path,
            decode=str,
            transform=reject,
            encode=str,
            error_type=ConfigError,
            description="config",
        )
    assert caught.value is error
    assert path.read_text() == "original"


def test_edit_file_propagates_atomic_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config"
    path.write_text("original")
    error = OSError("write refused")

    def fail_write(path: Path, text: str) -> None:
        raise error

    monkeypatch.setattr(_config_file, "write_text_atomic", fail_write)
    with pytest.raises(OSError) as caught:
        edit_file(
            path,
            decode=str,
            transform=lambda _: "changed",
            encode=str,
            error_type=ConfigError,
            description="config",
        )
    assert caught.value is error
    assert path.read_text() == "original"
