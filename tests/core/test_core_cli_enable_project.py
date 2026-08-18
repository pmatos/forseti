"""Tests for `forseti enable-project` and `forseti claude-code-hook` dispatch.

Hermetic: exercises the real merge/install logic against `tmp_path`, and the
hook dispatcher via stdin injection -- no esbmc, no real Claude Code session.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from forseti.core.cli import main


def test_enable_project_creates_settings_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["enable-project", str(tmp_path)])
    assert code == 0
    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    assert "installed" in capsys.readouterr().out


def test_enable_project_shared_writes_settings_json(tmp_path: Path) -> None:
    code = main(["enable-project", str(tmp_path), "--shared"])
    assert code == 0
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_enable_project_rerun_reports_already_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["enable-project", str(tmp_path)])
    capsys.readouterr()
    code = main(["enable-project", str(tmp_path)])
    assert code == 0
    assert "already up to date" in capsys.readouterr().out


def test_enable_project_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["enable-project"])
    assert code == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()


def test_enable_project_on_malformed_settings_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text("not json")

    code = main(["enable-project", str(tmp_path)])
    assert code == 1
    assert "forseti enable-project:" in capsys.readouterr().err


def test_claude_code_hook_session_start_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    code = main(["claude-code-hook", "session-start"])
    assert code == 0


def test_claude_code_hook_unknown_name_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["claude-code-hook", "does-not-exist"])
    assert excinfo.value.code == 2
