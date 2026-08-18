"""Tests for `forseti enable-project`'s merge/install logic (RFC-0004).

Hermetic and pure where possible: `merge_hooks` takes/returns plain dicts, no
I/O. `install` is exercised against `tmp_path`, never a real project.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from forseti.adapters.claude_code import install as install_module
from forseti.adapters.claude_code.install import (
    InstallOutcome,
    ProjectSettingsError,
    install,
    merge_hooks,
)

_MARKER = install_module._MARKER_PREFIX


def test_merge_hooks_on_empty_settings_adds_all_four_events() -> None:
    merged = merge_hooks({})
    assert set(merged["hooks"]) == {"SessionStart", "PostToolUse", "Stop"}
    assert len(merged["hooks"]["PostToolUse"]) == 2  # Write|Edit|MultiEdit + Bash
    for groups in merged["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"].startswith(_MARKER)


def test_hook_specs_match_the_plugin_manifest() -> None:
    # install.py's `_HOOK_SPECS` is a hand-kept mirror of the plugin manifest
    # (its own comment says so); this pins the two together so one can drift
    # from the other only if this test is also updated.
    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "adapters"
        / "claude-code"
        / "hooks"
        / "hooks.json"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["hooks"] == install_module._generated_matcher_groups()


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


@pytest.mark.parametrize(
    "hook_name", ["session_start", "post_tool_use", "post_bash", "stop_gate"]
)
@pytest.mark.parametrize(
    "path_prefix", ["/home/user/adapters/claude-code", "${CLAUDE_PLUGIN_ROOT}"]
)
def test_merge_hooks_drops_legacy_pre_rfc0004_commands(
    hook_name: str, path_prefix: str
) -> None:
    # review feedback on PR #201: the pre-RFC-0004 manual/plugin install
    # (`python3 "<path>/hooks/<name>.py"`) invoked scripts this PR deletes --
    # left wired up, they're a dead command Claude Code still fires every
    # turn. Recognized and dropped like forseti's own marker.
    existing = {
        "hooks": {
            "Stop" if hook_name == "stop_gate" else "SessionStart": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'python3 "{path_prefix}/hooks/{hook_name}.py"',
                            "timeout": 60,
                        }
                    ],
                }
            ]
        }
    }
    merged = merge_hooks(existing)
    event = "Stop" if hook_name == "stop_gate" else "SessionStart"
    commands = [h["command"] for g in merged["hooks"][event] for h in g["hooks"]]
    assert not any("hooks/" in c and ".py" in c for c in commands)
    assert all(c.startswith(_MARKER) for c in commands)


def test_merge_hooks_preserves_a_command_merely_resembling_the_legacy_shape() -> None:
    # The legacy pattern is anchored to forseti's own four hook basenames --
    # an unrelated tool's `python3 ".../hooks/other_script.py"` is not forseti's
    # to touch.
    other_command = 'python3 "/opt/other-tool/hooks/other_script.py"'
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": other_command, "timeout": 30}
                    ],
                }
            ]
        }
    }
    merged = merge_hooks(existing)
    commands = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert other_command in commands


def test_merge_hooks_preserves_a_foreign_hook_sharing_a_legacy_basename() -> None:
    # review feedback on PR #201: the legacy pattern must not match on hook
    # basename alone -- an unrelated tool's own `hooks/session_start.py`,
    # under its own unrelated path, is not forseti's to touch even though it
    # shares one of forseti's four basenames.
    foreign_command = 'python3 "/opt/another-plugin/hooks/session_start.py"'
    existing = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": foreign_command, "timeout": 30}
                    ],
                }
            ]
        }
    }
    merged = merge_hooks(existing)
    commands = [
        h["command"] for g in merged["hooks"]["SessionStart"] for h in g["hooks"]
    ]
    assert foreign_command in commands


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


def test_merge_hooks_preserves_a_matcher_group_with_no_forseti_hooks() -> None:
    # review feedback on PR #201: a group with zero forseti hooks in it (empty
    # or omitted `hooks`) must survive untouched -- `remaining` being empty
    # there is not the same as "forseti's removal emptied it".
    existing = {
        "hooks": {
            "Stop": [
                {"matcher": "SomeOtherTool", "hooks": []},
                {"matcher": "AnotherTool"},  # `hooks` omitted entirely
            ]
        }
    }
    merged = merge_hooks(existing)
    stop_groups = merged["hooks"]["Stop"]
    assert {"matcher": "SomeOtherTool", "hooks": []} in stop_groups
    assert {"matcher": "AnotherTool"} in stop_groups
    forseti_groups = [g for g in stop_groups if g.get("matcher") == "*"]
    assert len(forseti_groups) == 1


def test_merge_hooks_preserves_a_non_dict_matcher_group_verbatim() -> None:
    # A matcher-group *entry* that isn't itself a dict is malformed but not
    # forseti's problem to fix -- it survives untouched, alongside forseti's own
    # freshly generated group for the same event.
    existing = {"hooks": {"Stop": ["not-a-dict-group"]}}
    merged = merge_hooks(existing)
    assert "not-a-dict-group" in merged["hooks"]["Stop"]
    assert len(merged["hooks"]["Stop"]) == 2


def test_merge_hooks_preserves_a_non_dict_individual_hook_verbatim() -> None:
    # A *hook* entry (inside a group's "hooks" list) that isn't itself a dict
    # is malformed but not forseti's own -- the group survives untouched,
    # alongside forseti's own freshly generated group for the same event.
    existing = {"hooks": {"Stop": [{"matcher": "*", "hooks": ["not-a-dict-hook"]}]}}
    merged = merge_hooks(existing)
    stop_groups = merged["hooks"]["Stop"]
    assert {"matcher": "*", "hooks": ["not-a-dict-hook"]} in stop_groups
    assert len(stop_groups) == 2


def test_merge_hooks_preserves_a_hook_with_a_non_string_command() -> None:
    # A hook dict missing (or with a non-string) "command" can't be forseti's
    # own marker-carrying entry -- preserve it as-is.
    existing = {"hooks": {"Stop": [{"matcher": "*", "hooks": [{"type": "command"}]}]}}
    merged = merge_hooks(existing)
    assert {"type": "command"} in merged["hooks"]["Stop"][0]["hooks"]


def test_merge_hooks_partial_removal_keeps_the_foreign_hook_in_place() -> None:
    # A single matcher group holding both a stale forseti hook and an
    # unrelated one: only forseti's own is stripped, the rest of the group
    # (its foreign hook, matcher) stays -- not split into a separate group.
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo mine", "timeout": 5},
                        {
                            "type": "command",
                            "command": f"{_MARKER}stop-gate",
                            "timeout": 1,
                        },
                    ],
                }
            ]
        }
    }
    merged = merge_hooks(existing)
    stop_groups = merged["hooks"]["Stop"]
    mine_group = [
        g
        for g in stop_groups
        if any(h.get("command") == "echo mine" for h in g["hooks"])
    ]
    assert mine_group == [
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": "echo mine", "timeout": 5}],
        }
    ]
    forseti_groups = [g for g in stop_groups if g is not mine_group[0]]
    assert len(forseti_groups) == 1
    assert forseti_groups[0]["hooks"][0]["command"] == f"{_MARKER}stop-gate"


def test_merge_hooks_on_null_hooks_raises() -> None:
    with pytest.raises(ProjectSettingsError):
        merge_hooks({"hooks": None})


def test_merge_hooks_on_non_list_event_value_raises() -> None:
    with pytest.raises(ProjectSettingsError):
        merge_hooks({"hooks": {"Stop": {"matcher": "*"}}})


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


def test_install_rerun_with_an_empty_foreign_group_stays_unchanged(
    tmp_path: Path,
) -> None:
    # Companion to test_merge_hooks_preserves_a_matcher_group_with_no_forseti_hooks:
    # preserving that group *by identity* (not a rebuilt dict) matters for
    # install()'s own idempotency, since `updated == existing` is a plain
    # dict comparison -- materializing an unwanted "hooks": [] key on an
    # omitted-`hooks` group would make every rerun report UPDATED forever.
    settings_path, _ = install(tmp_path)
    data = json.loads(settings_path.read_text())
    # Inserted *before* forseti's own group: a rerun always re-appends
    # forseti's fresh groups at the end regardless of where they started
    # (unrelated pre-existing behavior), so this is the list shape a rerun
    # itself would reproduce -- the fixed point this test is checking for.
    data["hooks"]["Stop"].insert(0, {"matcher": "SomeOtherTool"})
    settings_path.write_text(json.dumps(data))

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


def test_install_rerun_preserves_a_restrictive_existing_mode(tmp_path: Path) -> None:
    # review feedback on PR #201: an atomic write installs a *new* inode
    # (temp file + rename), so without copying the old file's mode across, a
    # settings file the user `chmod 600`'d -- it can hold a Claude Code `env`
    # block with API keys -- would silently widen to the umask default.
    settings_path, _ = install(tmp_path)
    data = json.loads(settings_path.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1  # force a real rewrite
    settings_path.write_text(json.dumps(data))
    os.chmod(settings_path, 0o600)

    _, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UPDATED
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


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


def test_install_on_non_utf8_bytes_raises_project_settings_error(
    tmp_path: Path,
) -> None:
    # review feedback on PR #201: read_text() with no explicit encoding falls
    # back to the locale codec; a byte sequence that codec rejects must still
    # hit the documented ProjectSettingsError contract, not an uncaught
    # UnicodeDecodeError (a ValueError subclass json.JSONDecodeError alone
    # doesn't catch).
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_bytes(b'{"key": "\xff\xfe not valid utf-8"}')

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)


def test_install_preserves_non_ascii_content_in_existing_settings(
    tmp_path: Path,
) -> None:
    settings_path, _ = install(tmp_path)
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["permissions"] = {"note": "café — 日本語"}
    settings_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    _, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UNCHANGED
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restored["permissions"] == {"note": "café — 日本語"}


def test_install_on_non_object_json_raises(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text("[1, 2, 3]")

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)


def test_install_rejects_a_symlinked_settings_file(tmp_path: Path) -> None:
    # review feedback on PR #201: an atomic rewrite (temp file + rename)
    # replaces whatever is *at* the target path -- if that's a symlink (e.g.
    # into a dotfiles repo), the link itself is silently swapped for a plain
    # file rather than writing through it. Reject explicitly instead.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    real = tmp_path / "real-settings.json"
    real.write_text("{}")
    (claude_dir / "settings.local.json").symlink_to(real)

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)
    assert real.read_text() == "{}"  # untouched


def test_install_rejects_a_broken_symlinked_settings_file(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").symlink_to(tmp_path / "does-not-exist.json")

    with pytest.raises(ProjectSettingsError):
        install(tmp_path)
