"""Turn an *assumed* memory precondition into a *checked* one (RFC-0003 S3).

S2 verifies a pointer-taking unit against a synthesised precondition it
**assumes**: the sidecar `malloc`s a valid object per pointer and reports
``ASSUMED_VERIFIED`` — honest, but the assumption is nobody's obligation yet. The
soundness closure is compositional discharge: what the callee assumed becomes an
**obligation checked at every caller**, and the top-level harness (which
allocates real objects) discharges the chain. That is the CBMC/AWS modular model.

Mechanically, for a unit ``f``:

1. `inject_obligations` writes a **generated copy** of the translation unit with
   ``__ESBMC_assert(__ESBMC_r_ok(p, <the size S2 allocated>), ...)`` at ``f``'s
   entry — the user's source stays pristine (RFC-0003 OQ1).
2. Every unit in the same TU whose body references ``f`` is a caller. Each gets
   its *own* S2 sidecar — real objects for its own parameters — but including the
   injected copy, so ``f``'s obligation is evaluated with that caller's actual
   arguments.
3. A caller that passes an invalid or too-small pointer FAILS the obligation
   property (recognised by its label, never by counterexample prose). A caller
   that passes a valid one VERIFIES — and is credited only after a **site probe**
   confirms it reaches the call at all, so a dead call site cannot discharge
   vacuously.

**Vehicle, and why not `--replace-call-with-contract`.** RFC-0003 named ESBMC
function contracts as the discharge vehicle. Measured against our pinned esbmc
8.3.0 they cannot carry a *memory* precondition: a ``__ESBMC_requires`` whose
expression contains an intrinsic **call** (``is_fresh``, ``r_ok``,
``get_object_size``) is transplanted into the caller referencing a callee-local
temporary that no longer exists, so every call site FAILS regardless of what it
passes, and under ``--enforce-contract`` the same requires silently has no
effect. Both directions fail *closed* — never a false VERIFIED — but neither
discharges anything. `tests/esbmc/test_contract_vehicle.py` pins that behaviour
so a fork fix reopens the question. Until then the obligation is asserted at the
callee's entry, which checks the same predicate against the same arguments; what
it gives up is *modularity* (the callee's body is still explored), not soundness.

**Aggregation fails closed.** ``DISCHARGED_VERIFIED`` requires that every caller
was actually checked *and* every check passed. A caller L0 cannot materialise, an
inconclusive ladder, an unreachable call site, or a caller the gate cannot even
enumerate — a ``static inline`` in an included header is in this translation unit
but is not a unit `list_units` reports, so `parse_external_callers` counts them
from the same clang dump — each leaves the verdict at ``ASSUMED_VERIFIED`` with a
loud "discharge incomplete", never an upgrade.

**A caller set is only as complete as the call graph.** Callers are found by name,
so a body that calls ``fp(...)`` references the *variable* and no edge leads back
to whatever ``fp`` holds: a ``static cb_t fp = f;`` plus one indirect call is a
caller of ``f`` that no enumeration built from `Unit.calls` will ever list. So the
upgrade is withheld whenever `parse_address_escapes` finds ``f``'s address taken
rather than called (``UNRESOLVED``) — the caller set is not enumerable, and a
sweep of the callers we *could* see says nothing about the ones we could not.
That makes a function in a dispatch table permanently ``ASSUMED_VERIFIED``, which
is the honest reading: it really can be invoked from anywhere in the TU with
anything. Following an indirect call to its targets is L1/S4 work.

**What a discharge is relative to.** Each caller's own parameters are materialised
by *its* S2 assumption, so one run proves ``caller precondition => callee
precondition`` at every call site: one link of the chain, not the whole chain.
Closing a deeper chain means discharging each link in turn. The exception is an
**anchored** caller — one whose own precondition is empty (no pointer parameters),
whose harness therefore allocates real objects and leaves nothing assumed. The
label states the caveat and drops it only when every caller is anchored.

**And it fails closed in the *other* direction too.** The mirror-image hazard is
moving RFC-0003's phantom VIOLATED from the callee to the call site: a caller
whose own signature states no extent (``void hash_block(uint8_t *blk)`` for a
16-byte block) is materialised by L0's weakest fallback — one pointee — so it
*cannot* satisfy anything wider, however correct it is. Blaming it would flag
working code, so an obligation failure from such a caller is
``UNDERDETERMINED``: it withholds the upgrade without accusing anyone. Only a
caller whose every pointer is sized by something its signature actually states —
a companion length, a written array extent — can be VIOLATED at the call site.
Reading the under-specified caller's true extent is L1's job (RFC-0003 S4).

**Exit codes stay a statement about the unit under discharge.** A caller that
faults on a memory property of its own before the obligation is reached
(``CALLER_VIOLATED``) is reported loudly, with its counterexample, but leaves the
process status at the callee's own ``ASSUMED_VERIFIED`` 0: that caller is a
different verification unit, and its own gate run is what should redden. What is
never allowed is the reverse — a *silent* 0 — which is why every withheld
discharge is spelled out in the label.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from forseti.esbmc import (
    EsbmcResult,
    ListUnitsError,
    Unit,
    Verified,
    Violated,
    list_address_escapes,
    list_external_callers,
    list_units,
)
from forseti.orchestrator.ladder import verify_ladder
from forseti.orchestrator.ports import VerifyPort

from .synth import (
    DEFAULT_MAX_LEN,
    OBLIGATION_LABEL_PREFIX,
    OBLIGATION_SITE_LABEL_PREFIX,
    ParamRole,
    SynthError,
    UnitPlan,
    inject_obligations,
    plan_unit,
    render_sidecar,
)
from .verify import (
    DEFAULT_LADDER_CAP,
    DEFAULT_TIMEOUT_S,
    Assessment,
    PreconditionResult,
    PreconditionUnavailable,
    escalating_port,
    plan_for,
    precondition_ladder,
    sidecar_verify_port,
    verify_precondition,
)


class CallerOutcome(Enum):
    """What one caller's obligation run established."""

    DISCHARGED = "discharged"  # reaches the call and satisfies the precondition
    OBLIGATION_VIOLATED = "obligation_violated"  # passes a pointer that does not
    CALLER_VIOLATED = "caller_violated"  # fails a memory property of its own first
    UNDERDETERMINED = "underdetermined"  # L0 under-read the *caller*'s own extent
    UNREACHABLE = "unreachable"  # never reaches the call — discharges nothing
    UNCHECKED = "unchecked"  # not materialisable / inconclusive — not a discharge
    UNRESOLVED = "unresolved"  # takes the callee's address — the caller set is open


@dataclass(frozen=True)
class CallerCheck:
    """One caller's contribution to (or withholding of) the discharge.

    `caller` names whatever can reach the callee: a function of this translation
    unit, or — for an ``UNRESOLVED`` escape written at file scope — the object
    whose initialiser holds the callee's address.
    """

    caller: str
    outcome: CallerOutcome
    detail: str
    settled_k: int | None = None
    esbmc_result: EsbmcResult | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "caller": self.caller,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "settled_k": self.settled_k,
        }
        if isinstance(self.esbmc_result, Violated):
            payload["counterexample"] = self.esbmc_result.raw_counterexample
        return payload


@dataclass(frozen=True)
class DischargeResult:
    """The S2 verdict, plus what checking the unit's callers did to it."""

    function: str
    assessment: Assessment
    detail: str
    unit_result: PreconditionResult
    callers: tuple[CallerCheck, ...] = ()

    @property
    def label(self) -> str:
        """The one-line honest verdict."""
        if self.assessment is Assessment.DISCHARGED_VERIFIED:
            return (
                f"VERIFIED (discharged — {self.detail}; up to "
                f"k={self.unit_result.settled_k}, len<={self.unit_result.max_len})"
            )
        if self.assessment is Assessment.ASSUMED_VERIFIED:
            return f"{self.unit_result.label} [{self.detail}]"
        if self.callers:
            return f"VIOLATED at the call site ({self.detail})"
        return self.unit_result.label

    def to_dict(self) -> dict[str, Any]:
        payload = self.unit_result.to_dict()
        payload["assessment"] = self.assessment.value
        payload["assumed"] = self.assessment is Assessment.ASSUMED_VERIFIED
        payload["discharged"] = self.assessment is Assessment.DISCHARGED_VERIFIED
        payload["detail"] = self.detail
        payload["callers"] = [c.to_dict() for c in self.callers]
        return payload


def _memoized(lister: Callable[[Path], list[Unit]]) -> Callable[[Path], list[Unit]]:
    """`lister` with a one-entry-per-path cache, so the TU is parsed once.

    Both phases need the unit list — S2 to plan the callee, S3 to find its
    callers — and `esbmc --parse-tree-only` is not cheap.
    """
    cache: dict[Path, list[Unit]] = {}

    def cached(source: Path) -> list[Unit]:
        if source not in cache:
            cache[source] = lister(source)
        return cache[source]

    return cached


def emit_obligations(
    source: Path,
    *,
    function: str,
    esbmc_bin: str = "esbmc",
    list_units_fn: Callable[[Path], list[Unit]] | None = None,
) -> str:
    """The obligation-injected copy of `source` for `function` (no ESBMC verify).

    The pure injection path exposed for inspection (``forseti discharge
    --emit-only``) — the artefact that answers RFC-0003's OQ1: what the checker
    sees is the user's translation unit plus one assert per pointer parameter,
    and nothing is written back to the user's file.
    """
    lister = list_units_fn or (lambda src: list_units(src, esbmc_bin=esbmc_bin))
    plan = plan_for(source, function, lister)
    try:
        return inject_obligations(source.read_text(), plan)
    except SynthError as exc:
        raise PreconditionUnavailable(
            Assessment.NEEDS_CONTRACT, str(exc), plan
        ) from exc
    except OSError as exc:
        raise PreconditionUnavailable(
            Assessment.ERROR, f"could not read {source}: {exc}", plan
        ) from exc


def discharge_precondition(
    source: Path,
    *,
    function: str,
    max_len: int = DEFAULT_MAX_LEN,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    ladder_cap: int = DEFAULT_LADDER_CAP,
    work_dir: Path | None = None,
    raw_verify: VerifyPort | None = None,
    list_units_fn: Callable[[Path], list[Unit]] | None = None,
    external_callers_fn: Callable[[Path, str], tuple[str, ...]] | None = None,
    address_escapes_fn: Callable[[Path, str], tuple[str, ...]] | None = None,
) -> DischargeResult:
    """Verify `source::function` against its precondition, then discharge it.

    Runs S2 first and *only ever upgrades* its verdict: anything other than
    ``ASSUMED_VERIFIED`` (a real violation, ``NEEDS_CONTRACT``, an error) passes
    through untouched, because there is no assumption to discharge. `raw_verify`,
    `list_units_fn`, `external_callers_fn` and `address_escapes_fn` inject the
    esbmc calls for tests; production adds a ``-I`` for the source's own directory
    so the generated copy's relative ``#include``\\ s still resolve.
    """
    lister = _memoized(
        list_units_fn or (lambda src: list_units(src, esbmc_bin=esbmc_bin))
    )
    unit_result = verify_precondition(
        source,
        function=function,
        max_len=max_len,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        ladder_cap=ladder_cap,
        work_dir=work_dir,
        raw_verify=raw_verify,
        list_units_fn=lister,
    )
    if unit_result.assessment is not Assessment.ASSUMED_VERIFIED:
        return DischargeResult(
            function,
            unit_result.assessment,
            "no assumed pass to discharge",
            unit_result,
        )

    raw = raw_verify or sidecar_verify_port(
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=(f"-I{source.resolve().parent}",),
    )
    external = external_callers_fn or (
        lambda src, sym: list_external_callers(src, sym, esbmc_bin=esbmc_bin)
    )
    escapes = address_escapes_fn or (
        lambda src, sym: list_address_escapes(src, sym, esbmc_bin=esbmc_bin)
    )
    args = (
        source,
        function,
        unit_result,
        lister(source),
        external,
        escapes,
        max_len,
        ladder_cap,
        raw,
    )
    if work_dir is not None:
        return _discharge(*args, work_dir)
    with tempfile.TemporaryDirectory(prefix="forseti-discharge-") as tmp:
        return _discharge(*args, Path(tmp))


def _discharge(
    source: Path,
    function: str,
    unit_result: PreconditionResult,
    units: list[Unit],
    external_callers_fn: Callable[[Path, str], tuple[str, ...]],
    address_escapes_fn: Callable[[Path, str], tuple[str, ...]],
    max_len: int,
    ladder_cap: int,
    raw: VerifyPort,
    work_dir: Path,
) -> DischargeResult:
    plan = unit_result.plan
    assert plan is not None  # an ASSUMED_VERIFIED always carries its plan

    if not plan.pointer_params:
        return DischargeResult(
            function,
            Assessment.DISCHARGED_VERIFIED,
            "no pointer parameter, so the memory precondition is empty and "
            "nothing was assumed of a caller",
            unit_result,
        )
    try:
        source_text = source.read_text()
    except OSError as exc:
        return DischargeResult(
            function, Assessment.ERROR, f"could not read {source}: {exc}", unit_result
        )
    try:
        obligations = inject_obligations(source_text, plan)
        site_probe = inject_obligations(source_text, plan, site_probe=True)
    except SynthError as exc:
        return DischargeResult(
            function, Assessment.NEEDS_CONTRACT, str(exc), unit_result
        )

    callers = tuple(u for u in units if u.name != function and function in u.calls)
    # The two ways the caller set can be wider than what `units` shows. Counting
    # both is what keeps "every caller in this TU" from being a claim about a set
    # we never saw: a `static inline` in an included header is part of this
    # translation unit but is not a unit `list_units` reports, and an address
    # taken rather than called can be invoked from anywhere through the pointer
    # that holds it, under a name no call-graph edge leads back from.
    try:
        foreign = external_callers_fn(source, function)
        escaped = address_escapes_fn(source, function)
    except ListUnitsError as exc:
        return DischargeResult(function, Assessment.ERROR, str(exc), unit_result)
    unenumerable = tuple(
        CallerCheck(
            name,
            CallerOutcome.UNCHECKED,
            f"defined outside {source}, so the gate cannot enumerate it as a unit "
            "or build a harness for it",
        )
        for name in foreign
    ) + tuple(
        CallerCheck(
            site,
            CallerOutcome.UNRESOLVED,
            f"takes the address of {function}() rather than calling it, so an "
            "indirect call the caller enumeration cannot follow may reach it from "
            "anywhere in this translation unit",
        )
        for site in escaped
    )
    if not callers:
        if unenumerable:
            return _aggregate(function, unit_result, unenumerable)
        return DischargeResult(
            function,
            Assessment.ASSUMED_VERIFIED,
            f"discharge incomplete — no caller of {function}() in this translation "
            "unit, so the obligation is exported to its clients rather than "
            "discharged here",
            unit_result,
        )

    obligations_path = work_dir / f"{function}__obligations.c"
    obligations_path.write_text(obligations)
    site_path = work_dir / f"{function}__obligation_site.c"
    site_path.write_text(site_probe)

    checks = tuple(
        _check_caller(
            caller, obligations_path, site_path, max_len, ladder_cap, raw, work_dir
        )
        for caller in callers
    )
    anchored = frozenset(u.name for u in callers if not plan_unit(u).pointer_params)
    return _aggregate(function, unit_result, checks + unenumerable, anchored)


def _weakest_read_pointers(plan: UnitPlan) -> tuple[str, ...]:
    """Pointers `plan` sized by L0's fallback rather than by a stated extent.

    ``SCALAR_PTR`` is the "no length in the signature" case: one object of
    ``sizeof(*p)``, the *weakest* reading a caller could have meant. Every other
    pointer role sizes from something the signature actually states — a companion
    length, or a written array extent — and is therefore authoritative.
    """
    return tuple(p.var for p in plan.pointer_params if p.role is ParamRole.SCALAR_PTR)


def _check_caller(
    caller: Unit,
    obligations: Path,
    site: Path,
    max_len: int,
    ladder_cap: int,
    raw: VerifyPort,
    work_dir: Path,
) -> CallerCheck:
    """Verify `caller`'s own sidecar against the obligation-injected copy."""
    plan = plan_unit(caller)
    if not plan.resolvable:
        return CallerCheck(
            caller.name,
            CallerOutcome.UNCHECKED,
            "L0 cannot materialise the caller's parameter(s): "
            f"{', '.join(plan.unresolved_params)}",
        )

    harness = work_dir / f"{caller.name}__discharge.c"
    harness.write_text(render_sidecar(plan, str(obligations), max_len=max_len))

    settled = None
    for attempt in verify_ladder(
        harness,
        verify=escalating_port(raw),
        ladder=precondition_ladder(max_len, ladder_cap),
    ):
        settled = attempt
    assert settled is not None  # verify_ladder yields at least one attempt
    result, k = settled.result, settled.k

    if isinstance(result, Violated):
        if OBLIGATION_LABEL_PREFIX in result.raw_counterexample:
            weakest = _weakest_read_pointers(plan)
            if weakest:
                # The obligation cannot be blamed on this caller: L0 sized its own
                # object(s) by the fallback "one pointee", the weakest reading of a
                # signature that states no extent. A `void hash_block(uint8_t *blk)`
                # whose real contract is a 16-byte block is *correct code* that
                # cannot satisfy anything wider than one byte — blaming it would
                # just move the phantom VIOLATED from the callee to the call site,
                # which is the failure mode this whole RFC exists to remove. Reading
                # the caller's true extent is L1 (RFC-0003 S4).
                return CallerCheck(
                    caller.name,
                    CallerOutcome.UNDERDETERMINED,
                    "L0 reads no extent for its own pointer(s) "
                    f"({', '.join(weakest)}), so it cannot supply what the callee "
                    "needs and the failure is not attributable to it",
                    k,
                    result,
                )
            return CallerCheck(
                caller.name,
                CallerOutcome.OBLIGATION_VIOLATED,
                "passes a pointer that does not satisfy the callee's precondition",
                k,
                result,
            )
        return CallerCheck(
            caller.name,
            CallerOutcome.CALLER_VIOLATED,
            "violates a memory property of its own, so the obligation was never "
            "reached",
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

    probe_harness = work_dir / f"{caller.name}__discharge_site.c"
    probe_harness.write_text(render_sidecar(plan, str(site), max_len=max_len))
    probe = raw(probe_harness, unwind=k)
    if isinstance(probe, Violated) and OBLIGATION_SITE_LABEL_PREFIX in (
        probe.raw_counterexample
    ):
        return CallerCheck(
            caller.name,
            CallerOutcome.DISCHARGED,
            "reaches the call and satisfies the callee's precondition",
            k,
            result,
        )
    if isinstance(probe, Verified):
        return CallerCheck(
            caller.name,
            CallerOutcome.UNREACHABLE,
            "never reaches the call, so its pass discharges nothing",
            k,
            result,
        )
    return CallerCheck(
        caller.name,
        CallerOutcome.UNCHECKED,
        f"could not confirm the call is reached ({probe.verdict.value})",
        k,
        result,
    )


def _aggregate(
    function: str,
    unit_result: PreconditionResult,
    checks: tuple[CallerCheck, ...],
    anchored: frozenset[str] = frozenset(),
) -> DischargeResult:
    """Fold the per-caller checks into one verdict — upgrading only on a clean sweep.

    `anchored` names the callers whose *own* memory precondition is empty (no
    pointer parameters), so their harness allocates real objects and nothing is
    left assumed on their side. When every discharging caller is anchored the
    chain is closed outright; otherwise the discharge is **relative** to those
    callers' own still-assumed preconditions, and says so — a caller `g` proven
    to call `f` correctly under `g`'s precondition tells you nothing about a
    third function calling `g` badly, and claiming otherwise would over-state
    exactly the way this stage exists to stop.
    """
    broken = tuple(c for c in checks if c.outcome is CallerOutcome.OBLIGATION_VIOLATED)
    if broken:
        names = ", ".join(f"{c.caller}()" for c in broken)
        return DischargeResult(
            function,
            Assessment.VIOLATED,
            f"{names} passes {function}() a pointer that does not satisfy its "
            "synthesised memory precondition",
            unit_result,
            checks,
        )
    withheld = tuple(c for c in checks if c.outcome is not CallerOutcome.DISCHARGED)
    if withheld:
        why = "; ".join(f"{c.caller}(): {c.detail}" for c in withheld)
        return DischargeResult(
            function,
            Assessment.ASSUMED_VERIFIED,
            f"discharge incomplete — {why}",
            unit_result,
            checks,
        )
    anchors = tuple(c.caller for c in checks if c.caller in anchored)
    relative = (
        ""
        if len(anchors) == len(checks)
        else " — relative to each caller's own synthesised precondition, which "
        "stays assumed until that caller is itself discharged"
    )
    return DischargeResult(
        function,
        Assessment.DISCHARGED_VERIFIED,
        f"every caller of {function}() in this translation unit is proven to "
        f"satisfy its memory precondition{relative}",
        unit_result,
        checks,
    )
