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
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forseti.esbmc import VERIFY_GRACE_S
from forseti.properties import (
    CHECKABLE_STATUSES,
    PropertyStatus,
    PropertyStore,
    PropertyStoreError,
)

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
# `gate.env_int`/`env_float` (not a bare `int`/`float`) -- a malformed value
# must not crash the whole Stop-gate hook at import time over this opt-in
# feature (issue #95 review); see `gate.env_config_errors`.
DEFAULT_UNWIND = gate.env_int("FORSETI_PROPERTY_UNWIND", "4")
CHECK_TIMEOUT_S = gate.env_float("FORSETI_PROPERTY_CHECK_TIMEOUT_S", "20")
_SUBPROCESS_MARGIN_S = 10.0

# How many units-with-candidates one Stop-gate call will actually shell out to
# `forseti check` for. Each call is its own esbmc-driven subprocess, so an
# unbounded count could exhaust the hook's timeout on a project with many
# proposed units; excess units are counted (never silently dropped -- see
# `SemanticCheckSummary.deferred`) rather than checked.
MAX_UNITS_PER_TURN = gate.env_int("FORSETI_PROPERTY_MAX_UNITS", "3")

# Aggregate wall-clock ceiling for the whole semantic-check loop (all units in
# `to_check`, sequentially), not just one unit's own subprocess timeout. Half
# of hooks.json's 120s Stop timeout, leaving the other half for the OOB/blob
# git scans and the safety gate's own decision that already share that budget
# (module docstring) -- `stop_gate.main` computes the block/allow decision
# *before* calling `semantic_check_summary` (issue #95 review), so if this
# loop itself runs past the hook's own timeout, Claude Code kills the whole
# process before that already-decided verdict is ever emitted, silently
# failing the turn open instead. Enforced two ways: a unit not yet started is
# `deferred` once the remaining budget can't cover one honest attempt
# (`_MIN_ATTEMPT_BUDGET_S`), and a unit already starting has its own
# subprocess timeout clamped to whatever budget remains (`_check_unit`'s
# `budget_s`) -- a between-units check alone cannot bound total loop time,
# since a single unit with enough stored properties can need more wall clock
# than one attempt's share of the ceiling by itself (issue #95 review,
# thread a7NSB on the between-units-only version of this fix).
MAX_TOTAL_CHECK_S = gate.env_float("FORSETI_PROPERTY_MAX_TOTAL_CHECK_S", "60")

# The smallest remaining budget worth starting a fresh unit with: one esbmc
# attempt's worst case (`CHECK_TIMEOUT_S + VERIFY_GRACE_S`) plus the
# subprocess margin. Below this, `_check_unit` would be clamped to a timeout
# too small to complete even a single property honestly -- indistinguishable
# from a real tooling failure (`failed`) even though it never got a fair
# attempt; `deferred` (issue #95 review) is the honest bucket for "ran out of
# turn budget", the same as a unit this loop never reaches at all.
_MIN_ATTEMPT_BUDGET_S = CHECK_TIMEOUT_S + VERIFY_GRACE_S + _SUBPROCESS_MARGIN_S


@dataclass(frozen=True)
class SemanticCheckSummary:
    """What one Stop-gate pass over the property store found.

    `violations` are per-property VIOLATED verdict dicts (`forseti check
    --json`'s own `verdicts[]` shape, plus `unit_id`). `unresolved` are the
    same shape for UNKNOWN/ERROR outcomes -- CLAUDE.md's "never silently pass
    UNKNOWN" applies here too, so those are reported rather than dropped.
    `skipped` are the same shape for SKIPPED outcomes -- a reachability-kind
    property `forseti check` deliberately defers (ADR-0009 D2), not a failure,
    but still not a property that was actually checked; a unit whose only
    stored candidate is reachability-kind would otherwise `check` clean
    (`checked` incremented, no violation/unresolved/failed) and read as fully
    covered when nothing was ever verified (issue #95 review). `checked`/
    `deferred` count units-with-candidates actually checked vs. held back by
    `MAX_UNITS_PER_TURN` -- so a project with more proposed units than one
    turn's budget is told it, rather than reading as fully covered. `failed`
    counts a unit `_check_unit` could not get any verdicts for at all
    (subprocess timeout, bad build flags, an unparseable payload) -- distinct
    from `unresolved`, which is a property `forseti check` *did* run but
    couldn't settle; a unit that never ran isn't silently indistinguishable
    from one with nothing to report either (issue #95 review).
    """

    violations: tuple[dict[str, Any], ...]
    checked: int
    deferred: int
    unresolved: tuple[dict[str, Any], ...] = ()
    failed: int = 0
    skipped: tuple[dict[str, Any], ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.violations
            and not self.deferred
            and not self.unresolved
            and not self.failed
            and not self.skipped
        )


def _verified_units(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Recorded units the safety gate itself marked `verified` (safety-clean)."""
    return [
        u for u in state.get("units", {}).values() if u.get("verdict") == "verified"
    ]


def _units_with_candidates(
    project_dir: str, units: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], int]]:
    """`(unit, checkable_count)` for every `units` entry with >= 1 stored CANDIDATE.

    A cheap, in-process SQLite read -- no subprocess, no esbmc -- so a project
    with properties for only one of ten edited units pays the expensive
    `forseti check` subprocess exactly once. `checkable_count` -- every stored
    non-terminal property, not just CANDIDATE -- is a byproduct of the same
    read, reused to size `_check_unit`'s subprocess timeout: `forseti check`
    itself checks every non-terminal property for the unit in one process
    (`CHECKABLE_STATUSES`, e.g. a unit with 1 CANDIDATE + 2 already-GRADED
    properties runs 3 esbmc attempts, not 1), so counting CANDIDATE alone would
    under-budget the subprocess for a unit with mixed statuses (issue #95
    review). Presence is still gated on CANDIDATE specifically -- a unit with
    only GRADED/other non-terminal properties and no fresh CANDIDATE has
    nothing new for this opt-in surface to report.
    """
    with PropertyStore.open(Path(project_dir) / _STORE_ROOT_NAME) as store:
        out = []
        for unit in units:
            rel, function = unit.get("file"), unit.get("function")
            if not rel or not function:
                continue
            unit_id = f"{rel}::{function}"
            checkable = store.list_for_unit(unit_id, CHECKABLE_STATUSES)
            if any(p.status == PropertyStatus.CANDIDATE for p in checkable):
                out.append((unit, len(checkable)))
        return out


def _check_unit(
    project_dir: str, unit: dict[str, Any], n_props: int, budget_s: float
) -> list[Any] | None:
    """Run `forseti check --json` for one unit; `None` on any tooling failure.

    Never raised: a broken check must surface as "nothing found" here, not
    crash the Stop-gate over a best-effort feature (module docstring). `n_props`
    (the unit's stored-checkable-property count, `_units_with_candidates`)
    scales the subprocess's wall-clock budget -- `--timeout` below is esbmc's
    *per-attempt* budget, but `forseti check` runs it once per stored
    checkable property for the unit, so a unit with 2+ properties needs more
    than one attempt's worth of wall clock. The subprocess timeout below adds
    `VERIFY_GRACE_S` per attempt on top of `CHECK_TIMEOUT_S` -- `verify`
    (`esbmc/runner.py`) itself permits `CHECK_TIMEOUT_S + VERIFY_GRACE_S`
    before it hard-kills esbmc, so budgeting only `CHECK_TIMEOUT_S` per
    attempt here would let this subprocess time out before a legitimately-
    running child could, discarding every verdict already computed (issue #95
    review).

    `budget_s` -- the caller's *remaining* slice of `MAX_TOTAL_CHECK_S` -- caps
    the per-unit formula from above: a unit with enough stored properties can
    legitimately need more wall clock than one attempt's share of the
    aggregate ceiling (a unit with 6 candidates needs `6 * 25 + 10 = 160s`,
    already past `MAX_TOTAL_CHECK_S`'s 60s default and the Stop hook's own
    120s timeout on its own) -- the between-units deadline check in
    `semantic_check_summary` only bounds *when* a unit starts, not how long
    its own subprocess may then run (issue #95 review, a7NSB: the earlier
    aggregate-deadline fix left exactly this gap). The caller is responsible
    for not calling this at all once `budget_s` can no longer cover one
    honest attempt -- see `_MIN_ATTEMPT_BUDGET_S`.
    """
    rel, function = unit["file"], unit["function"]
    # `rel`, unchanged: the subprocess already runs with `cwd=project_dir`, so
    # `Unit.from_path` there builds `unit_id = f"{rel}::{function}"` -- the same
    # spelling `_units_with_candidates` just proved has a stored candidate.
    # Resolving to an absolute path here would key the check's store lookup
    # under a different string than the one that selected this unit, making
    # `forseti check` silently find nothing (issue #95 review).
    argv = [
        *gate.resolve_forseti_cmd(),
        "check",
        rel,
        "--function",
        function,
        "--store-root",
        str(Path(project_dir) / _STORE_ROOT_NAME),
        "--unwind",
        str(DEFAULT_UNWIND),
        "--unwind-ladder",
        "",  # no escalation -- see module docstring on the budget
        "--timeout",
        str(CHECK_TIMEOUT_S),
        "--json",
    ]
    # Same `FORSETI_BUILD_FLAGS` the safety gate forwards to its own verify
    # and `list-units` calls (`forseti_gate.build_flags_from_env`) -- without
    # them the semantic harness can compile a different preprocessor branch or
    # fail to resolve a project header, reporting held/violated for code the
    # safety gate never actually verified (issue #95 review). Unparseable
    # quoting degrades this best-effort unit to "nothing found", the same as
    # any other tooling failure here -- never a crash.
    try:
        build_flags = gate.build_flags_from_env()
    except gate.UnitsUnavailable:
        return None
    if build_flags:
        argv += ["--", *build_flags]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=min(
                max(1, n_props) * (CHECK_TIMEOUT_S + VERIFY_GRACE_S)
                + _SUBPROCESS_MARGIN_S,
                budget_s,
            ),
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
    return payload["verdicts"]


def semantic_check_summary(
    project_dir: str, state: dict[str, Any]
) -> SemanticCheckSummary:
    """Scan safety-verified units for stored semantic properties and check them.

    `[]`/empty when there is nothing to report: no store on disk, or no
    `verified` unit has any stored `CANDIDATE` property. A `forseti check`
    invocation that fails at the tooling level is not silently absorbed into
    an empty summary -- it counts into `failed` instead (issue #95 review).
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

    # Deterministic, not insertion-order: checking never mutates a property's
    # stored status, so nothing changes which prefix `state["units"]`'s
    # insertion order would hand back turn after turn -- an accidental
    # dependency on edit order rather than a real, documented selection rule
    # (issue #95 review; see MAX_UNITS_PER_TURN's own message below).
    candidates.sort(
        key=lambda pair: (pair[0].get("file", ""), pair[0].get("function", ""))
    )
    to_check = candidates[:MAX_UNITS_PER_TURN]
    deferred = max(0, len(candidates) - MAX_UNITS_PER_TURN)
    violations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    checked = 0
    failed = 0
    # Aggregate wall-clock cap across this whole loop, not just each unit's own
    # subprocess timeout (MAX_TOTAL_CHECK_S's own comment) -- checked between
    # units so a unit already in flight is never killed mid-subprocess. A unit
    # cut off this way is `deferred`, not `failed`: it was never attempted,
    # the same "never silently dropped" accounting the per-turn unit budget
    # already gets. `_check_unit` is itself capped to the *remaining* slice
    # of the budget (`min(..., budget_s)`), never just this loop's own
    # between-units check alone -- a unit with enough stored properties can
    # need more wall clock than one attempt's share of the ceiling on its
    # own (issue #95 review, a7NSB), so the between-units check alone cannot
    # bound total loop time. Below `_MIN_ATTEMPT_BUDGET_S` (one honest
    # attempt's worth) a unit is deferred rather than started -- clamping it
    # to a too-small timeout would misreport a real "ran out of budget" as a
    # `failed` tooling error.
    start = time.monotonic()
    for i, (unit, n_props) in enumerate(to_check):
        remaining = MAX_TOTAL_CHECK_S - (time.monotonic() - start)
        if remaining < _MIN_ATTEMPT_BUDGET_S:
            deferred += len(to_check) - i
            break
        verdicts = _check_unit(project_dir, unit, n_props, remaining)
        if verdicts is None:
            failed += 1
            continue
        checked += 1
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            outcome = verdict.get("outcome")
            if outcome == "violated":
                violations.append(verdict)
            elif outcome in ("unknown", "error"):
                unresolved.append(verdict)
            elif outcome == "skipped":
                skipped.append(verdict)
    return SemanticCheckSummary(
        tuple(violations), checked, deferred, tuple(unresolved), failed, tuple(skipped)
    )
