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

**And a translation unit is only the whole world for a ``static`` callee.** An
externally visible one can be named — and handed anything — by any other TU of
the linked program, none of which this command sees. So a clean local sweep of a
non-``static`` callee leaves ``ASSUMED_VERIFIED`` with its obligation *exported*,
exactly as a callee with no local caller at all does; only internal linkage makes
"every caller in this translation unit" mean every caller. `Unit.internal_linkage`
reads the ``static`` marker off the same clang dump.

**A caller set is only as complete as the call graph.** Callers are found by name,
so a body that calls ``fp(...)`` references the *variable* and no edge leads back
to whatever ``fp`` holds: a ``static cb_t fp = f;`` plus one indirect call is a
caller of ``f`` that no enumeration built from `Unit.calls` will ever list. So the
upgrade is withheld whenever `parse_address_escapes` finds ``f`` named outside a
direct call (``UNRESOLVED``) — an address taken, or an attribute like a
``cleanup`` handler, which clang prints as the same kind of reference. The caller
set is then not enumerable, and a sweep of the callers we *could* see says
nothing about the ones we could not.
That makes a function in a dispatch table permanently ``ASSUMED_VERIFIED``, which
is the honest reading: it really can be invoked from anywhere in the TU with
anything. A second *name* for the callee opens the set the same way and for the
same reason — a call written through it references only that name — so
`parse_symbol_aliases` withholds for a GNU ``alias`` attribute and for a shared
``__asm__`` label alike. Following an indirect call to its
targets is L1/S4 work.

**A GNU ``asm("call f")`` is invisible to the call graph in a stronger sense
still.** It names its target only inside the assembly string, which never
produces a ``DeclRefExpr`` — so unlike a function pointer, this path leaves no
reference of *any* kind for `parse_address_escapes` to find either. Worse, the
string itself is never printed by clang's own AST dump (checked against both
esbmc's pinned clang and a current upstream one), so no scan run over this
format can tell which symbol, if any, a given inline-asm block invokes.
`parse_asm_statements` therefore withholds unconditionally: every such block
found in `source` opens the caller set for whatever function is under
discharge, since none can be ruled out. That is wider than strictly necessary
whenever the block does something unrelated (a memory barrier, a syscall), but
narrower is not an option this AST format supports.

**A recursive callee is a call site of itself.** Asserting at the entry rather than
transplanting a contract has one property a modular check would not: the assert
fires at *every* entry, so a re-entry that breaks the precondition cannot slip
past — it fails inside whatever caller's run reached it. What that costs is
*blame*: one failing run cannot say which entry broke it. So a callee that can
re-enter itself (`_self_reachable`, direct or through a cycle) is verified against
its own harness too, which satisfies the obligation at the outer entry by
construction; if that settles the re-entry as safe, a failure elsewhere is the
caller's and is named, and if it does not, the caller's failure is
``UNATTRIBUTED`` and the recursion is what the report names. A re-entry never
*anchors* a chain, so a callee whose only in-TU call site is its own recursion
still exports its obligation.

**What a discharge is relative to.** Each caller's own parameters are materialised
by *its* S2 assumption, so one run proves ``caller precondition => callee
precondition`` at every call site: one link of the chain, not the whole chain.
Closing a deeper chain means discharging each link in turn. The exception is an
**anchored** caller — one whose own precondition is empty (no pointer parameters),
whose harness therefore allocates real objects and leaves nothing assumed *about
its parameters*. The label states the caveat and drops it only when every caller
is anchored.

**What a single caller check does and does not cover.** Every caller — anchored
or not — is verified from exactly one call in a harness ``main``, with the
translation unit's own file-scope and ``static`` variables left at whatever
value their own initialisers give them; RFC-0003 S2 materialises *parameters*
only, never ambient state. A caller whose forwarded argument depends on mutable
static/global state a real program could have changed by a later call — the
classic "pick a smaller buffer once a flag has been flipped" — is therefore
checked for that one entry, not for every entry a real program could make.
Dropping the "relative" caveat says the parameter dimension is closed; it says
nothing about ambient state, which is why the claim below states its own scope
instead of leaving it implicit (RFC-0003 S4).

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
from dataclasses import dataclass, replace
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
    list_asm_statements,
    list_external_callers,
    list_implicit_invocations,
    list_symbol_aliases,
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
    UNATTRIBUTED = "unattributed"  # a failure the callee's own re-entry can explain


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
        """The one-line honest verdict.

        The S2 label passes through **only** when S2's own verdict is what this
        result carries. A failure of the discharge machinery itself (an unreadable
        source, an uninjectable definition, a failed TU listing) leaves S2's
        ``ASSUMED_VERIFIED`` label attached to an ``ERROR``, which the CLI would
        print as if verification had succeeded.
        """
        if self.assessment is Assessment.DISCHARGED_VERIFIED:
            return f"VERIFIED (discharged — {self.detail})"
        if self.assessment is Assessment.ASSUMED_VERIFIED:
            return f"{self.unit_result.label} [{self.detail}]"
        if self.callers:
            return f"VIOLATED at the call site ({self.detail})"
        if self.assessment is self.unit_result.assessment:
            return self.unit_result.label
        return f"{self.assessment.value.upper()} ({self.detail})"

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
        return inject_obligations(
            source.read_text(), plan, source_path=str(source.resolve())
        )
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
    aliases_fn: Callable[[Path, str], tuple[str, ...]] | None = None,
    implicit_invocations_fn: Callable[[Path, str], tuple[str, ...]] | None = None,
    asm_statements_fn: Callable[[Path], tuple[str, ...]] | None = None,
) -> DischargeResult:
    """Verify `source::function` against its precondition, then discharge it.

    Runs S2 first and *only ever upgrades* its verdict: anything other than
    ``ASSUMED_VERIFIED`` (a real violation, ``NEEDS_CONTRACT``, an error) passes
    through untouched, because there is no assumption to discharge. `raw_verify`,
    `list_units_fn`, `external_callers_fn`, `address_escapes_fn`, `aliases_fn`,
    `implicit_invocations_fn` and `asm_statements_fn` inject the esbmc calls for
    tests; production adds a ``-I`` for the source's own directory so the
    generated copy's relative ``#include``\\ s still resolve. `asm_statements_fn`
    takes no `symbol` — see `parse_asm_statements` for why one cannot narrow it.
    """
    lister = _memoized(
        list_units_fn
        or (lambda src: list_units(src, esbmc_bin=esbmc_bin, timeout_s=timeout_s))
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
        lambda src, sym: list_external_callers(
            src, sym, esbmc_bin=esbmc_bin, timeout_s=timeout_s
        )
    )
    escapes = address_escapes_fn or (
        lambda src, sym: list_address_escapes(
            src, sym, esbmc_bin=esbmc_bin, timeout_s=timeout_s
        )
    )
    aliases = aliases_fn or (
        lambda src, sym: list_symbol_aliases(
            src, sym, esbmc_bin=esbmc_bin, timeout_s=timeout_s
        )
    )
    implicit_invocations = implicit_invocations_fn or (
        lambda src, sym: list_implicit_invocations(
            src, sym, esbmc_bin=esbmc_bin, timeout_s=timeout_s
        )
    )
    asm_statements = asm_statements_fn or (
        lambda src: list_asm_statements(src, esbmc_bin=esbmc_bin, timeout_s=timeout_s)
    )
    args = (
        source,
        function,
        unit_result,
        lister(source),
        external,
        escapes,
        aliases,
        implicit_invocations,
        asm_statements,
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
    aliases_fn: Callable[[Path, str], tuple[str, ...]],
    implicit_invocations_fn: Callable[[Path, str], tuple[str, ...]],
    asm_statements_fn: Callable[[Path], tuple[str, ...]],
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
    # The obligation-injected copy is written under its own path in `work_dir`,
    # not the user's — so without a `#line` directive, `__FILE__` inside it would
    # report *that* path rather than the source's, and a caller whose behaviour
    # or object sizing depends on `__FILE__` would be checked against a program
    # that is not quite the one being verified (RFC-0003 OQ1's "generated copy"
    # promise is about the check, not about what the copy claims to be).
    resolved_source = str(source.resolve())
    try:
        obligations = inject_obligations(source_text, plan, source_path=resolved_source)
        site_probe = inject_obligations(
            source_text, plan, site_probe=True, source_path=resolved_source
        )
    except SynthError as exc:
        return DischargeResult(
            function, Assessment.NEEDS_CONTRACT, str(exc), unit_result
        )

    referrers = tuple(u for u in units if u.name != function and function in u.calls)
    # A callee that can re-enter itself is a call site of its own, and one the
    # obligation *already* checks: the assert sits at the entry, so it fires for
    # every re-entry inside whatever caller's run reached it. Verifying the callee
    # against its own harness is what tells the two apart — that harness satisfies
    # the obligation at the outer entry by construction, so a failure there is the
    # re-entry's, and no caller should be blamed for it.
    reentrant = (
        tuple(u for u in units if u.name == function)
        if _self_reachable(units, function)
        else ()
    )
    # Whether this TU is the callee's whole world. A `static` callee cannot be
    # named from another one, so "every caller here" is every caller; an
    # externally visible one exports the obligation to clients we never see.
    internal = any(u.internal_linkage for u in units if u.name == function)
    # The five ways the caller set can be wider than what `units` shows, each of
    # which keeps "every caller in this TU" from being a claim about a set we
    # never saw: a `static inline` in an included header is part of this
    # translation unit but is not a unit `list_units` reports; an address taken
    # rather than called can be invoked from anywhere through the pointer that
    # holds it; an alias attribute gives the callee a second name whose call
    # sites reference only that name; a constructor/destructor attribute names
    # no one at all — the loader invokes the callee's own declaration directly,
    # supplying none of the arguments any explicit caller does; and a GNU inline
    # ``asm("call f")`` names its target only inside a string clang's own AST
    # dump never prints, so no scan can tell which symbol, if any, it reaches.
    try:
        foreign = external_callers_fn(source, function)
        escaped = address_escapes_fn(source, function)
        aliased = aliases_fn(source, function)
        implicit = implicit_invocations_fn(source, function)
        asm_sites = asm_statements_fn(source)
    except ListUnitsError as exc:
        return DischargeResult(function, Assessment.ERROR, str(exc), unit_result)
    unenumerable = (
        tuple(
            CallerCheck(
                name,
                CallerOutcome.UNCHECKED,
                f"defined outside {source}, so the gate cannot enumerate it as a unit "
                "or build a harness for it",
            )
            for name in foreign
        )
        + tuple(
            CallerCheck(
                site,
                CallerOutcome.UNRESOLVED,
                f"names {function}() outside a direct call — an address taken, or "
                "an attribute such as a cleanup handler — so a call path the "
                "enumeration cannot follow may reach it from anywhere in this "
                "translation unit",
            )
            for site in escaped
        )
        + tuple(
            CallerCheck(
                name,
                CallerOutcome.UNRESOLVED,
                f"is another name for {function}() (an alias attribute or a shared "
                "assembly label), and a call written through it references only "
                "that name, so its callers are not in the set that was checked",
            )
            for name in aliased
        )
        + tuple(
            CallerCheck(
                f"<{kind}>",
                CallerOutcome.UNRESOLVED,
                f"carries a {kind} attribute, so the loader invokes {function}() "
                "directly at load/unload time, supplying none of the arguments "
                "any explicit call site in this translation unit does",
            )
            for kind in implicit
        )
        + tuple(
            CallerCheck(
                site,
                CallerOutcome.UNRESOLVED,
                "contains a GNU inline-assembly statement whose text clang's own "
                "AST dump never prints, so it cannot be shown not to reach "
                f"{function}() by a path that leaves no `DeclRefExpr` for this "
                "enumeration to ever see",
            )
            for site in asm_sites
        )
    )
    # A re-entry is never an *anchor*: something outside the recursion has to
    # enter the callee in the first place, so with no other caller the obligation
    # is still exported rather than discharged here.
    if not referrers:
        if unenumerable:
            return _aggregate(function, unit_result, unenumerable, internal=internal)
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

    callers = reentrant + referrers
    checks = _attributed(
        tuple(
            _check_caller(
                caller, obligations_path, site_path, max_len, ladder_cap, raw, work_dir
            )
            for caller in callers
        ),
        function,
        reentrant=bool(reentrant),
    )
    anchored = frozenset(u.name for u in callers if not plan_unit(u).pointer_params)
    return _aggregate(function, unit_result, checks + unenumerable, anchored, internal)


def _self_reachable(units: list[Unit], function: str) -> bool:
    """Whether `function` can re-enter itself through this TU's references.

    A breadth-first walk of `Unit.calls`, so an indirect cycle (``f`` → ``g`` →
    ``f``) counts as well as a direct self-call. `Unit.calls` over-approximates
    (an address taken reads as a reference), so this can report a re-entry that
    cannot happen — costing one extra verification and a softer blame, never a
    claimed discharge.
    """
    calls = {u.name: u.calls for u in units}
    seen: set[str] = set()
    frontier = set(calls.get(function, ()))
    while frontier:
        if function in frontier:
            return True
        seen |= frontier
        frontier = {c for name in frontier for c in calls.get(name, ())} - seen
    return False


def _attributed(
    checks: tuple[CallerCheck, ...], function: str, *, reentrant: bool
) -> tuple[CallerCheck, ...]:
    """`checks` with obligation failures a re-entry can explain taken off callers.

    The obligation is asserted at the callee's entry, so one failing run cannot
    say *which* entry broke it — this caller's, or a re-entry reached from it. When
    the callee's own harness settles that the re-entry keeps the obligation, a
    failure elsewhere is the caller's and is named. When it does not, blaming the
    caller would accuse code that passed a perfectly valid pointer, so the failure
    becomes ``UNATTRIBUTED``: the upgrade is withheld and the recursion is what
    the report names.
    """
    if not reentrant:
        return checks
    settled = any(
        c.caller == function and c.outcome is CallerOutcome.DISCHARGED for c in checks
    )
    if settled:
        return checks
    return tuple(
        replace(
            check,
            outcome=CallerOutcome.UNATTRIBUTED,
            detail=f"{function}() can re-enter itself and the obligation is checked "
            "at every entry, so this failure is as likely the re-entry's as this "
            "caller's, and is not attributable to it",
        )
        if check.caller != function
        and check.outcome is CallerOutcome.OBLIGATION_VIOLATED
        else check
        for check in checks
    )


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
    internal: bool = False,
) -> DischargeResult:
    """Fold the per-caller checks into one verdict — upgrading only on a clean sweep.

    A clean sweep upgrades only when `internal` says the callee is ``static``:
    for an externally visible one, every caller *here* is not every caller, since
    any other translation unit of the program can name it and pass what it likes.
    That obligation is exported exactly as it is for a callee with no local caller
    at all, and is reported the same way.

    `anchored` names the callers whose *own* memory precondition is empty (no
    pointer parameters), so their harness allocates real objects and nothing is
    left assumed about their parameters. When every discharging caller is
    anchored the chain closes outright for that dimension; otherwise the
    discharge is **relative** to those callers' own still-assumed preconditions,
    and says so — a caller `g` verified to call `f` correctly under `g`'s
    precondition tells you nothing about a third function calling `g` badly, and
    claiming otherwise would over-state exactly the way this stage exists to
    stop. Either way, the claim is scoped to one invocation of each caller from
    this translation unit's own initial state: a caller whose forwarded argument
    depends on mutable static/global state it can change between real calls is
    not re-checked after that state changes, so the claim says so instead of
    reading as a closure over every invocation a real program could make.
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
    if not internal:
        return DischargeResult(
            function,
            Assessment.ASSUMED_VERIFIED,
            f"discharge incomplete — every caller of {function}() in this "
            "translation unit satisfies its memory precondition, but the function "
            "is externally visible, so a client in another translation unit can "
            "call it with anything; the obligation is exported there, and closing "
            f"it needs {function}() to be static or discharged in that TU too",
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
    # The bound belongs *inside* the claim, not appended by the label: `detail` is
    # what `--json` hands a consumer on its own, and an unqualified "every caller
    # satisfies it" would read as a statement for all inputs rather than one up to
    # k (CLAUDE.md's vocabulary discipline). The invocation scope is the same
    # kind of thing and belongs inside for the same reason: it is true of *every*
    # discharge, anchored or relative, since `render_sidecar` calls each caller
    # once with the TU's static/global state left at its own initial value — a
    # caller whose forwarded argument depends on mutable ambient state it can
    # change between real calls is not re-checked after that state changes.
    return DischargeResult(
        function,
        Assessment.DISCHARGED_VERIFIED,
        f"every caller of {function}() in this translation unit is verified to "
        f"satisfy its memory precondition up to k={unit_result.settled_k}, "
        f"len<={unit_result.max_len}, for one invocation of each caller from "
        f"this translation unit's initial state{relative}",
        unit_result,
        checks,
    )
