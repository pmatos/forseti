"""Tests for `forseti enable-project`'s merge/install logic (RFC-0004).

Hermetic and pure where possible: `merge_hooks` takes/returns plain dicts, no
I/O. `install` is exercised against `tmp_path`, never a real project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forseti.adapters.claude_code.install import (
    InstallOutcome,
    ProjectSettingsError,
    install,
    merge_hooks,
)

_MARKER = "forseti claude-code-hook "


def test_merge_hooks_on_empty_settings_adds_all_four_events() -> None:
    merged = merge_hooks({})
    assert set(merged["hooks"]) == {"SessionStart", "PostToolUse", "Stop"}
    assert len(merged["hooks"]["PostToolUse"]) == 2  # Write|Edit|MultiEdit + Bash
    for groups in merged["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"].startswith(_MARKER)


def test_merge_hooks_preserves_unrelated_keys_and_hooks() -> None:
    existing = {
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [
                        {"type": "command", "command": "echo mine", "timeout": 5}
                    ],
                }
            ]
        },
    }
    merged = merge_hooks(existing)
    assert merged["permissions"] == {"allow": ["Bash(git *)"]}
    # the user's own PostToolUse matcher survives untouched...
    own = [g for g in merged["hooks"]["PostToolUse"] if g["matcher"] == "Write"]
    assert own == [
        {
            "matcher": "Write",
            "hooks": [{"type": "command", "command": "echo mine", "timeout": 5}],
        }
    ]
    # ...alongside forseti's own two PostToolUse matchers.
    forseti_groups = [
        g for g in merged["hooks"]["PostToolUse"] if g["matcher"] != "Write"
    ]
    assert len(forseti_groups) == 2


def test_merge_hooks_rerun_replaces_stale_entries_without_duplicating() -> None:
    once = merge_hooks({})
    twice = merge_hooks(once)
    assert twice == once


def test_merge_hooks_drops_matcher_group_left_empty_by_removal() -> None:
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{_MARKER}stop-gate",
                            "timeout": 1,
                        }
                    ],
                }
            ]
        }
    }
    merged = merge_hooks(existing)
    # exactly one Stop matcher-group survives: the freshly generated one, not a
    # leftover empty group from stripping the stale entry.
    assert len(merged["hooks"]["Stop"]) == 1
    assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == f"{_MARKER}stop-gate"


def test_install_on_fresh_project_creates_settings_local(tmp_path: Path) -> None:
    settings_path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.CREATED
    assert settings_path == tmp_path / ".claude" / "settings.local.json"
    data = json.loads(settings_path.read_text())
    assert "SessionStart" in data["hooks"]


def test_install_shared_writes_settings_json(tmp_path: Path) -> None:
    settings_path, outcome = install(tmp_path, shared=True)
    assert outcome is InstallOutcome.CREATED
    assert settings_path == tmp_path / ".claude" / "settings.json"


def test_install_rerun_is_unchanged(tmp_path: Path) -> None:
    install(tmp_path)
    _, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UNCHANGED


def test_install_rerun_after_stale_edit_reports_updated(tmp_path: Path) -> None:
    settings_path, _ = install(tmp_path)
    data = json.loads(settings_path.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1
    settings_path.write_text(json.dumps(data))

    _, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UPDATED
    restored = json.loads(settings_path.read_text())
    assert restored["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 120


def test_install_on_malformed_json_raises_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text("not json")

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)
    assert settings_path.read_text() == "not json"


def test_install_on_non_object_json_raises(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text("[1, 2, 3]")

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)
