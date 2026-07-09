"""Tests for the ``claude -p`` transport client -- subprocess is always faked.

No real ``claude`` binary is ever spawned here: ``subprocess.run`` is
monkeypatched, so these assert the argv we build, the stdin we send, envelope
unwrapping, and that every failure mode raises `LLMError` (never a silent pass).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from forseti.properties import ClaudeCliClient, LLMError


def _install_fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: BaseException | None = None,
) -> dict[str, Any]:
    """Patch ``subprocess.run`` in the llm module; return a dict of captured args."""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("forseti.properties.llm.subprocess.run", fake_run)
    return captured


def test_complete_unwraps_result_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = json.dumps({"type": "result", "is_error": False, "result": "HELLO"})
    captured = _install_fake_run(monkeypatch, stdout=envelope)

    out = ClaudeCliClient(model="sonnet").complete("my prompt")

    assert out == "HELLO"
    cmd = captured["cmd"]
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in cmd
    assert "--max-turns" not in cmd  # absent from the installed build
    assert captured["kwargs"]["input"] == "my prompt"


def test_provider_is_claude_p() -> None:
    assert ClaudeCliClient.provider == "claude -p"
    assert ClaudeCliClient(model="opus").model == "opus"


def test_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, returncode=2, stderr="boom")
    with pytest.raises(LLMError, match="exit 2"):
        ClaudeCliClient().complete("p")


def test_non_json_stdout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, stdout="not json at all")
    with pytest.raises(LLMError, match="non-JSON"):
        ClaudeCliClient().complete("p")


def test_error_envelope_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, stdout=json.dumps({"is_error": True, "result": "x"}))
    with pytest.raises(LLMError, match="reported an error"):
        ClaudeCliClient().complete("p")


def test_empty_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(
        monkeypatch, stdout=json.dumps({"is_error": False, "result": "   "})
    )
    with pytest.raises(LLMError, match="empty or absent"):
        ClaudeCliClient().complete("p")


def test_non_string_result_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, stdout=json.dumps({"is_error": False, "result": 42}))
    with pytest.raises(LLMError, match="empty or absent"):
        ClaudeCliClient().complete("p")


def test_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, raises=FileNotFoundError())
    with pytest.raises(LLMError, match="not found"):
        ClaudeCliClient(claude_bin="nope").complete("p")


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(
        monkeypatch, raises=subprocess.TimeoutExpired(cmd="claude", timeout=1.0)
    )
    with pytest.raises(LLMError, match="timed out"):
        ClaudeCliClient(timeout_s=1.0).complete("p")


def test_os_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, raises=OSError("bad fd"))
    with pytest.raises(LLMError, match="invocation failed"):
        ClaudeCliClient().complete("p")
