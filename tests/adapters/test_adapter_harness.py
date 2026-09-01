"""Tests for `forseti.adapters.harness.detect_harness` (#212).

Every case passes an explicit `env` mapping -- never relies on the ambient
`os.environ` of whatever process runs the suite (which, under CI or a real
Claude Code/Codex session, may itself carry one of these markers).
"""

from __future__ import annotations

import pytest

from forseti.adapters.harness import Harness, detect_harness


def test_detects_codex_from_session_id() -> None:
    assert detect_harness({"CODEX_SESSION_ID": "abc"}) == Harness.CODEX


def test_detects_codex_from_thread_id() -> None:
    assert detect_harness({"CODEX_THREAD_ID": "abc"}) == Harness.CODEX


def test_detects_claude_code_from_claudecode_var() -> None:
    assert detect_harness({"CLAUDECODE": "1"}) == Harness.CLAUDE_CODE


def test_unrelated_codex_prefixed_var_is_not_a_match() -> None:
    # A different tool's own env var (e.g. a companion plugin) must not be
    # mistaken for the genuine Codex CLI session markers.
    assert detect_harness({"CODEX_COMPANION_SESSION_ID": "abc"}) is None


def test_both_present_is_ambiguous() -> None:
    assert detect_harness({"CLAUDECODE": "1", "CODEX_SESSION_ID": "abc"}) is None


def test_neither_present_is_unknown() -> None:
    assert detect_harness({}) is None


def test_empty_string_value_does_not_count_as_present() -> None:
    assert detect_harness({"CLAUDECODE": "", "CODEX_SESSION_ID": ""}) is None


def test_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDECODE", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_harness() == Harness.CLAUDE_CODE
