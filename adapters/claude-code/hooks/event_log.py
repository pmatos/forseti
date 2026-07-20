"""Append-only JSONL trace of the Forseti verify-gate loop (issue #15, adapter-local).

Where ``gate_state.json`` is a *latest-verdict snapshot* (one entry per unit,
overwritten each verify), this is the *ordered history* of the whole
write -> verify -> counterexample -> fix back-and-forth: every PostToolUse firing
(what Claude wrote), every ESBMC call (unit, verdict, k, duration, full argv),
every gate decision (fed the cex back / let it pass), and every Stop-gate
decision (block / loud residual / allow). One JSON object per line, appended to
``.forseti/events.jsonl`` (per project, gitignored). It is enough to reconstruct
the loop as a sequence diagram — see ``tools/trace_to_mermaid.py``.

Scope: this is the *adapter's* local trace, deliberately lightweight and
standalone (stdlib only, no ``forseti``/``forseti_gate`` import) so a hook can log
without pulling the package in. The canonical, redacted W10 event schema
([#15](https://github.com/pmatos/forseti/issues/15)) supersedes it later; the
event *vocabulary* here is chosen to map onto that cleanly.

What it does NOT capture: Claude's natural-language messages. A hook sees the tool
call (the code written/edited) and the verifier's response, not the assistant's
prose — that lives only in Claude Code's own session transcript. The trace is the
loop mechanics; weave the transcript in by timestamp if the prose is wanted.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_STATE_DIR = ".forseti"
_EVENTS_FILE = "events.jsonl"

# Event type vocabulary (the `type` field). Kept small and stable so the renderer
# and any later #15 migration have a fixed contract to map from.
EDIT = "edit"  # a Write/Edit/MultiEdit fired on a C file
VERIFY = "verify"  # one `forseti verify` (ESBMC) call and its verdict
GATE = "gate"  # the PostToolUse decision for a file (pass | block)
STOP = "stop"  # the Stop-gate decision (block | residual | allow)


def events_path(project_dir: str | os.PathLike[str]) -> Path:
    """The trace file for a project: ``<project_dir>/.forseti/events.jsonl``."""
    return Path(project_dir) / _STATE_DIR / _EVENTS_FILE


def log_event(project_dir: str | os.PathLike[str], type: str, **fields: Any) -> None:
    """Append one event to the project's trace; never raise into the caller.

    Stamps a wall-clock ``ts`` (epoch seconds) and a ``type``, then writes the
    JSON object as a single line. The write is one ``O_APPEND`` syscall of a
    below-``PIPE_BUF`` line, so concurrent PostToolUse hooks appending in parallel
    interleave whole lines rather than corrupting each other — the same
    concurrency the gate lock guards for ``gate_state.json``, achieved here
    lock-free because append is atomic and the trace is write-only.

    Logging is best-effort observability: a trace failure must never turn a
    verdict into an error or crash a hook, so all I/O errors are swallowed.
    """
    event = {"ts": time.time(), "type": type, **fields}
    try:
        path = events_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def read_events(project_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load a project's trace in order; ``[]`` if there is none."""
    return read_events_file(events_path(project_dir))


def read_events_file(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load an ``events.jsonl`` file in order; ``[]`` if it is absent.

    Malformed lines (a torn final write, say) are skipped rather than raising —
    a reader should degrade to the events it can parse, never crash on a partial
    trace.
    """
    path = Path(path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
