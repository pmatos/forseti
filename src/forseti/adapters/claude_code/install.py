"""`forseti enable-project`: install/update the Claude Code verify-gate hooks.

RFC-0004. Generates the four SessionStart/PostToolUse/Stop hook entries (matching
`adapters/claude-code/hooks/hooks.json`'s plugin manifest) and merges them into
a target project's Claude Code settings file. Every hook entry `forseti` writes
carries a `"forseti claude-code-hook "`-prefixed command, its ownership marker:
`merge_hooks` uses that marker to replace only forseti's own prior entries on a
rerun, leaving any other hook/key in the file untouched. There is no separate
version marker -- rerunning always regenerates forseti's entries from whatever
`forseti` is currently installed, so it is idempotent by construction.

Also recognizes (and drops, same as its own marker) the pre-RFC-0004 manual
install commands -- `python3 "<path>/hooks/<name>.py"`, hand-copied from the
old README or plugin manifest before the hook scripts moved into this
package -- so a project that adopted forseti before `enable-project` existed
doesn't keep invoking a script this relocation deletes.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from . import event_log

_MARKER_PREFIX = "forseti claude-code-hook "

# Anchored to the only two path forms forseti's own pre-PR README/plugin
# manifest ever actually used -- not an arbitrary `.../hooks/<name>.py`, which
# would misclassify (and silently delete) an unrelated tool's own hook that
# happens to share one of forseti's hook basenames under its own `hooks/` dir.
_LEGACY_HOOK_RE = re.compile(
    r'^python3\s+"(?:\$\{CLAUDE_PLUGIN_ROOT\}|[^"]*/adapters/claude-code)/hooks/'
    r"(?:session_start|post_tool_use|post_bash|stop_gate)"
    r'\.py"$'
)

# (event, matcher, hook name, timeout_s) -- mirrors claude-code/hooks/hooks.json
# (checked equal by test_install.py), the plugin manifest for these same four
# hooks. `HOOK_NAMES` below is core/cli.py's single source for the
# `claude-code-hook <name>` argparse choices (checked by
# test_core_cli_dispatch.py), so the in-process dispatcher can't drift either.
_HOOK_SPECS: tuple[tuple[str, str, str, int], ...] = (
    ("SessionStart", "*", "session-start", 60),
    ("PostToolUse", "Write|Edit|MultiEdit", "post-tool-use", 300),
    ("PostToolUse", "Bash", "post-bash", 300),
    ("Stop", "*", "stop-gate", 120),
)

HOOK_NAMES: frozenset[str] = frozenset(spec[2] for spec in _HOOK_SPECS)


class InstallOutcome(Enum):
    """What `install` did to the target settings file (`.value` is CLI-facing prose)."""

    CREATED = "installed"
    UPDATED = "updated"
    UNCHANGED = "already up to date"


class RemoveOutcome(Enum):
    """What `remove` did to the target settings file (`.value` is CLI-facing prose)."""

    REMOVED = "removed"
    UNCHANGED = "already absent"


class ProjectSettingsError(Exception):
    """The target settings file exists but cannot be safely read as forseti's own."""


def _hook_entry(hook_name: str, timeout_s: int) -> dict[str, Any]:
    return {
        "type": "command",
        "command": f"{_MARKER_PREFIX}{hook_name}",
        "timeout": timeout_s,
    }


def _is_forseti_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    return command.startswith(_MARKER_PREFIX) or bool(_LEGACY_HOOK_RE.match(command))


def _generated_matcher_groups() -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, hook_name, timeout_s in _HOOK_SPECS:
        groups.setdefault(event, []).append(
            {"matcher": matcher, "hooks": [_hook_entry(hook_name, timeout_s)]}
        )
    return groups


def _validate_hooks_shape(raw_hooks: object) -> dict[str, Any]:
    if not isinstance(raw_hooks, dict):
        raise ProjectSettingsError('existing settings\' "hooks" key must be an object')
    hooks: dict[str, Any] = dict(raw_hooks)
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ProjectSettingsError(
                f'existing settings\' "hooks"."{event}" must be a list'
            )
    return hooks


def _strip_forseti_hooks(hooks: dict[str, Any]) -> dict[str, Any]:
    """Return `hooks` with every forseti-owned hook entry removed.

    Total over the shape Claude Code itself writes (a dict mapping event name
    to a list of matcher groups): every non-forseti hook, matcher, and event
    survives unchanged; a matcher-group is dropped only when removing
    forseti's own hooks from it is what left it with none (an already-empty
    or -omitted `hooks` array is preserved as-is, not read as "all forseti's
    own" and dropped).
    """
    stripped: dict[str, Any] = {}
    for event, groups in hooks.items():
        kept: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            original = group.get("hooks", [])
            if not isinstance(original, list):
                # Malformed foreign group ("hooks": null or similar) -- not
                # forseti's shape to fix; preserve it untouched, same as a
                # non-dict group above, rather than crash iterating it.
                kept.append(group)
                continue
            remaining = [h for h in original if not _is_forseti_hook(h)]
            if len(remaining) == len(original):
                # No forseti hook was in this group at all -- preserve it
                # exactly (not a rebuilt dict), so an omitted `hooks` key or
                # any other extra field on it survives untouched too.
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": remaining})
            # else: every hook in this group was forseti's own -- drop it.
        stripped[event] = kept
    return stripped


def merge_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    """Return `existing` with forseti's own hook entries replaced by the current set.

    Total over any `existing` whose `"hooks"` value has the shape Claude Code
    itself writes (absent, or a dict mapping event name to a list of matcher
    groups): every non-forseti hook, matcher, and top-level key survives
    unchanged (see `_strip_forseti_hooks`). Raises `ProjectSettingsError` if
    `"hooks"` or any event's value doesn't have that shape, rather than
    silently dropping/corrupting it.
    """
    merged = dict(existing)
    hooks = _validate_hooks_shape(merged.get("hooks", {}))
    generated = _generated_matcher_groups()

    stripped = _strip_forseti_hooks(hooks)
    for event in set(hooks) | set(generated):
        kept = list(stripped.get(event, []))
        kept.extend(generated.get(event, []))
        stripped[event] = kept

    merged["hooks"] = stripped
    return merged


def _settings_path(project_dir: Path, *, shared: bool) -> Path:
    name = "settings.json" if shared else "settings.local.json"
    return project_dir / ".claude" / name


def install(project_dir: Path, *, shared: bool = False) -> tuple[Path, InstallOutcome]:
    """Install/update the Claude Code verify-gate hooks for `project_dir`.

    Writes `.claude/settings.json` when `shared` (git-committed, team-wide) else
    the default `.claude/settings.local.json` (gitignored, personal). Raises
    `ProjectSettingsError` on unreadable/malformed existing JSON rather than
    overwriting it. The write is atomic (temp file + rename), which -- if the
    target were a symlink (e.g. into a dotfiles repo) -- would silently
    replace the link itself with a plain file rather than writing through it;
    raises `ProjectSettingsError` instead of doing that.
    """
    settings_path = _settings_path(project_dir, shared=shared)
    if settings_path.is_symlink():
        raise ProjectSettingsError(
            f"{settings_path} is a symlink -- refusing to replace it (an atomic "
            "rewrite would swap in a plain file, silently breaking the link)"
        )
    existed = settings_path.exists()

    if existed:
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError, not just json.JSONDecodeError: a byte sequence the
            # locale codec would otherwise misdecode into mojibake instead
            # raises UnicodeDecodeError here (also a ValueError subclass),
            # so it hits this same honest-error path rather than escaping it.
            raise ProjectSettingsError(
                f"{settings_path}: cannot read existing settings ({exc})"
            ) from exc
        if not isinstance(existing, dict):
            raise ProjectSettingsError(
                f"{settings_path}: top-level JSON must be an object"
            )
    else:
        existing = {}

    updated = merge_hooks(existing)
    if updated == existing:
        return settings_path, InstallOutcome.UNCHANGED

    event_log.write_text_atomic(settings_path, json.dumps(updated, indent=2) + "\n")
    outcome = InstallOutcome.CREATED if not existed else InstallOutcome.UPDATED
    return settings_path, outcome


def remove(project_dir: Path, *, shared: bool = False) -> tuple[Path, RemoveOutcome]:
    """Remove forseti's own hook entries from `project_dir`'s Claude Code settings.

    No-ops -- and never creates the settings file -- if it is absent or carries
    no forseti-owned hook entries. Leaves every other hook/key untouched (the
    migration cleanup path for a project that installed the wrong harness).
    """
    settings_path = _settings_path(project_dir, shared=shared)
    if settings_path.is_symlink():
        raise ProjectSettingsError(
            f"{settings_path} is a symlink -- refusing to replace it (an atomic "
            "rewrite would swap in a plain file, silently breaking the link)"
        )
    if not settings_path.exists():
        return settings_path, RemoveOutcome.UNCHANGED

    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectSettingsError(
            f"{settings_path}: cannot read existing settings ({exc})"
        ) from exc
    if not isinstance(existing, dict):
        raise ProjectSettingsError(f"{settings_path}: top-level JSON must be an object")

    merged = dict(existing)
    hooks = _validate_hooks_shape(merged.get("hooks", {}))
    merged["hooks"] = _strip_forseti_hooks(hooks)
    if merged == existing:
        return settings_path, RemoveOutcome.UNCHANGED

    event_log.write_text_atomic(settings_path, json.dumps(merged, indent=2) + "\n")
    return settings_path, RemoveOutcome.REMOVED
