from __future__ import annotations

import http.client
import importlib.metadata
import io
import json
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from forseti.core.cli import main

_WHEEL_URL = (
    "https://github.com/pmatos/forseti/releases/download/"
    "v1.8.0/forseti-1.8.0-py3-none-any.whl"
)


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _release(version: str) -> _Response:
    wheel_url = (
        "https://github.com/pmatos/forseti/releases/download/"
        f"v{version}/forseti-{version}-py3-none-any.whl"
    )
    return _Response(
        {
            "tag_name": f"v{version}",
            "assets": [
                {
                    "name": f"forseti-{version}-py3-none-any.whl",
                    "browser_download_url": wheel_url,
                }
            ],
        }
    )


def _latest_release() -> _Response:
    return _release("1.8.0")


def _expected_banner() -> str:
    return (
        "╭─ Forseti update available: 1.7.5 → 1.8.0\n"
        f"│ uv tool install --force {_WHEEL_URL}\n"
        "╰─\n"
    )


def test_user_facing_command_warns_before_running_when_a_new_wheel_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _latest_release(),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )

    captured = capsys.readouterr()
    assert "verify-gate hooks installed" in captured.out
    assert captured.err == _expected_banner()


def test_fresh_cache_warns_again_without_another_github_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    responses = iter([_latest_release()])

    def open_once(_request: object, *, timeout: float) -> _Response:
        try:
            return next(responses)
        except StopIteration:
            pytest.fail("a fresh update cache made another GitHub request")

    monkeypatch.setattr(urllib.request, "urlopen", open_once)
    project = tmp_path / "project"

    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == _expected_banner()
    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == _expected_banner()


def test_cache_refreshes_after_twelve_hours_but_not_before(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    now = 1_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    responses = iter([_release("1.8.0"), _release("1.9.0")])
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: next(responses),
    )
    project = tmp_path / "project"

    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == _expected_banner()
    now += 12 * 60 * 60 - 1
    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == _expected_banner()
    now += 1
    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == (
        "╭─ Forseti update available: 1.7.5 → 1.9.0\n"
        "│ uv tool install --force https://github.com/pmatos/forseti/"
        "releases/download/v1.9.0/forseti-1.9.0-py3-none-any.whl\n"
        "╰─\n"
    )


def test_github_failure_is_silent_and_throttled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    attempted = False

    def unavailable(_request: object, *, timeout: float) -> _Response:
        nonlocal attempted
        if attempted:
            pytest.fail("a failed check was not throttled")
        attempted = True
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    project = tmp_path / "project"

    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == ""
    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == ""


def test_http_protocol_failure_is_silent_and_throttled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    attempted = False

    def truncated(_request: object, *, timeout: float) -> _Response:
        nonlocal attempted
        if attempted:
            pytest.fail("a failed check was not throttled")
        attempted = True
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(urllib.request, "urlopen", truncated)
    project = tmp_path / "project"

    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == ""
    assert main(["enable-project", "--harness", "claude-code", str(project)]) == 0
    assert capsys.readouterr().err == ""


def test_uninstalled_source_tree_does_not_check_for_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def not_installed(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: pytest.fail(
            "an uninstalled source tree checked for updates"
        ),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"tag_name": 123, "assets": []},
        {"tag_name": "v1.8.0", "assets": "not-a-list"},
        {"tag_name": "v1.x.0", "assets": []},
    ],
)
def test_malformed_github_release_payload_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: object,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response(payload),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_release_with_unrelated_assets_still_finds_the_wheel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    payload: dict[str, object] = {
        "tag_name": "v1.8.0",
        "assets": [
            {
                "name": "forseti-1.8.0.tar.gz",
                "browser_download_url": (
                    "https://github.com/pmatos/forseti/releases/download/"
                    "v1.8.0/forseti-1.8.0.tar.gz"
                ),
            },
            {
                "name": "forseti-1.8.0-py3-none-any.whl",
                "browser_download_url": _WHEEL_URL,
            },
        ],
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response(payload),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == _expected_banner()


def test_release_without_a_stable_v_tag_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    response = _latest_release()
    payload = json.loads(response.read())
    payload["tag_name"] = "1.8.0"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response(payload),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_release_without_a_downloadable_github_wheel_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    response = _latest_release()
    payload = json.loads(response.read())
    payload["assets"][0]["browser_download_url"] = ""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response(payload),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("candidate_version", "has_wheel"),
    [("1.7.5", True), ("1.7.4", True), ("1.8.0", False)],
)
def test_non_update_release_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candidate_version: str,
    has_wheel: bool,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    response = _release(candidate_version)
    payload = json.loads(response.read())
    if not has_wheel:
        payload["assets"] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response(payload),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_malformed_cache_timestamp_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_home = tmp_path / "cache"
    cache_file = cache_home / "forseti/update-check.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(
        json.dumps(
            {
                "checked_at": True,
                "candidate": {
                    "version": "9.9.9",
                    "wheel_url": "https://example.invalid/untrusted.whl",
                },
            }
        )
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(time, "time", lambda: 2.0)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response({"tag_name": "v1.7.5", "assets": []}),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_non_dict_cache_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_home = tmp_path / "cache"
    cache_file = cache_home / "forseti/update-check.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("[]")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response({"tag_name": "v1.7.5", "assets": []}),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "candidate_payload",
    [
        {"version": 123, "wheel_url": "https://example.invalid/x.whl"},
        {"version": "9.9.9", "wheel_url": 123},
        "not-a-dict-or-none",
        42,
    ],
)
def test_cache_with_malformed_candidate_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candidate_payload: object,
) -> None:
    cache_home = tmp_path / "cache"
    cache_file = cache_home / "forseti/update-check.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(
        json.dumps({"checked_at": 2.0, "candidate": candidate_payload})
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(time, "time", lambda: 2.0)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response({"tag_name": "v1.7.5", "assets": []}),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_non_text_cache_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_home = tmp_path / "cache"
    cache_file = cache_home / "forseti/update-check.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"\xff")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response({"tag_name": "v1.7.5", "assets": []}),
    )

    assert (
        main(["enable-project", "--harness", "claude-code", str(tmp_path / "project")])
        == 0
    )
    assert capsys.readouterr().err == ""


def test_internal_claude_code_hook_does_not_check_for_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: pytest.fail(
            "an internal Claude Code hook checked for updates"
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    assert main(["claude-code-hook", "session-start"]) == 0


def test_internal_mcp_command_does_not_check_for_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: pytest.fail(
            "the internal MCP command checked for updates"
        ),
    )
    server = types.ModuleType("forseti.core.mcp_server")
    monkeypatch.setattr(server, "serve", lambda: None, raising=False)
    monkeypatch.setitem(sys.modules, "forseti.core.mcp_server", server)

    assert main(["mcp"]) == 0


def test_version_option_reports_the_installed_distribution_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.7.5")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, *, timeout: _Response({"tag_name": "v1.7.5", "assets": []}),
    )

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "forseti 1.7.5\n"
    assert captured.err == ""
