"""Stop-gate surface for semantic property checks (issue #95).

Read-only and best-effort: if `.forseti/forseti.db` already holds CANDIDATE
semantic properties for a unit the safety gate has *already* verified clean,
`forseti check` them and surface any VIOLATED loudly at Stop -- the same
"never silently pass" spirit the safety gate holds itself to (CLAUDE.md),
extended to properties a subagent proposed out of band (`forseti propose`,
the #95 property-generation subagent). Deliberately non-blocking: making this
*block* the turn needs the same prune/reconciliation machinery
`blocking_units`/`prune_deleted_units` already give the safety-verdict `units`
map (issue #99 review) -- a parallel `state["properties"]` key without
matching reconciliation would reproduce that class of Stop-gate deadlock. A
blocking version is a documented follow-up (issue #95), not this cut.

Store-presence *is* the opt-in: properties only land via an explicit `forseti
propose` (or another store write) -- a project that never proposes stays at
exactly today's (v0) behaviour, no flag needed. The one fast path this module
guarantees: no `.forseti/forseti.db` on disk means zero SQLite/subprocess
cost, checked before opening (and, load-bearing, before it could ever
*create*) the store.

Known residual: a property's `unit_id` is keyed by whatever path string
`forseti propose` was given (`core/propose.py`: ``f"{source}::{function}"``,
unnormalized). This module matches it against the gate's own `rel::function`
spelling -- the same `unit_id(project_dir, file_path)` the safety verdicts are
already keyed by (`state["units"]`'s own `file` field). A subagent that
proposes against a differently-spelled path (an absolute path where the gate
recorded a relative one, or vice versa) will not be matched here; widening the
match is a follow-up, not a correctness bug in what this module does report.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forseti.properties import PropertyStatus, PropertyStore, PropertyStoreError

from . import forseti_gate as gate

_STORE_ROOT_NAME = ".forseti"

# Deliberately much tighter than `forseti check`'s own CLI default
# (core/check.py: unwind=4, ladder=(8,16) -- up to 3 esbmc runs *per*
# property): this runs inside the Stop-gate's existing 120s hook timeout
# (hooks.json), shared with the OOB/blob git scans that already run there. No
# ladder (`--unwind-ladder ""`) keeps one property to exactly one esbmc run; a
# genuine loop property that needs more reports UNKNOWN here (loud, via
# `forseti check`'s own contract) rather than eat the hook's budget escalating
# on its own -- raise FORSETI_PROPERTY_UNWIND for a project that needs it.
DEFAULT_UNWIND = int(os.environ.get("FORSETI_PROPERTY_UNWIND", "4"))
CHECK_TIMEOUT_S = float(os.environ.get("FORSETI_PROPERTY_CHECK_TIMEOUT_S", "20"))
_SUBPROCESS_MARGIN_S = 10.0

# How many units-with-candidates one Stop-gate call will actually shell out to
# `forseti check` for. Each call is its own esbmc-driven subprocess, so an
# unbounded count could exhaust the hook's timeout on a project with many
# proposed units; excess units are counted (never silently dropped -- see
# `SemanticCheckSummary.deferred`) rather than checked.
MAX_UNITS_PER_TURN = int(os.environ.get("FORSETI_PROPERTY_MAX_UNITS", "3"))


@dataclass(frozen=True)
class SemanticCheckSummary:
    """What one Stop-gate pass over the property store found.

    `violations` are per-property VIOLATED verdict dicts (`forseti check
    --json`'s own `verdicts[]` shape, plus `unit_id`). `checked`/`deferred`
    count units-with-candidates actually checked vs. held back by
    `MAX_UNITS_PER_TURN` -- so a project with more proposed units than one
    turn's budget is told it, rather than reading as fully covered.
    """

    violations: tuple[dict[str, Any], ...]
    checked: int
    deferred: int

    @property
    def empty(self) -> bool:
        return not self.violations and not self.deferred


def _verified_units(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Recorded units the safety gate itself marked `verified` (safety-clean)."""
    return [
        u for u in state.get("units", {}).values() if u.get("verdict") == "verified"
    ]


def _units_with_candidates(
    project_dir: str, units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Subset of `units` that have at least one stored CANDIDATE property.

    A cheap, in-process SQLite read -- no subprocess, no esbmc -- so a project
    with properties for only one of ten edited units pays the expensive
    `forseti check` subprocess exactly once.
    """
    store = PropertyStore.open(Path(project_dir) / _STORE_ROOT_NAME)
    try:
        out = []
        for unit in units:
            rel, function = unit.get("file"), unit.get("function")
            if not rel or not function:
                continue
            unit_id = f"{rel}::{function}"
            if store.list_for_unit(unit_id, {PropertyStatus.CANDIDATE}):
                out.append(unit)
        return out
    finally:
        store.close()


def _check_unit(project_dir: str, unit: dict[str, Any]) -> dict[str, Any] | None:
    """Run `forseti check --json` for one unit; `None` on any tooling failure.

    Never raised: a broken check must surface as "nothing found" here, not
    crash the Stop-gate over a best-effort feature (module docstring).
    """
    rel, function = unit["file"], unit["function"]
    abspath = os.path.join(project_dir, rel)
    argv = [
        *gate.resolve_forseti_cmd(),
        "check",
        abspath,
        "--function",
        function,
        "--store-root",
        str(Path(project_dir) / _STORE_ROOT_NAME),
        "--unwind",
        str(DEFAULT_UNWIND),
        "--unwind-ladder",
        "",  # no escalation -- see module docstring on the budget
        "--timeout",
        str(int(CHECK_TIMEOUT_S)),
        "--json",
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S + _SUBPROCESS_MARGIN_S,
            cwd=project_dir,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        return None
    return payload


def semantic_check_summary(
    project_dir: str, state: dict[str, Any]
) -> SemanticCheckSummary:
    """Scan safety-verified units for stored semantic properties and check them.

    `[]`/empty when there is nothing to report: no store on disk, no
    `verified` unit has any stored `CANDIDATE` property, or (best-effort) every
    `forseti check` invocation attempted failed at the tooling level.
    """
    empty = SemanticCheckSummary((), 0, 0)
    if not (Path(project_dir) / _STORE_ROOT_NAME / "forseti.db").exists():
        return empty
    verified = _verified_units(state)
    if not verified:
        return empty
    try:
        candidates = _units_with_candidates(project_dir, verified)
    except (PropertyStoreError, sqlite3.Error):
        # A corrupt/unreadable forseti.db must not turn this best-effort surface
        # into a crashed Stop-gate hook (module docstring) -- unlike
        # `check_source`, which translates the same failure into a raised
        # `PropertyStoreError` for a caller that *wants* to know.
        return empty
    if not candidates:
        return empty

    to_check, deferred = (
        candidates[:MAX_UNITS_PER_TURN],
        candidates[MAX_UNITS_PER_TURN:],
    )
    violations: list[dict[str, Any]] = []
    checked = 0
    for unit in to_check:
        payload = _check_unit(project_dir, unit)
        if payload is None:
            continue
        checked += 1
        for verdict in payload["verdicts"]:
            if isinstance(verdict, dict) and verdict.get("outcome") == "violated":
                violations.append(verdict)
    return SemanticCheckSummary(tuple(violations), checked, len(deferred))
