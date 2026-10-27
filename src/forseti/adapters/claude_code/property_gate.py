"""Stop-gate surface for semantic property checks (issue #95, blocking: #213).

Read-only apart from the Stop-gate's own `stop_attempts` bookkeeping, and
best-effort otherwise: if `.forseti/forseti.db` already holds CANDIDATE
semantic properties for a unit the safety gate has *already* verified clean,
`forseti check` them and surface any VIOLATED loudly at Stop -- the same
"never silently pass" spirit the safety gate holds itself to (CLAUDE.md),
extended to properties a subagent proposed out of band (`forseti propose`,
the #95 property-generation subagent). As of issue #213, a VIOLATED verdict
also blocks the turn (`stop_gate.main`); every other outcome here (unresolved,
failed, skipped, deferred) stays loud-but-non-blocking.

Blocking needed none of the persisted `state["properties"]` map + prune/
reconciliation machinery `blocking_units`/`prune_deleted_units` give the
safety-verdict `state["units"]` map (issue #99 review), because nothing here
is cached across turns the way `state["units"]` is. Every Stop call recomputes
`semantic_check_summary` fresh: from whatever is currently in the property
store, checked against whatever the C source currently says, via a live
`forseti check` subprocess. A property that starts holding after a fix lands
simply stops appearing in `violations` on the very next call -- no
reconciliation step needed -- and a unit whose backing file was deleted
out-of-band yields `failed` (the subprocess can't find the file), never a
stale `violated`. What *is* shared with `state["units"]`'s blocking loop is
`stop_attempts`/`MAX_STOP_ATTEMPTS` (issue #95 review): a property that can
never be made to hold because the property itself, not the code, is wrong
still exits loudly past the attempt cap, the same "genuinely stuck" outcome
an unverifiable safety unit already gets. Forseti currently exposes no
host-facing way to withdraw or reclassify a stored CANDIDATE property short
of the code satisfying it (`PropertyStore.update_status` is a library method
only, with no CLI/MCP command) -- the attempt cap is the only exit in that
case, and `stop_gate._semantic_message` says so.

One residual inherited unchanged from the non-blocking cut: this whole
surface runs inside the Stop hook's own budget (`MAX_TOTAL_CHECK_S`, default
60s, half of hooks.json's 120s Stop timeout). If Claude Code kills the hook
process before this module returns, a real VIOLATED property is lost for that
turn the same way every other Stop-gate decision would be -- a fail-open by
hook-kill, not by this module's own logic, and not something a smaller
`MAX_TOTAL_CHECK_S` can fully close since the outer timeout is the harness's.

Store-presence *is* the opt-in: properties only land via an explicit `forseti
propose` (or another store write) -- a project that never proposes stays at
exactly today's (v0) behaviour, no flag needed. The one fast path this module
guarantees: no `.forseti/forseti.db` on disk means zero SQLite/subprocess
cost, checked before opening (and, load-bearing, before it could ever
*create*) the store.

Known residual: a property's `unit_id` is keyed by whatever path string
`forseti propose` was given (`core/propose.py`: ``make_unit_id(source, function)``,
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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forseti.adapters._cli_json import capture_json_command
from forseti.esbmc import VERIFY_GRACE_S
from forseti.properties import (
    CHECKABLE_STATUSES,
    PropertyStatus,
    PropertyStore,
    PropertyStoreError,
)
from forseti.unit_id import make_unit_id

from . import forseti_gate as gate

_STORE_ROOT_NAME = ".forseti"

# Deliberately much tighter than `forseti check`'s own CLI default
# (core/check.py: unwind=4, ladder=(8,16) -- up to 3 esbmc runs *per*
# property): this runs inside the Stop-gate's existing 120s hook timeout
# (hooks.json), shared with the OOB/blob git scans that already run there. No
# ladder (`--unwind-ladder ""`) keeps one property to one verdict run plus, when
# that verdict is Verified, one vacuity-probe run (`_ESBMC_RUNS_PER_PROPERTY`); a
# genuine loop property that needs more reports UNKNOWN here (loud, via
# `forseti check`'s own contract) rather than eat the hook's budget escalating
# on its own -- raise FORSETI_PROPERTY_UNWIND for a project that needs it.
# `gate.env_int`/`env_float` (not a bare `int`/`float`) -- a malformed value
# must not crash the whole Stop-gate hook at import time over this opt-in
# feature (issue #95 review); see `gate.env_config_errors`.
DEFAULT_UNWIND = gate.env_int("FORSETI_PROPERTY_UNWIND", "4", minimum=1)
CHECK_TIMEOUT_S = gate.env_float("FORSETI_PROPERTY_CHECK_TIMEOUT_S", "20", minimum=1.0)
_SUBPROCESS_MARGIN_S = 10.0
_ESBMC_RUNS_PER_PROPERTY = 2

# How many units-with-candidates one Stop-gate call will actually shell out to
# `forseti check` for. Each call is its own esbmc-driven subprocess, so an
# unbounded count could exhaust the hook's timeout on a project with many
# proposed units; excess units are counted (never silently dropped -- see
# `SemanticCheckSummary.deferred`) rather than checked.
MAX_UNITS_PER_TURN = gate.env_int("FORSETI_PROPERTY_MAX_UNITS", "3", minimum=0)

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
# subprocess timeout clamped to whatever budget remains (`_check_selected_units`)
# -- a between-units check alone cannot bound total loop time,
# since a single unit with enough stored properties can need more wall clock
# than one attempt's share of the ceiling by itself (issue #95 review,
# thread a7NSB on the between-units-only version of this fix).
MAX_TOTAL_CHECK_S = gate.env_float(
    "FORSETI_PROPERTY_MAX_TOTAL_CHECK_S", "60", minimum=1.0
)


def _check_min_attempt_budget_fits(
    min_attempt_budget_s: float, max_total_check_s: float
) -> None:
    """Records an env-config error if `max_total_check_s` cannot cover even one
    honest attempt at `min_attempt_budget_s`.

    Neither `env_int`/`env_float` alone can catch this -- it is two knobs
    interacting, not one out-of-range value. Left unchecked, a project that
    raises FORSETI_PROPERTY_CHECK_TIMEOUT_S without a matching raise to
    FORSETI_PROPERTY_MAX_TOTAL_CHECK_S would silently defer every unit, every
    turn, forever -- `semantic_check_summary`'s `remaining <
    _MIN_ATTEMPT_BUDGET_S` check never lets a single one start (issue #95
    review). A plain function, not inline module-level code, so a test can
    exercise the check directly with arbitrary values the same way
    `env_int`/`env_float` already are, instead of needing a real module
    reload to vary an import-time constant.
    """
    if min_attempt_budget_s > max_total_check_s:
        gate.record_env_config_error(
            "FORSETI_PROPERTY_CHECK_TIMEOUT_S needs at least "
            f"{min_attempt_budget_s:.0f}s of FORSETI_PROPERTY_MAX_TOTAL_CHECK_S="
            f"{max_total_check_s!r} for even one unit to start -- every unit "
            "would be deferred forever; raise the total budget or lower the "
            "per-check timeout"
        )


# The smallest remaining budget worth starting a fresh unit with: one esbmc
# attempt's worst case (`CHECK_TIMEOUT_S + VERIFY_GRACE_S`) plus the
# subprocess margin. Below this, a check would be clamped to a timeout
# too small to complete even a single property honestly -- indistinguishable
# from a real tooling failure (`failed`) even though it never got a fair
# attempt; `deferred` (issue #95 review) is the honest bucket for "ran out of
# turn budget", the same as a unit this loop never reaches at all.
_MIN_ATTEMPT_BUDGET_S = CHECK_TIMEOUT_S + VERIFY_GRACE_S + _SUBPROCESS_MARGIN_S
_check_min_attempt_budget_fits(_MIN_ATTEMPT_BUDGET_S, MAX_TOTAL_CHECK_S)


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
    from one with nothing to report either (issue #95 review). `vacuous` are the
    same shape for VACUOUS outcomes (W3.4): ESBMC verified the harness but the
    post-call point is unreachable at that k, so the postcondition was never
    evaluated. They never count as held and never make the summary `empty`.
    """

    violations: tuple[dict[str, Any], ...]
    checked: int
    deferred: int
    unresolved: tuple[dict[str, Any], ...] = ()
    failed: int = 0
    skipped: tuple[dict[str, Any], ...] = ()
    vacuous: tuple[dict[str, Any], ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.violations
            and not self.deferred
            and not self.unresolved
            and not self.failed
            and not self.skipped
            and not self.vacuous
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
    read, reused by the selected-unit batch to size subprocess timeouts: `forseti check`
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
            unit_id = make_unit_id(rel, function)
            checkable = store.list_for_unit(unit_id, CHECKABLE_STATUSES)
            if any(p.status == PropertyStatus.CANDIDATE for p in checkable):
                out.append((unit, len(checkable)))
        return out


def _check_unit(
    project_dir: str, unit: dict[str, Any], timeout_s: float
) -> list[Any] | None:
    """Run `forseti check --json` for one unit; `None` on any tooling failure.

    The selected-unit batch owns admission and supplies the complete subprocess
    timeout. `TimeoutExpired` propagates to that batch, which knows whether the
    timeout was clamped by the turn budget. Other expected tooling failures stay
    best-effort here; command construction and payload validation have no budget
    policy to re-derive.
    """
    rel, function = unit["file"], unit["function"]
    # `rel`, unchanged: the subprocess already runs with `cwd=project_dir`, so
    # `Unit.from_path` there builds `unit_id = make_unit_id(rel, function)` -- the same
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
        command = capture_json_command(
            argv,
            timeout=timeout_s,
            cwd=project_dir,
        )
    except OSError:
        return None
    try:
        payload = command.decode()
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        return None
    return payload["verdicts"]


@dataclass(frozen=True)
class _CheckBatch:
    """Completed verdict rows and honest unit accounting for one selected batch."""

    verdicts: tuple[Any, ...]
    checked: int
    deferred: int
    failed: int


def _check_selected_units(
    project_dir: str, selected: Sequence[tuple[dict[str, Any], int]]
) -> _CheckBatch:
    """Check selected units within one turn budget, retaining completed verdicts.

    Called after candidate discovery, sorting and prefix selection: store reads
    do not spend this budget. Before each unit, require one honest attempt's
    allowance (`_MIN_ATTEMPT_BUDGET_S`); equality admits, a smaller remainder
    defers the entire unstarted suffix without preparing another command.

    Each unit checks every non-terminal property, so its natural subprocess
    allowance is `max(1, n_props)` properties, each a verdict run plus a
    vacuity-probe run (`_ESBMC_RUNS_PER_PROPERTY`), plus one margin. Every run
    needs `CHECK_TIMEOUT_S + VERIFY_GRACE_S`: the ESBMC runner can legitimately
    use that grace before killing its child. The remaining turn budget caps this
    allowance even when a single unit would need more than the whole ceiling
    (six properties need `6 * 2 * 25 + 10 = 310s` by default; issue #95, a7NSB).

    Only an actual timeout under a strictly smaller allowance is `deferred`
    (ran out of turn budget). An unclamped timeout, bad flags, missing executable
    or malformed payload is `failed`. Successful clamped commands are `checked`.
    """
    start = time.monotonic()
    rows: list[Any] = []
    checked = deferred = failed = 0
    for index, (unit, n_props) in enumerate(selected):
        remaining = MAX_TOTAL_CHECK_S - (time.monotonic() - start)
        if remaining < _MIN_ATTEMPT_BUDGET_S:
            deferred += len(selected) - index
            break
        natural_timeout = (
            max(1, n_props)
            * _ESBMC_RUNS_PER_PROPERTY
            * (CHECK_TIMEOUT_S + VERIFY_GRACE_S)
            + _SUBPROCESS_MARGIN_S
        )
        timeout = min(natural_timeout, remaining)
        try:
            verdicts = _check_unit(project_dir, unit, timeout)
        except subprocess.TimeoutExpired:
            if timeout < natural_timeout:
                deferred += 1
            else:
                failed += 1
            continue
        if verdicts is None:
            failed += 1
            continue
        checked += 1
        rows.extend(verdicts)
    return _CheckBatch(tuple(rows), checked, deferred, failed)


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
    except (PropertyStoreError, sqlite3.Error, OSError):
        # A corrupt/unreadable forseti.db must not turn this best-effort surface
        # into a crashed Stop-gate hook (module docstring) -- unlike
        # `check_source`, which translates the same failure into a raised
        # `PropertyStoreError` for a caller that *wants* to know. `OSError`
        # (not just `PropertyStoreError`/`sqlite3.Error`) because
        # `PropertyStore.open`'s `mkdir` can raise it directly -- the
        # `.forseti/forseti.db` existence check just above is only a fast
        # path, not a guarantee: another process can remove `.forseti/`
        # between that check and `open()` a few lines below (issue #95
        # review; `core/cli.py`'s `_run_check` already catches `OSError`
        # around the equivalent `check_source` call for the same reason).
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
    batch = _check_selected_units(project_dir, to_check)
    deferred = max(0, len(candidates) - MAX_UNITS_PER_TURN) + batch.deferred
    violations: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    vacuous: list[dict[str, Any]] = []
    for verdict in batch.verdicts:
        if not isinstance(verdict, dict):
            continue
        outcome = verdict.get("outcome")
        if outcome == "violated":
            violations.append(verdict)
        elif outcome in ("unknown", "error"):
            unresolved.append(verdict)
        elif outcome == "skipped":
            skipped.append(verdict)
        elif outcome == "vacuous":
            vacuous.append(verdict)
    return SemanticCheckSummary(
        tuple(violations),
        batch.checked,
        deferred,
        tuple(unresolved),
        batch.failed,
        tuple(skipped),
        tuple(vacuous),
    )
