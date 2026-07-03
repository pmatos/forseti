"""Run persistence: one JSONL record per run, keyed by unit id (`path::symbol`).

JSONL-first per #29 — the per-project `.forseti/` store (gitignored) gets one
append-only file per unit, one line per run. The record is the pure
`report_for` projection plus the emitted events, so a human (or #15's tooling)
can replay what the loop did. SQLite (`.forseti/forseti.db`) is deferred until a
query workload actually needs it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from .loop import LoopRun
from .report import report_for
from .telemetry import Event


def _unit_slug(unit_id: str) -> str:
    """A filesystem-safe, collision-free key for a `path::symbol` unit id.

    A human-readable slug (`examples/abs.c::my_abs` -> `examples_abs.c__my_abs`)
    plus a short stable hash of the *full* unit id. The readable part alone is
    lossy — `a/b.c::f` and `a_b.c::f` slug alike — so the hash suffix (taken over
    the exact unit id) keeps distinct units in distinct files, preserving the
    one-file-per-unit `path::symbol` keying.
    """
    readable = unit_id.replace("::", "__").replace("/", "_")
    digest = hashlib.sha256(unit_id.encode()).hexdigest()[:12]
    return f"{readable}-{digest}"


def persist_run(
    run: LoopRun,
    *,
    unit_id: str,
    events: Sequence[Event] = (),
    root: Path = Path(".forseti"),
) -> Path:
    """Append a run record (JSON line) under `root/runs/<slug>.jsonl`; return the path.

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
