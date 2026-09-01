"""Tests for `forseti enable-project`/`disable-project` and hook dispatch (#212).

Hermetic: exercises the real merge/install logic against `tmp_path`, and the
hook dispatchers via stdin injection -- no esbmc, no real Claude Code/Codex
session. Every test that touches auto-detection clears all three harness env
vars first, so it never depends on whatever session actually runs the suite.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from forseti.core.cli import main

_HARNESS_ENV_VARS = ("CLAUDECODE", "CODEX_SESSION_ID", "CODEX_THREAD_ID")


@pytest.fixture(autouse=True)
def _clear_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_enable_project_creates_settings_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    assert code == 0
    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    assert "installed" in capsys.readouterr().out


def test_enable_project_shared_writes_settings_json(tmp_path: Path) -> None:
    code = main(
        ["enable-project", "--harness", "claude-code", str(tmp_path), "--shared"]
    )
    assert code == 0
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_enable_project_rerun_reports_already_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    capsys.readouterr()
    code = main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    assert code == 0
    assert "already up to date" in capsys.readouterr().out


def test_enable_project_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["enable-project", "--harness", "claude-code"])
    assert code == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()


def test_enable_project_on_malformed_settings_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text("not json")

    code = main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    assert code == 1
    assert "forseti enable-project:" in capsys.readouterr().err


def test_claude_code_hook_session_start_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    code = main(["claude-code-hook", "session-start"])
    assert code == 0


def test_claude_code_hook_post_tool_use_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No tool_input.file_path -- post_tool_use's own early-return path, but this
    # test's job is only to confirm `claude-code-hook post-tool-use` reaches
    # `post_tool_use.main()` at all (its own logic is covered by
    # tests/adapters/claude_code/test_post_tool_use.py).
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    code = main(["claude-code-hook", "post-tool-use"])
    assert code == 0


def test_claude_code_hook_post_bash_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a git repo -- out-of-band discovery is inactive and there is nothing
    # stale to verify, so post_bash.main() takes its own early-return path.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    code = main(["claude-code-hook", "post-bash"])
    assert code == 0


def test_claude_code_hook_stop_gate_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a git repo, nothing outstanding -- stop_gate.main() allows cleanly.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    code = main(["claude-code-hook", "stop-gate"])
    assert code == 0


def test_claude_code_hook_unknown_name_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["claude-code-hook", "does-not-exist"])
    assert excinfo.value.code == 2


def test_codex_hook_verify_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "shell"})))
    code = main(["codex-hook", "verify"])
    assert code == 0


def test_codex_hook_unknown_name_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["codex-hook", "does-not-exist"])
    assert excinfo.value.code == 2


def test_enable_project_harness_codex_creates_codex_config_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["enable-project", "--harness", "codex", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".claude").exists()
    out = capsys.readouterr().out
    assert "Codex" in out
    assert "trust" in out


def test_enable_project_harness_codex_rejects_shared(tmp_path: Path) -> None:
    code = main(["enable-project", "--harness", "codex", str(tmp_path), "--shared"])
    assert code == 1
    assert not (tmp_path / ".codex").exists()


def test_enable_project_auto_detects_codex_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_SESSION_ID", "abc")
    code = main(["enable-project", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".claude").exists()


def test_enable_project_auto_detects_claude_code_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    code = main(["enable-project", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".codex").exists()


def test_enable_project_auto_detected_codex_still_rejects_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --shared is a claude-code-only flag; fail closed even when Codex was
    # reached via auto-detection rather than an explicit --harness, rather
    # than silently ignoring the flag the caller passed.
    monkeypatch.setenv("CODEX_SESSION_ID", "abc")
    code = main(["enable-project", str(tmp_path), "--shared"])
    assert code == 1
    assert not (tmp_path / ".codex").exists()


def test_enable_project_auto_detect_ambiguous_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CODEX_SESSION_ID", "abc")
    code = main(["enable-project", str(tmp_path)])
    assert code == 1
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()
    assert "forseti enable-project:" in capsys.readouterr().err


def test_enable_project_auto_detect_unknown_fails_closed(tmp_path: Path) -> None:
    code = main(["enable-project", str(tmp_path)])
    assert code == 1
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()


def test_disable_project_requires_harness() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["disable-project"])
    assert excinfo.value.code == 2


def test_disable_project_codex_removes_only_forseti_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('notify = ["python3", "/x/notify.py"]\n')
    main(["enable-project", "--harness", "codex", str(tmp_path)])

    code = main(["disable-project", "--harness", "codex", str(tmp_path)])
    assert code == 0
    remaining = config.read_text()
    assert "forseti codex-hook" not in remaining
    assert 'notify = ["python3", "/x/notify.py"]' in remaining
    assert "removed" in capsys.readouterr().out


def test_disable_project_claude_code_removes_only_forseti_entries(
    tmp_path: Path,
) -> None:
    main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    settings_path = tmp_path / ".claude" / "settings.local.json"
    existing = json.loads(settings_path.read_text())
    existing["permissions"] = {"allow": ["Bash(git *)"]}
    settings_path.write_text(json.dumps(existing))

    code = main(["disable-project", "--harness", "claude-code", str(tmp_path)])
    assert code == 0
    remaining = json.loads(settings_path.read_text())
    assert remaining["permissions"] == {"allow": ["Bash(git *)"]}
    for groups in remaining["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert not hook["command"].startswith("forseti claude-code-hook ")


def test_disable_project_codex_does_not_touch_claude_settings(
    tmp_path: Path,
) -> None:
    main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    main(["enable-project", "--harness", "codex", str(tmp_path)])
    claude_before = (tmp_path / ".claude" / "settings.local.json").read_text()

    code = main(["disable-project", "--harness", "codex", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".claude" / "settings.local.json").read_text() == claude_before
    assert "forseti codex-hook" not in (tmp_path / ".codex" / "config.toml").read_text()


def test_disable_project_claude_code_does_not_touch_codex_config(
    tmp_path: Path,
) -> None:
    main(["enable-project", "--harness", "claude-code", str(tmp_path)])
    main(["enable-project", "--harness", "codex", str(tmp_path)])
    codex_before = (tmp_path / ".codex" / "config.toml").read_text()

    code = main(["disable-project", "--harness", "claude-code", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".codex" / "config.toml").read_text() == codex_before
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert not hook["command"].startswith("forseti claude-code-hook ")


def test_disable_project_codex_shared_flag_errors(tmp_path: Path) -> None:
    code = main(["disable-project", "--harness", "codex", str(tmp_path), "--shared"])
    assert code == 1


def test_disable_project_on_absent_project_is_noop(tmp_path: Path) -> None:
    code = main(["disable-project", "--harness", "codex", str(tmp_path)])
    assert code == 0
    assert not (tmp_path / ".codex").exists()
