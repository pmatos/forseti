"""Discharge a representation invariant at the callers that construct the type.

A representation invariant (RFC-0007 kind 1, #387) is *assumed* at entry of every
function that takes ``T *``. Nothing in that check says the callers that build a
``T`` hand over one that satisfies it, so an over-strong invariant can hide a bug.
This module closes the gap the way RFC-0003 S3 closes the memory-precondition one:
the assumption becomes an **obligation** asserted at the callee's entry in a
*generated copy* of the translation unit, and every caller in it is verified
against that copy.

What it generalises, and what it does not:

* The injection is S3's (`find_definition_brace`, same-line insertion so every line
  number matches the user's file, a ``#line`` directive for ``__FILE__``). The
  predicate is the invariant, ``{ const T *self = <param>; __ESBMC_assert(...); }``
  at the entry of each non-initialiser function with a ``T *`` parameter -- the
  functions the inductive check assumes it for. An initialiser (``rb_init``) is
  what *establishes* the invariant, so it receives no obligation.
* The caller sweep reuses `open_caller_checks`/`CallerCheck`/`CallerOutcome` and
  the same fail-closed aggregation: a clean sweep upgrades only for a ``static``
  callee (an externally visible one exports the obligation to other translation
  units), only when every caller was checked and passed, and only after a site
  probe shows the call is reached.
* A caller is **closed**: it takes no parameters of its own (``main``, a test
  driver), so it constructs the ``T`` it passes. A caller that takes parameters
  (including one that relays a ``T *`` it was handed) is not verified here --
  materialising those is a reachable context built by ``init`` (#127) -- and
  withholds the discharge, never claims it.

The caller's own ``assert()`` calls are compiled out (``NDEBUG``, set before the
user's includes): ESBMC reports one violated property per run, and a caller's own
assertion that the broken state trips first would hide the obligation behind it.
The question here is whether the invariant holds on entry, not whether the caller
is otherwise correct; the caller's own properties are its own unit's to check.

Results are *bounded*: a discharge is "VERIFIED up to k" for one invocation of each
caller from the translation unit's initial state, never "proof". An invariant that
is not discharged stays *assumed*, and says why.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Literal, assert_never

from forseti.esbmc import (
    ListUnitsError,
    Unit,
    Verified,
    Violated,
    find_definition_brace,
)
from forseti.esbmc.units import TranslationUnitAnalysis, analyze_translation_unit
from forseti.orchestrator.ladder import climb_to_terminal, validated_ladder
from forseti.orchestrator.ports import VerifyPort
from forseti.properties import Property
from forseti.properties.invariant import InvariantRole, infer_role

from .discharge import open_caller_checks, self_reachable
from .model import CallerCheck, CallerOutcome
from .reachability import ProbeReachability, classify_site_probe
from .synth import RENAMED_MAIN, SynthError, line_directive, pointee_type

DISCHARGE_LABEL_PREFIX = "forseti:rep-invariant-entry:"
DISCHARGE_SITE_LABEL_PREFIX = "forseti:rep-invariant-site:"

_PARSE_TIMEOUT_S = 30.0
_NDEBUG_PRELUDE = "#ifndef NDEBUG\n#define NDEBUG\n#endif\n"


class DischargeStatus(Enum):
    """What the callers of one invariant-assuming function established."""

    DISCHARGED = "discharged"  # every caller closed, reached, and satisfies it
    VIOLATED = "violated"  # a caller hands over a T the invariant does not hold on
    UNKNOWN = "unknown"  # a caller's run was inconclusive: nothing was settled
    INCOMPLETE = "incomplete"  # the caller set is open: the invariant stays assumed


@dataclass(frozen=True)
class InvariantDischarge:
    """One invariant's discharge status at one function that assumes it."""

    property_id: str
    expression: str
    callee: str
    status: DischargeStatus
    detail: str
    callers: tuple[CallerCheck, ...] = ()
    k: int | None = None

    @property
    def label(self) -> str:
        """The honest one-word-plus-reason reading, never "proof"."""
        match self.status:
            case DischargeStatus.DISCHARGED:
                return f"DISCHARGED (VERIFIED up to k={self.k}): {self.detail}"
            case DischargeStatus.VIOLATED:
                return f"VIOLATED: {self.detail}"
            case DischargeStatus.UNKNOWN:
                return f"UNKNOWN (still assumed): {self.detail}"
            case DischargeStatus.INCOMPLETE:
                return f"ASSUMED (not discharged): {self.detail}"
            case _:
                assert_never(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.property_id,
            "expression": self.expression,
            "function": self.callee,
            "status": self.status.value,
            "discharged": self.status is DischargeStatus.DISCHARGED,
            "assumed": self.status is not DischargeStatus.DISCHARGED,
            "k": self.k,
            "detail": self.detail,
            "callers": [c.to_dict() for c in self.callers],
        }


DischargeOutcome = Literal["violated", "unknown", "incomplete", "empty", "discharged"]


@dataclass(frozen=True)
class InvariantDischargeRun:
    """Every stored invariant of one type, discharged function by function."""

    type_key: str
    discharges: tuple[InvariantDischarge, ...]

    @property
    def outcome(self) -> DischargeOutcome:
        """Worst-outcome-wins: violated > unknown > incomplete > empty > discharged.

        ``empty`` (no invariant stored, or no function takes the type) is never
        read as a discharge.
        """
        statuses = {d.status for d in self.discharges}
        if DischargeStatus.VIOLATED in statuses:
            return "violated"
        if DischargeStatus.UNKNOWN in statuses:
            return "unknown"
        if DischargeStatus.INCOMPLETE in statuses:
            return "incomplete"
        if not statuses:
            return "empty"
        return "discharged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type_key,
            "outcome": self.outcome,
            "discharges": [d.to_dict() for d in self.discharges],
        }


# The process status of `forseti invariant-discharge`. A discharge that stays
# `incomplete` is the *assumed* state every invariant already had, reported loudly
# (like S3's ASSUMED_VERIFIED, 0); `unknown` is a distinct non-zero state, never a
# silent pass.
DISCHARGE_EXIT_CODES: dict[DischargeOutcome, int] = {
    "discharged": 0,
    "incomplete": 0,
    "empty": 0,
    "violated": 1,
    "unknown": 2,
}


def assuming_functions(
    units: Sequence[Unit], type_name: str, initialisers: frozenset[str] = frozenset()
) -> tuple[tuple[Unit, tuple[str, ...]], ...]:
    """The functions that assume the invariant of `type_name`, with its parameters.

    A unit assumes it when a single-level ``T *`` / ``const T *`` parameter points
    to the type and the function is not an initialiser (`infer_role`, or named in
    `initialisers`): the same set the inductive check (`check --type`) assumes it
    for. Each is paired with the names of those parameters.
    """
    found: list[tuple[Unit, tuple[str, ...]]] = []
    for unit in units:
        if (
            unit.name in initialisers
            or infer_role(unit.name) is InvariantRole.INITIALISER
        ):
            continue
        names = tuple(p.name for p in unit.params if pointee_type(p.type) == type_name)
        if names:
            found.append((unit, names))
    return tuple(found)


def _entry_check(callee: str, param: str, type_name: str, expression: str) -> str:
    """The block asserting `expression` over `param`; a NULL `param` is no ``T``.

    The guard keeps a caller that deliberately passes NULL to a NULL-tolerant
    callee from failing on the assertion's own dereference, which would read as a
    fault of the caller rather than of the invariant.
    """
    label = f"{DISCHARGE_LABEL_PREFIX}{callee}:{param}"
    assertion = f'__ESBMC_assert(!self || ({expression}), "{label}");'
    if param == "self":
        return f"{{ {assertion} }}"
    return f"{{ const {type_name} *self = {param}; {assertion} }}"


def _inject_at_entry(
    source_text: str, callee: Unit, checks: Sequence[str], source_path: str
) -> str:
    brace = find_definition_brace(
        source_text, callee.name, callee.def_line, callee.predefined_guards
    )
    if brace is None:
        raise SynthError(f"no definition of {callee.name}() found in the source text")
    injected = "".join(f" {check}" for check in checks)
    copy = source_text[: brace + 1] + injected + source_text[brace + 1 :]
    return _NDEBUG_PRELUDE + line_directive(source_path) + copy


def inject_invariant_obligation(
    source_text: str,
    callee: Unit,
    params: Sequence[str],
    *,
    type_name: str,
    expression: str,
    source_path: str,
) -> str:
    """`source_text` with the invariant asserted at `callee`'s entry.

    A **generated copy**: the user's file stays pristine. One block per parameter
    of `params` is inserted immediately after the definition's ``{`` on that same
    line, so every line number matches the original. A parameter itself named
    ``self`` is asserted over directly (aliasing it would initialise it from
    itself). ``NDEBUG`` and a ``#line`` restoring `source_path` are prepended
    without shifting any line. `SynthError` for an unnamed parameter or a
    definition that is not isolable.
    """
    if any(not name for name in params):
        raise SynthError(f"{callee.name}() has an unnamed {type_name} * parameter")
    checks = [
        _entry_check(callee.name, name, type_name, expression.strip())
        for name in params
    ]
    return _inject_at_entry(source_text, callee, checks, source_path)


def inject_site_probe(source_text: str, callee: Unit, *, source_path: str) -> str:
    """`source_text` with one labelled ``assert(0)`` at `callee`'s entry.

    A caller that reaches the call makes it fail: the reachability reading that
    keeps a dead call site from discharging vacuously.
    """
    check = f'__ESBMC_assert(0, "{DISCHARGE_SITE_LABEL_PREFIX}{callee.name}");'
    return _inject_at_entry(source_text, callee, [check], source_path)


def render_caller_harness(copy_path: Path, caller: str) -> str:
    """A ``main`` that runs the closed `caller` once against the injected copy."""
    target = RENAMED_MAIN if caller == "main" else caller
    return (
        f"#define main {RENAMED_MAIN}\n"
        f'#include "{copy_path}"\n'
        "#undef main\n"
        "int main(void) {\n"
        f"    {target}();\n"
        "    return 0;\n"
        "}\n"
    )


@dataclass(frozen=True)
class _Verify:
    port: VerifyPort
    ladder: tuple[int, ...]
    work_dir: Path


def _check_caller(
    caller: Unit,
    callee: Unit,
    obligations: Path,
    site: Path,
    run: _Verify,
) -> CallerCheck:
    """Verify `caller`'s one invocation against the obligation-injected copy."""
    if caller.params:
        return CallerCheck(
            caller.name,
            CallerOutcome.UNCHECKED,
            "takes parameters, so it is not a closed caller that constructs the "
            "type itself; a caller that relays a pointer it was handed is "
            "covered only by its own inductive check, and a reachable context "
            "for its parameters needs `init` (#127)",
        )
    stem = f"{callee.name}__inv__{caller.name}"
    harness = run.work_dir / f"{stem}.c"
    harness.write_text(render_caller_harness(obligations, caller.name))
    settled = climb_to_terminal(harness, verify=run.port, ladder=run.ladder)
    result, k = settled.result, settled.k

    if isinstance(result, Violated):
        if f"{DISCHARGE_LABEL_PREFIX}{callee.name}:" in result.raw_counterexample:
            return CallerCheck(
                caller.name,
                CallerOutcome.OBLIGATION_VIOLATED,
                f"reaches {callee.name}() in a state the invariant does not hold on "
                "(the break may come from a constructor or an earlier mutator; "
                "see the counterexample and `check --type`)",
                k,
                result,
            )
        return CallerCheck(
            caller.name,
            CallerOutcome.CALLER_VIOLATED,
            "violates a property of its own (not the invariant) before the "
            "obligation is reached",
            k,
            result,
        )
    if not isinstance(result, Verified):
        return CallerCheck(
            caller.name,
            CallerOutcome.UNCHECKED,
            f"inconclusive up to k={k} ({result.verdict.value})",
            k,
            result,
        )

    site_harness = run.work_dir / f"{stem}__site.c"
    site_harness.write_text(render_caller_harness(site, caller.name))
    probe = run.port(site_harness, unwind=k)
    match reading := classify_site_probe(probe, label=DISCHARGE_SITE_LABEL_PREFIX):
        case ProbeReachability.REACHED:
            return CallerCheck(
                caller.name,
                CallerOutcome.DISCHARGED,
                f"reaches {callee.name}() and the invariant holds on entry",
                k,
                result,
            )
        case ProbeReachability.UNREACHABLE:
            return CallerCheck(
                caller.name,
                CallerOutcome.UNREACHABLE,
                f"never reaches {callee.name}(), so its pass discharges nothing",
                k,
                result,
            )
        case ProbeReachability.INCONCLUSIVE:
            return CallerCheck(
                caller.name,
                CallerOutcome.UNCHECKED,
                f"could not confirm {callee.name}() is reached ({probe.verdict.value})",
                k,
                result,
            )
        case _:
            assert_never(reading)


def _inconclusive(check: CallerCheck) -> bool:
    """A run that settled nothing: it faulted on its own, or ESBMC could not say."""
    return check.esbmc_result is not None and check.outcome in (
        CallerOutcome.CALLER_VIOLATED,
        CallerOutcome.UNCHECKED,
    )


def _aggregate(
    prop: Property,
    callee: Unit,
    checks: tuple[CallerCheck, ...],
    *,
    internal: bool,
) -> InvariantDischarge:
    """Fold the per-caller checks into one status, upgrading only on a clean sweep."""

    def result(
        status: DischargeStatus, detail: str, k: int | None = None
    ) -> InvariantDischarge:
        return InvariantDischarge(
            prop.property_id, prop.expression, callee.name, status, detail, checks, k
        )

    broken = tuple(c for c in checks if c.outcome is CallerOutcome.OBLIGATION_VIOLATED)
    if broken:
        names = ", ".join(f"{c.caller}()" for c in broken)
        return result(
            DischargeStatus.VIOLATED,
            f"{names} reaches {callee.name}() in a state the invariant does not "
            "hold on at entry, so what constructs and mutates the type does not "
            "establish it",
            max((c.settled_k or 0) for c in broken),
        )
    withheld = tuple(c for c in checks if c.outcome is not CallerOutcome.DISCHARGED)
    if withheld:
        why = "; ".join(f"{c.caller}(): {c.detail}" for c in withheld)
        status = (
            DischargeStatus.UNKNOWN
            if any(_inconclusive(c) for c in withheld)
            else DischargeStatus.INCOMPLETE
        )
        return result(status, f"discharge incomplete: {why}")
    if not internal:
        return result(
            DischargeStatus.INCOMPLETE,
            f"every caller of {callee.name}() in this translation unit establishes "
            f"the invariant, but {callee.name}() is externally visible, so a "
            "client in another translation unit can call it with anything; the "
            "obligation is exported there, and closing it needs "
            f"{callee.name}() to be static or discharged in that unit too",
        )
    k = max((c.settled_k or 0) for c in checks)
    return result(
        DischargeStatus.DISCHARGED,
        f"every caller of {callee.name}() in this translation unit is closed, "
        "reaches the call and establishes the invariant, for one invocation of "
        "each caller from this translation unit's initial state",
        k,
    )


def _discharge_function(
    prop: Property,
    callee: Unit,
    params: tuple[str, ...],
    *,
    type_name: str,
    source: Path,
    source_text: str,
    units: Sequence[Unit],
    open_callers_fn: Callable[[Path, str], tuple[CallerCheck, ...]],
    run: _Verify,
) -> InvariantDischarge:
    def incomplete(detail: str) -> InvariantDischarge:
        return InvariantDischarge(
            prop.property_id,
            prop.expression,
            callee.name,
            DischargeStatus.INCOMPLETE,
            detail,
        )

    try:
        unenumerable = open_callers_fn(source, callee.name)
    except ListUnitsError as exc:
        return incomplete(f"the caller set could not be enumerated: {exc}")

    referrers = tuple(
        u for u in units if u.name != callee.name and callee.name in u.calls
    )
    internal = any(u.internal_linkage for u in units if u.name == callee.name)
    if not referrers:
        detail = (
            f"no caller of {callee.name}() in this translation unit, so the "
            "invariant is exported to its clients rather than discharged here"
        )
        if unenumerable:
            why = "; ".join(f"{c.caller}: {c.detail}" for c in unenumerable)
            detail = f"{detail}; the caller set is also open: {why}"
        return incomplete(detail)

    resolved = str(source.resolve())
    try:
        obligations = inject_invariant_obligation(
            source_text,
            callee,
            params,
            type_name=type_name,
            expression=prop.expression,
            source_path=resolved,
        )
        site = inject_site_probe(source_text, callee, source_path=resolved)
    except SynthError as exc:
        return incomplete(f"the obligation could not be injected: {exc}")

    stem = f"{callee.name}__inv"
    obligations_path = run.work_dir / f"{stem}__obligations.c"
    obligations_path.write_text(obligations)
    site_path = run.work_dir / f"{stem}__obligation_site.c"
    site_path.write_text(site)

    checks = tuple(
        _check_caller(caller, callee, obligations_path, site_path, run)
        for caller in referrers
    )
    if self_reachable(list(units), callee.name):
        # The assert fires at every entry, so one failing run cannot say whether a
        # caller's value or a re-entry the body made broke it: blaming the caller
        # would accuse code that handed over exactly what it built.
        checks = (
            *(
                replace(
                    c,
                    outcome=CallerOutcome.UNATTRIBUTED,
                    detail=f"{callee.name}() can re-enter itself and the "
                    "obligation is asserted at every entry, so this failure "
                    "cannot be attributed to this caller",
                )
                if c.outcome is CallerOutcome.OBLIGATION_VIOLATED
                else c
                for c in checks
            ),
            CallerCheck(
                callee.name,
                CallerOutcome.UNATTRIBUTED,
                "re-enters itself, which this check does not explore from a "
                "caller's state",
            ),
        )
    return _aggregate(prop, callee, checks + unenumerable, internal=internal)


def discharge_invariants(
    source: Path,
    *,
    type_name: str,
    invariants: Sequence[Property],
    port: VerifyPort,
    unwind: int,
    unwind_ladder: tuple[int, ...] = (),
    initialisers: frozenset[str] = frozenset(),
    work_dir: Path | None = None,
    esbmc_bin: str = "esbmc",
    timeout_s: float | None = None,
    extra_flags: Sequence[str] = (),
    list_units_fn: Callable[[Path], list[Unit]] | None = None,
    open_callers_fn: Callable[[Path, str], tuple[CallerCheck, ...]] | None = None,
) -> tuple[InvariantDischarge, ...]:
    """Discharge each of `invariants` at every function of `type_name` that assumes it.

    `port` verifies a generated harness at a bound, climbing
    ``(unwind, *unwind_ladder)`` on UNKNOWN (`ValueError` for a malformed
    ladder). With neither listing hook injected, unit planning and
    caller-completeness share one translation-unit AST (as
    `discharge_precondition` does), parsed under the same `extra_flags` the
    verifier compiles with so both see the same translation unit.
    `ListUnitsError` propagates when it cannot be parsed at all.
    """
    if (list_units_fn is None) != (open_callers_fn is None):
        raise ValueError("inject both list_units_fn and open_callers_fn, or neither")
    ladder = validated_ladder(unwind, unwind_ladder)
    if not invariants:
        return ()
    if list_units_fn is None or open_callers_fn is None:
        analysis: TranslationUnitAnalysis = analyze_translation_unit(
            source,
            esbmc_bin=esbmc_bin,
            timeout_s=timeout_s or _PARSE_TIMEOUT_S,
            extra_flags=extra_flags,
        )
        units = analysis.units()

        def open_callers(path: Path, symbol: str) -> tuple[CallerCheck, ...]:
            return open_caller_checks(symbol, path, analysis.caller_openings(symbol))
    else:
        units = list_units_fn(source)
        open_callers = open_callers_fn

    assuming = assuming_functions(units, type_name, initialisers)
    if not assuming:
        return ()
    source_text = source.read_text()

    with (
        nullcontext(work_dir)
        if work_dir is not None
        else tempfile.TemporaryDirectory(prefix="forseti-invdischarge-")
    ) as directory:
        work = Path(directory)
        work.mkdir(parents=True, exist_ok=True)
        run = _Verify(port, ladder, work)
        return tuple(
            _discharge_function(
                prop,
                callee,
                params,
                type_name=type_name,
                source=source,
                source_text=source_text,
                units=units,
                open_callers_fn=open_callers,
                run=run,
            )
            for prop in invariants
            for callee, params in assuming
        )


def _caller_line(check: CallerCheck) -> str:
    bound = f" k={check.settled_k}" if check.settled_k is not None else ""
    return f"    {check.caller}() [{check.outcome.value}{bound}]: {check.detail}"


def invariant_discharge_transcript(run: InvariantDischargeRun) -> str:
    """Render a run as a readable transcript (pure, total).

    One line per (invariant, function), each led by its status so a discharged
    invariant is told apart from one that is still *assumed*, then a note on what
    the readings do and do not claim.
    """
    lines = [f"Forseti invariant discharge — {run.type_key}"]
    if not run.discharges:
        lines.append(
            "Nothing was discharged: no stored invariant, or no function of this "
            "file takes the type (the invariant stays assumed)."
        )
        return "\n".join(lines)
    by_property: dict[str, list[InvariantDischarge]] = {}
    for discharge in run.discharges:
        by_property.setdefault(discharge.property_id, []).append(discharge)
    for property_id, group in by_property.items():
        lines.append(f"Invariant {property_id}: {group[0].expression}")
        for d in group:
            lines.append(f"  {d.callee}(): {d.label}")
            lines.extend(_caller_line(c) for c in d.callers)
    lines.append(
        "Note: a discharge is VERIFIED up to the bound k, for one invocation of "
        "each closed caller from this translation unit's initial state; the "
        "caller's own assert() calls are compiled out, so the verdict concerns "
        "the invariant only. Anything not DISCHARGED stays assumed."
    )
    return "\n".join(lines)
