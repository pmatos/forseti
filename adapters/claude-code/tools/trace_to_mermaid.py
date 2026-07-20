#!/usr/bin/env python3
"""Render a Forseti loop trace (``.forseti/events.jsonl``) as a mermaid diagram.

Reads the append-only event trace written by the verify-gate hooks
(``hooks/event_log.py``) and prints a mermaid ``sequenceDiagram`` of the
write -> verify -> counterexample -> fix back-and-forth between four actors:
Claude, the PostToolUse Gate, ESBMC, and the Stop-gate. Paste the output into any
mermaid renderer (GitHub, mermaid.live, an Obsidian note) to see the loop.

Usage::

    trace_to_mermaid.py [PATH]

PATH is either the ``events.jsonl`` file or a project directory containing
``.forseti/events.jsonl`` (default: the current directory). ``--title`` overrides
the diagram title.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks")
)

import event_log

_PARTICIPANTS = (
    ("C", "Claude"),
    ("G", "Gate (PostToolUse)"),
    ("E", "ESBMC"),
    ("S", "Stop-gate"),
)

_VERDICT_LABEL = {
    "verified": "VERIFIED",
    "violated": "VIOLATED",
    "unknown": "UNKNOWN",
    "error": "ERROR",
}


def _resolve(path: Path) -> Path:
    """Accept either the events file itself or a project dir holding one."""
    if path.is_dir():
        return event_log.events_path(path)
    return path


def _sanitize(text: Any) -> str:
    """Make a value safe for a mermaid message label (no newlines/semicolons)."""
    return str(text).replace("\n", " ").replace(";", ",").strip()


def _dur(event: dict[str, Any]) -> str:
    d = event.get("duration_s")
    return f" ({d:.2f}s)" if isinstance(d, (int, float)) else ""


def _render_event(event: dict[str, Any]) -> list[str]:
    """The mermaid arrows for one trace event (empty for an unknown type)."""
    kind = event.get("type")
    if kind == event_log.EDIT:
        fns = _sanitize(", ".join(event.get("functions") or []) or "no functions")
        tool = _sanitize(event.get("tool", "?"))
        file = _sanitize(event.get("file"))
        return [f"    C->>G: {tool} {file} ({fns})"]
    if kind == event_log.VERIFY:
        unit = _sanitize(event.get("unit"))
        k = event.get("k")
        verdict = _VERDICT_LABEL.get(
            str(event.get("verdict")), str(event.get("verdict")).upper()
        )
        return [
            f"    G->>E: verify {unit} (k={k})",
            f"    E-->>G: {verdict}{_dur(event)}",
        ]
    if kind == event_log.GATE:
        if event.get("decision") == "pass":
            return ["    G-->>C: pass — VERIFIED up to k"]
        n = event.get("n_failures", "?")
        return [
            f"    G-->>C: block — {n} cex fed back (exit {event.get('exit_code', 2)})"
        ]
    if kind == event_log.STOP:
        decision = event.get("decision")
        if decision == "allow":
            return ["    C->>S: end turn?", "    S-->>C: ALLOW (clean)"]
        if decision == "residual":
            n = event.get("n_unverified", "?")
            return [
                "    C->>S: end turn?",
                f"    S-->>C: ALLOW with LOUD residual ({n} unverified)",
            ]
        return [
            "    C->>S: end turn?",
            f"    S-->>C: BLOCK (attempt {event.get('attempt', '?')})",
        ]
    return []


def render(
    events: list[dict[str, Any]], *, title: str = "Forseti verify-gate loop"
) -> str:
    """Turn an ordered event list into a mermaid ``sequenceDiagram`` string."""
    lines = ["sequenceDiagram", f"    title {_sanitize(title)}"]
    lines += [f"    participant {short} as {name}" for short, name in _PARTICIPANTS]
    body = [arrow for event in events for arrow in _render_event(event)]
    lines += body or ["    note over C,S: (no events recorded yet)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Forseti loop trace as mermaid."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        type=Path,
        help="events.jsonl file or a project dir containing .forseti/events.jsonl",
    )
    parser.add_argument("--title", default="Forseti verify-gate loop")
    args = parser.parse_args(argv)

    events_file = _resolve(args.path)
    if not events_file.exists():
        print(f"trace_to_mermaid: no trace at {events_file}", file=sys.stderr)
        return 1
    events = event_log.read_events_file(events_file)
    print(render(events, title=args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
