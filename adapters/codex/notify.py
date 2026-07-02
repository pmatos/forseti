#!/usr/bin/env python3
"""Forseti — Codex `notify` reference hook (a turn-end reminder).

The enforcing gate is the `PostToolUse` hook (./verify_hook.py), which blocks on
a counterexample. This `notify` script is a secondary backstop: it fires at
`agent-turn-complete`, *after* the turn, so it cannot block a hand-off — it just
records a reminder to confirm every changed unit is VERIFIED, covering edits the
`PostToolUse` hook can't see (e.g. raw shell writes rather than `apply_patch`).

Wire it in ~/.codex/config.toml (a top-level key — see ./config.toml.example):

    notify = ["python3", "/absolute/path/to/adapters/codex/notify.py"]

Codex passes one JSON arg with `type` (currently only "agent-turn-complete"),
`thread-id`, `turn-id`, `cwd`, `input-messages`, and `last-assistant-message`.

Codex runs `notify` fire-and-forget and does **not** surface the program's
stdout/stderr in its TUI, so a reminder must go somewhere durable. This script
therefore (a) appends the reminder to `<cwd>/.forseti/notify.log`, (b) makes a
best-effort desktop notification via a notifier on PATH (notify-send / osascript
/ terminal-notifier), and (c) still writes to stderr as a last resort. Every step
is best-effort: any error still exits 0 so a broken notify config cannot wedge
Codex.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Words that suggest the agent is declaring completion — the moment the emulated
# Stop-gate most needs a human to confirm every changed unit is VERIFIED up to k.
_DONE_HINTS = ("done", "complete", "finished", "ready", "all set", "lgtm")


def _log(cwd: str, message: str) -> None:
    """Append the reminder to the per-project store; never raise."""
    try:
        log_dir = Path(cwd or ".") / ".forseti"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "notify.log").open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except OSError:
        pass


def _desktop_notify(message: str) -> None:
    """Best-effort desktop notification via whatever notifier is on PATH."""
    cmd: list[str] | None = None
    if shutil.which("notify-send"):
        cmd = ["notify-send", "Forseti", message]
    elif shutil.which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", "Forseti", "-message", message]
    elif shutil.which("osascript"):
        script = f'display notification {json.dumps(message)} with title "Forseti"'
        cmd = ["osascript", "-e", script]
    if cmd is None:
        return
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _emit(cwd: str, message: str) -> None:
    _log(cwd, message)
    _desktop_notify(message)
    print(message, file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        event = json.loads(argv[1])
    except (ValueError, TypeError):
        return 0
    if not isinstance(event, dict) or event.get("type") != "agent-turn-complete":
        return 0

    cwd = str(event.get("cwd") or ".")
    last = str(event.get("last-assistant-message") or "").lower()

    _emit(
        cwd,
        "[forseti] Codex turn complete — the PostToolUse verify hook is the "
        "enforcement gate; this reminder covers edits it can't see (e.g. "
        "non-apply_patch shell writes).",
    )
    if any(hint in last for hint in _DONE_HINTS):
        _emit(
            cwd,
            "[forseti] The turn reads as 'done'. Confirm every changed unit is "
            "VERIFIED up to k (no VIOLATED, no UNKNOWN) before trusting it.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
