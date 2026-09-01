"""Forseti Core's canonical lifecycle events -- one trace format, every harness (#213).

Each harness adapter has its own local trigger/gate trace (e.g. Claude Code's
`adapters.claude_code.event_log`, keyed by `edit`/`verify`/`gate`/`stop`/`session`).
Those stay -- they are trigger metadata specific to that harness's hook shape --
but the *Core* operations that do the actual semantic-loop work (propose, submit,
check) must emit the same handful of event types regardless of which harness
called them, so a trace is comparable across Claude Code and Codex rather than
becoming three incompatible formats.

`record_event` appends one JSON line to `<store_root>/events.jsonl` -- the same
`.forseti/events.jsonl` the Claude Code adapter's own trace already writes to
when `store_root` is the project's default `.forseti` -- so a project's whole
loop history (adapter-local trigger events interleaved with Core's own) lives in
one append-only file. Best-effort and never raises: a trace failure must not
turn a verdict into an error (mirrors `adapters.claude_code.event_log.log_event`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_EVENTS_FILE = "events.jsonl"

# Canonical event-type vocabulary (the `type` field). Dotted names distinguish
# these from an adapter's own short local names (`edit`, `gate`, `stop`, ...) at
# a glance in a shared events.jsonl.
PROPERTY_PROPOSED = "property.proposed"
PROPERTY_CHECK_START = "property.check.start"
PROPERTY_VERDICT = "property.verdict"
GATE_DECISION = "gate.decision"


def events_path(store_root: Path) -> Path:
    """The trace file for a store root: ``<store_root>/events.jsonl``."""
    return store_root / _EVENTS_FILE


def record_event(store_root: Path, event_type: str, **fields: Any) -> None:
    """Append one canonical lifecycle event; never raise into the caller.

    Stamps a wall-clock ``ts`` (epoch seconds) and ``type``, then writes the
    JSON object as a single line (one ``O_APPEND`` syscall of a below-``PIPE_BUF``
    line, so concurrent writers interleave whole lines rather than corrupting
    each other -- mirrors the Claude Code adapter's own event log). All I/O and
    serialization errors are swallowed: observability must never turn a
    successful propose/check/gate outcome into a crash.
    """
    event = {"ts": time.time(), "type": event_type, **fields}
    try:
        path = events_path(store_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass
