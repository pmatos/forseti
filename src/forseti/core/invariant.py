"""Forseti Core's representation-invariant operations (RFC-0007 kind 1, #387).

`submit_invariant_source` stores a hand-written invariant for a struct *type*
(`<source>::<type>`, shared by every function taking that type) through the same
persistence boundary and ``ProposalResult`` shape `submit_source` uses.
`check_invariant_source` checks one function against the type's stored invariants:
nondet ``T``, assume the invariant at entry, call, assert it at exit (an initialiser
skips the assumption), each with the post-call vacuity probe, through the ordinary
`check_properties` driver. The invariant is *assumed*, never discharged at callers:
every verdict says so (`INVARIANT_ASSUMPTION_NOTE`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from forseti.core.check import run_check
from forseti.core.persistence import open_store, persist_proposal, store_exists
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.core.submit import DEFAULT_PROMPT_ID, DEFAULT_PROMPT_VERSION
from forseti.esbmc import Unit as ClangUnit
from forseti.orchestrator import (
    InvariantHarnessWriter,
    InvariantsOfType,
    PropertyCheckRun,
    Unit,
    VerifyPort,
)
from forseti.orchestrator.semantic_check import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_UNWIND,
    SemanticCheckPlan,
    validated_max_len,
)
from forseti.precond import DEFAULT_MAX_LEN, CallerCheck
from forseti.precond.invariant_discharge import (
    InvariantDischargeRun,
    discharge_invariants,
)
from forseti.properties import (
    INVARIANT_CHECKABLE_STATUSES,
    BlankProvenanceError,
    CandidateSpec,
    DuplicateProperty,
    InvariantRole,
    Property,
    PropertyKind,
    PropertyNotFound,
    PropertyStatus,
    PropertyStore,
    ProposalResult,
    Provenance,
    RejectedCandidate,
    invariant_rejection,
    make_invariant,
    normalised_type_name,
)
from forseti.unit_id import make_unit_id, unit_id_prefix, unit_id_symbol


def submit_invariant_source(
    source: Path,
    *,
    type_name: str,
    expression: str,
    provider: str,
    model: str,
    rationale: str = "",
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    persist: bool = True,
    store_root: Path = DEFAULT_STORE_ROOT,
) -> ProposalResult:
    """Validate and (optionally) store a representation invariant for `type_name`.

    `expression` is a pure C boolean expression over ``self``, a ``const T *``
    (``self->count <= CAP``). Static checks only (`invariant_reason`, a name that
    occurs in `source`); whether it is *vacuous* is the check's non-vacuity probe's
    to say. The result carries the type key (``<source>::<type_name>``) as its
    ``unit_id``; a rejection is returned, not raised. `BlankProvenanceError` on a
    blank `provider`/`model`, as for `submit_source`. Idempotent per content id.
    """
    if not provider.strip() or not model.strip():
        raise BlankProvenanceError(
            f"provider={provider!r} and model={model!r} must both be nonblank"
        )
    type_name = normalised_type_name(type_name)
    type_key = make_unit_id(source, type_name)
    expression = expression.strip()
    reason = invariant_rejection(source.read_text(), type_name, expression)
    prop = make_invariant(
        type_key=type_key,
        expression=expression,
        provenance=Provenance(prompt_id, prompt_version, provider, model),
        description=rationale or None,
    )

    def ingest(store: PropertyStore | None) -> ProposalResult:
        if reason is None and store is not None:
            with contextlib.suppress(DuplicateProperty):
                store.add_type_invariant(prop)
        rejected = (
            ()
            if reason is None
            else (
                RejectedCandidate(
                    CandidateSpec(
                        expression,
                        rationale=rationale,
                        kind=PropertyKind.REP_INVARIANT.value,
                    ),
                    reason,
                ),
            )
        )
        return ProposalResult(
            unit_id=type_key,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            accepted=(prop,) if reason is None else (),
            rejected=rejected,
            model_raw="",
        )

    return persist_proposal(
        ingest, persist=persist, store_root=store_root, channel="submitted"
    )


def set_invariant_status(
    source: Path,
    *,
    type_name: str,
    property_id: str,
    status: PropertyStatus,
    store_root: Path = DEFAULT_STORE_ROOT,
) -> Property:
    """Accept or reject the stored invariant `property_id` of `source`::`type_name`.

    ``CANDIDATE -> ACCEPTED | REJECTED`` (`is_valid_invariant_transition`); an
    accepted invariant is the context `forseti synth` assumes for every ``T *``
    function (`accepted_invariants`), assumed and never discharged at the callers
    that build a ``T``. The id must belong to that type: an id of another type or
    file is `PropertyNotFound`, so a mistyped ``--type`` cannot settle the wrong
    row. `InvalidStatusTransition` (a `ValueError`) for a refused move; a missing store
    is `PropertyNotFound` and is not created; a failed
    read or write is `PropertyStoreError`.
    """
    type_key = make_unit_id(source, normalised_type_name(type_name))
    if not store_exists(store_root):
        raise PropertyNotFound(property_id)  # nothing stored: do not create a store
    with open_store(store_root) as store:
        stored = store.get_type_invariant(property_id)
        if stored is None or stored.unit_id != type_key:
            raise PropertyNotFound(property_id)
        return store.update_type_invariant_status(property_id, status)


def accepted_invariants(
    source: Path, *, store_root: Path = DEFAULT_STORE_ROOT
) -> dict[str, tuple[str, ...]]:
    """The accepted invariant expressions of each struct type of `source`.

    The S0 context (#387): ``{type name: expressions}``, empty when nothing was
    accepted -- including when `store_root` has no store at all, which this read
    never creates. Keyed by the raw ``source`` spelling the invariants were
    submitted under, like every unit id.
    """
    if not store_exists(store_root):
        return {}
    by_type: dict[str, list[str]] = {}
    with open_store(store_root) as store:
        for prop in store.list_for_type_prefix(
            unit_id_prefix(source), frozenset({PropertyStatus.ACCEPTED})
        ):
            by_type.setdefault(unit_id_symbol(prop.unit_id), []).append(prop.expression)
    return {name: tuple(exprs) for name, exprs in by_type.items()}


def check_invariant_source(
    source: Path,
    *,
    function: str,
    type_name: str,
    role: InvariantRole | None = None,
    store_root: Path = DEFAULT_STORE_ROOT,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    verify_port: VerifyPort | None = None,
    max_len: int | None = DEFAULT_MAX_LEN,
) -> PropertyCheckRun:
    """Check `source`::`function` against the stored invariants of `type_name`.

    One verdict per stored, non-terminal invariant, each ``held`` (VERIFIED up to k,
    *assuming* the invariant at entry), ``violated`` (the exit assertion, or a
    built-in safety check, fails under it), ``vacuous`` (the assumed invariant admits
    no state the call returns from, e.g. ``0``) or ``unknown`` / ``error``. A method
    that only takes ``const T *`` is ``skipped``. `role` defaults to the one inferred
    from the function name (`infer_role`). The remaining knobs are `check_source`'s.
    """
    type_name = normalised_type_name(type_name)
    unit = Unit.from_path(source, function)
    plan = SemanticCheckPlan(
        unwind=unwind,
        unwind_ladder=unwind_ladder,
        max_len=max_len,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=extra_flags,
        render=InvariantHarnessWriter(max_len=validated_max_len(max_len), role=role),
    )
    type_key = make_unit_id(source, type_name)
    return run_check(
        unit,
        source=source,
        plan=plan,
        store_root=store_root,
        unwind=unwind,
        verify_port=verify_port,
        properties_of=partial(InvariantsOfType, type_key=type_key),
    )


def discharge_invariant_source(
    source: Path,
    *,
    type_name: str,
    initialisers: Sequence[str] = (),
    store_root: Path = DEFAULT_STORE_ROOT,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    verify_port: VerifyPort | None = None,
    list_units_fn: Callable[[Path], list[ClangUnit]] | None = None,
    open_callers_fn: Callable[[Path, str], tuple[CallerCheck, ...]] | None = None,
) -> InvariantDischargeRun:
    """Check the callers of `source`'s ``type_name *`` functions against its invariants.

    The counterpart of `check_invariant_source`'s *assumed* reading (RFC-0007 kind 1,
    #430): for each stored, non-rejected invariant of `type_name` and each function
    of `source` that assumes it (a ``T *`` parameter, not an initialiser: `infer_role`
    or named in `initialisers`), the invariant is asserted at that function's entry in
    a generated copy of the file and every closed caller (one taking no parameters,
    e.g. ``main``) is verified against it. A status is ``discharged`` only when the
    callee is ``static``, every caller was closed, reached the call and passed; a
    caller that hands over a state the invariant does not hold on is ``violated``;
    anything else stays *assumed*, with the reason. All bounded: VERIFIED up to k.

    A missing store, or no stored invariant, gives an empty run (never created or
    read as a discharge). The remaining knobs are `check_source`'s;
    `list_units_fn`/`open_callers_fn` inject the clang listing for tests.
    """
    type_name = normalised_type_name(type_name)
    plan = SemanticCheckPlan(
        unwind=unwind,
        unwind_ladder=unwind_ladder,
        max_len=None,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=extra_flags,
    )
    type_key = make_unit_id(source, type_name)
    invariants: tuple[Property, ...] = ()
    if store_exists(store_root):
        with open_store(store_root) as store:
            invariants = tuple(
                store.list_for_type(type_key, INVARIANT_CHECKABLE_STATUSES)
            )
    discharges = discharge_invariants(
        source,
        type_name=type_name,
        invariants=invariants,
        port=verify_port or plan.verify_for(source),
        unwind=unwind,
        unwind_ladder=plan.ladder,
        initialisers=frozenset(initialisers),
        esbmc_bin=esbmc_bin,
        list_units_fn=list_units_fn,
        open_callers_fn=open_callers_fn,
    )
    return InvariantDischargeRun(type_key, discharges)
