"""Run persistence: one JSONL record per run, keyed by unit id (`path::symbol`).

JSONL-first per #29 — the per-project `.forseti/` store (gitignored) gets one
append-only file per unit, one line per run. The record is the pure
`report_for` projection plus the emitted events, so a human (or #15's tooling)
can replay what the loop did.

`persist_property_check` is the sibling for the #66 driver: the per-property run
artifact goes under `.forseti/property-checks/`, distinct from the SQLite grading
store (`.forseti/forseti.db`, #62/ADR-0009 D1) — this is the run trace, not the
queryable property state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .loop import LoopRun
from .report import report_for
from .telemetry import Event

if TYPE_CHECKING:  # only for typing — avoids a runtime cycle with check.py
    from .check import PropertyCheckRun


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


def persist_property_check(
    run: PropertyCheckRun,
    *,
    events: Sequence[Event] = (),
    root: Path = Path(".forseti"),
) -> Path:
    """Append a property-check record under `root/property-checks/<slug>.jsonl`.

    The record is `{"unit", "run", "events"}` — `run` is `PropertyCheckRun`'s
    serializable projection (unit id, counts, and every per-property verdict) and
    `events` the emitted telemetry. Append-only, so a unit's file accumulates one
    line per check run. Distinct from `persist_run`'s `runs/` and from the SQLite
    grading store — this is the #66 check artifact.
    """
    dest = root / "property-checks" / f"{_unit_slug(run.unit_id)}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "unit": run.unit_id,
        "run": run.to_dict(),
        "events": [event.to_dict() for event in events],
    }
    with dest.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return dest
