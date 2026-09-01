"""Tests for `forseti enable-project --harness codex`'s merge/install logic (#212).

Hermetic: `merge_config` takes/returns plain text, no I/O. `install`/`remove` are
exercised against `tmp_path`, never a real project.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from forseti.adapters.codex import install as install_module
from forseti.adapters.codex.install import (
    InstallOutcome,
    ProjectConfigError,
    RemoveOutcome,
    install,
    merge_config,
    remove,
)

_MARKER = install_module._MARKER_PREFIX


def test_merge_config_on_empty_file_adds_managed_block() -> None:
    merged = merge_config("", Path("config.toml"))
    parsed = tomllib.loads(merged)
    hooks = parsed["hooks"]["PostToolUse"]
    assert len(hooks) == 1
    assert hooks[0]["matcher"] == "apply_patch"
    command = hooks[0]["hooks"][0]["command"]
    assert command.startswith(_MARKER)
    assert "notify" not in parsed


def test_merge_config_preserves_unrelated_content() -> None:
    existing = (
        'notify = ["python3", "/x/notify.py"]\n\n'
        "[mcp_servers.forseti]\n"
        'command = "forseti"\n'
        'args = ["mcp"]\n'
    )
    merged = merge_config(existing, Path("config.toml"))
    assert merged.startswith(existing)
    parsed = tomllib.loads(merged)
    assert parsed["notify"] == ["python3", "/x/notify.py"]
    assert parsed["mcp_servers"]["forseti"]["command"] == "forseti"
    assert parsed["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch"


def test_merge_config_preserves_foreign_apply_patch_hook() -> None:
    existing = (
        "[[hooks.PostToolUse]]\n"
        'matcher = "apply_patch"\n\n'
        "[[hooks.PostToolUse.hooks]]\n"
        'type = "command"\n'
        'command = "some-other-tool verify"\n'
        "timeout = 30\n"
    )
    merged = merge_config(existing, Path("config.toml"))
    parsed = tomllib.loads(merged)
    commands = {
        h["command"] for g in parsed["hooks"]["PostToolUse"] for h in g["hooks"]
    }
    assert "some-other-tool verify" in commands
    assert any(c.startswith(_MARKER) for c in commands)


def test_merge_config_is_idempotent() -> None:
    first = merge_config("", Path("config.toml"))
    second = merge_config(first, Path("config.toml"))
    assert first == second


def test_merge_config_converges_from_content_without_trailing_newline() -> None:
    # Real-world files rarely end in exactly the two newlines `merge_config`
    # itself would produce; confirm it still reaches a fixed point rather than
    # perpetually reformatting the blank-line separator.
    existing = 'notify = ["python3", "/x/notify.py"]'  # no trailing newline
    first = merge_config(existing, Path("config.toml"))
    second = merge_config(first, Path("config.toml"))
    assert first == second


def test_merge_config_rejects_hand_edited_duplicate_marker() -> None:
    existing = (
        "[[hooks.PostToolUse]]\n"
        'matcher = "apply_patch"\n\n'
        "[[hooks.PostToolUse.hooks]]\n"
        'type = "command"\n'
        f'command = "{_MARKER}verify"\n'
        "timeout = 60\n"
    )
    with pytest.raises(ProjectConfigError, match="hand-edited"):
        merge_config(existing, Path("config.toml"))


def test_merge_config_rejects_output_that_would_not_parse() -> None:
    # `hooks` already bound to an inline table -- appending our own
    # `[[hooks.PostToolUse]]` header afterward is an illegal redefinition.
    existing = "hooks = { PostToolUse = [] }\n"
    with pytest.raises(ProjectConfigError):
        merge_config(existing, Path("config.toml"))


def test_merge_config_refuses_sentinel_copied_into_toml_string() -> None:
    # A copied managed block hand-pasted into an unrelated multiline string
    # value (e.g. a prompt/instructions field) still contains the sentinel
    # lines verbatim. `tomllib` sees them as string data, not comments --
    # blindly stripping them as our own block would silently delete part of
    # that string. Refuse instead of guessing.
    block = install_module._managed_block()
    existing = f'instructions = """\n{block}"""\n'
    with pytest.raises(ProjectConfigError, match="string value"):
        merge_config(existing, Path("config.toml"))


def test_merge_config_refuses_unpaired_start_sentinel() -> None:
    # A stray START with no matching END -- e.g. a half-reverted hand edit --
    # would otherwise make `_BLOCK_RE`'s non-greedy match run from this START
    # through whatever END comes *next*, silently deleting real content in
    # between (#250 review). Refuse rather than guess.
    existing = f"{install_module._SENTINEL_START}\nnotify = 1\n"
    with pytest.raises(ProjectConfigError, match="not one well-formed pair"):
        merge_config(existing, Path("config.toml"))


def test_merge_config_refuses_stray_start_before_a_real_block() -> None:
    # The exact failure mode reported: a stray START sitting before an
    # unrelated legitimate-looking END would let `_strip_managed_block`
    # consume everything between them, including foreign TOML content.
    existing = (
        "notify = 1\n"
        + install_module._SENTINEL_START
        + "\n"
        + "important = true\n"
        + "also_important = 42\n"
        + install_module._managed_block()
    )
    with pytest.raises(ProjectConfigError, match="not one well-formed pair"):
        merge_config(existing, Path("config.toml"))


def test_install_creates_fresh_config(tmp_path: Path) -> None:
    path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.CREATED
    assert path == tmp_path / ".codex" / "config.toml"
    assert path.exists()


def test_install_rerun_reports_already_up_to_date(tmp_path: Path) -> None:
    install(tmp_path)
    _path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UNCHANGED


def test_install_on_existing_config_reports_updated(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('notify = ["python3", "/x/notify.py"]\n')
    path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UPDATED
    assert 'notify = ["python3", "/x/notify.py"]' in path.read_text()


def test_install_on_malformed_toml_raises(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("this is not [valid toml")
    with pytest.raises(ProjectConfigError):
        install(tmp_path)


def test_install_on_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.toml"
    real.write_text("")
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        install(tmp_path)


def test_remove_on_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.toml"
    real.write_text('notify = ["python3", "/x/notify.py"]\n')
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        remove(tmp_path)
    assert real.is_file()
    assert not real.is_symlink()


def test_remove_on_broken_symlink_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").symlink_to(tmp_path / "missing.toml")
    with pytest.raises(ProjectConfigError, match="symlink"):
        remove(tmp_path)


def test_remove_refuses_sentinel_copied_into_toml_string(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    block = install_module._managed_block()
    config.write_text(f'instructions = """\n{block}"""\n')
    with pytest.raises(ProjectConfigError, match="string value"):
        remove(tmp_path)


def test_remove_refuses_stray_start_before_a_real_block(tmp_path: Path) -> None:
    # Same failure mode as merge_config's: a stray START sitting before an
    # unrelated legitimate-looking END must not let `remove` silently delete
    # the foreign content between them (#250 review).
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "notify = 1\n"
        + install_module._SENTINEL_START
        + "\n"
        + "important = true\n"
        + "also_important = 42\n"
        + install_module._managed_block()
    )
    with pytest.raises(ProjectConfigError, match="not one well-formed pair"):
        remove(tmp_path)


def test_remove_on_missing_project_is_noop_and_creates_nothing(
    tmp_path: Path,
) -> None:
    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.UNCHANGED
    assert not path.exists()


def test_remove_strips_only_managed_block(tmp_path: Path) -> None:
    install(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    original = config.read_text()
    unrelated = 'notify = ["python3", "/x/notify.py"]\n\n'
    config.write_text(unrelated + original)

    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.REMOVED
    remaining = path.read_text()
    assert remaining == 'notify = ["python3", "/x/notify.py"]\n'
    assert _MARKER not in remaining


def test_install_then_remove_restores_original_content_exactly(
    tmp_path: Path,
) -> None:
    # install/remove must round-trip byte-for-byte: the blank-line separator
    # `merge_config` inserts before its own block is not left behind as a
    # remove-time artifact.
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = 'notify = ["python3", "/x/notify.py"]\n'
    config.write_text(original)

    install(tmp_path)
    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.REMOVED
    assert path.read_text() == original


def test_remove_preserves_extra_trailing_newlines_beyond_separator(
    tmp_path: Path,
) -> None:
    # `merge_config` never strips existing newlines, only pads up to a
    # two-newline separator -- so three or more trailing newlines in the
    # original can only have come from the file itself, and `remove` must
    # leave them exactly as they were rather than collapsing to one (#250
    # review).
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = 'notify = ["python3", "/x/notify.py"]\n\n\n'
    config.write_text(original)

    install(tmp_path)
    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.REMOVED
    assert path.read_text() == original


def test_remove_again_reports_unchanged(tmp_path: Path) -> None:
    install(tmp_path)
    remove(tmp_path)
    _path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.UNCHANGED
