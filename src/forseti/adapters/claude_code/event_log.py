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
standalone (stdlib only, no ``forseti_gate`` import) so a hook can log without
pulling the gate machinery in. The canonical, redacted W10 event schema
([#15](https://github.com/pmatos/forseti/issues/15)) supersedes it later; the
event *vocabulary* here is chosen to map onto that cleanly.
The writer and project destination helper are re-exported from
`forseti._events`; this module owns the local vocabulary and trace reader.

What it does NOT capture: Claude's natural-language messages. A hook sees the tool
call (the code written/edited) and the verifier's response, not the assistant's
prose — that lives only in Claude Code's own session transcript. The trace is the
loop mechanics; weave the transcript in by timestamp if the prose is wanted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from forseti._events import log_event as log_event
from forseti._events import project_events_path as events_path
from forseti.adapters._atomic import write_text_atomic as write_text_atomic

# Event type vocabulary (the `type` field). Kept small and stable so the renderer
# and any later #15 migration have a fixed contract to map from.
EDIT = "edit"  # a Write/Edit/MultiEdit fired on a C file
VERIFY = "verify"  # one `forseti verify` (ESBMC) call and its verdict
GATE = "gate"  # the PostToolUse decision for a file (pass | block)
STOP = "stop"  # the Stop-gate decision (block | residual | allow)
SESSION = "session"  # a SessionStart baseline of the pre-session dirty C tree


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
