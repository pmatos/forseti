"""Run persistence: one JSONL record per run, keyed by unit id (`path::symbol`).

JSONL-first per #29 — the per-project `.forseti/` store (gitignored) gets one
append-only file per unit, one line per run. The record is the pure
`report_for` projection plus the emitted events, so a human (or #15's tooling)
can replay what the loop did. SQLite (`.forseti/forseti.db`) is deferred until a
query workload actually needs it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from .loop import LoopRun
from .report import report_for
from .telemetry import Event


def _unit_slug(unit_id: str) -> str:
    """A filesystem-safe key for a `path::symbol` unit id.

    `examples/abs.c::my_abs` -> `examples_abs.c__my_abs`.
    """
    return unit_id.replace("::", "__").replace("/", "_")


def persist_run(
    run: LoopRun,
    *,
    unit_id: str,
    events: Sequence[Event] = (),
    root: Path = Path(".forseti"),
) -> Path:
    """Append one run record (JSON line) under `root/runs/<slug>.jsonl`; return its path.

    The record is `{"unit", "report", "events"}` — `report` is `report_for(run)`'s
    serializable projection and `events` the emitted telemetry. Append-only, so a
    unit's file accumulates one line per run.
    """
    dest = root / "runs" / f"{_unit_slug(unit_id)}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "unit": unit_id,
        "report": report_for(run).to_dict(),
        "events": [event.to_dict() for event in events],
    }
    with dest.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return dest
