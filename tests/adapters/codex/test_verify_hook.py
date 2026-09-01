"""Tests for the Codex `PostToolUse` verify hook (`adapters/codex/verify_hook.py`).

Hermetic: `_verify`'s own `subprocess.run` call and `main`'s call into `_verify`
are both monkeypatched -- no esbmc, no real `forseti verify`, no real Codex
session.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from forseti.adapters.codex import verify_hook


def test_edited_sources_parses_add_update_move_headers() -> None:
    command = (
        "*** Add File: new.c\n*** Update File: existing.cpp\n*** Move to: renamed.py\n"
    )
    assert verify_hook._edited_sources(command) == [
        "new.c",
        "existing.cpp",
        "renamed.py",
    ]


def test_edited_sources_dedupes_and_filters_by_suffix() -> None:
    command = "*** Update File: a.c\n*** Update File: a.c\n*** Update File: README.md\n"
    assert verify_hook._edited_sources(command) == ["a.c"]


def test_edited_sources_empty_command() -> None:
    assert verify_hook._edited_sources("") == []


def test_verify_parses_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps({"verdict": "violated", "counterexample": "x == 0"}),
            stderr="",
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    verdict, evidence = verify_hook._verify("f.c")
    assert verdict == "violated"
    assert evidence == "x == 0"


def test_verify_reports_skipped_on_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("forseti not found")

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    verdict, evidence = verify_hook._verify("f.c")
    assert verdict == "skipped"
    assert "forseti not found" in evidence


def test_verify_reports_skipped_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="not json", stderr="boom"
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    verdict, evidence = verify_hook._verify("f.c")
    assert verdict == "skipped"
    assert evidence == "boom"


def test_main_ignores_non_apply_patch_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "shell"})))
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_tolerates_unparseable_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert verify_hook.main() == 0


def test_main_skips_nonexistent_edited_file(monkeypatch: pytest.MonkeyPatch) -> None:
    event = {
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Update File: /does/not/exist.c\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert verify_hook.main() == 0


def test_main_blocks_on_violated_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    edited = tmp_path / "unit.c"
    edited.write_text("int main(void) { return 0; }\n")
    event = {
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Update File: {edited}\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(
        verify_hook, "_verify", lambda path: ("violated", "counterexample: x=0")
    )

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert str(edited) in payload["reason"]
    assert "counterexample: x=0" in payload["reason"]


def test_main_surfaces_inconclusive_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    edited = tmp_path / "unit.c"
    edited.write_text("int main(void) { return 0; }\n")
    event = {
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Update File: {edited}\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_verify", lambda path: ("unknown", ""))

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "decision" not in payload
    assert str(edited) in payload["systemMessage"]
    assert "unknown" in payload["systemMessage"]


def test_main_allows_verified_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    edited = tmp_path / "unit.c"
    edited.write_text("int main(void) { return 0; }\n")
    event = {
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Update File: {edited}\n"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_verify", lambda path: ("verified", ""))

    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_reports_violated_plus_inconclusive_residual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.c"
    bad.write_text("x\n")
    other = tmp_path / "other.c"
    other.write_text("y\n")
    event = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (f"*** Update File: {bad}\n*** Update File: {other}\n")
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    def fake_verify(path: str) -> tuple[str, str]:
        return ("violated", "cex") if path == str(bad) else ("unknown", "")

    monkeypatch.setattr(verify_hook, "_verify", fake_verify)

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert str(bad) in payload["reason"]
    assert "Also inconclusive" in payload["reason"]
    assert str(other) in payload["reason"]
