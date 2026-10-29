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
import re
from collections.abc import Sequence
from pathlib import Path

from forseti.core.check import run_check
from forseti.core.persistence import persist_proposal
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.core.submit import DEFAULT_PROMPT_ID, DEFAULT_PROMPT_VERSION
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
from forseti.precond import DEFAULT_MAX_LEN
from forseti.properties import (
    BlankProvenanceError,
    CandidateSpec,
    DuplicateProperty,
    InvariantRole,
    PropertyKind,
    PropertyStore,
    ProposalResult,
    Provenance,
    RejectedCandidate,
    invariant_reason,
    make_invariant,
    normalised_type_name,
    valid_type_name,
)
from forseti.unit_id import make_unit_id


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
    spec = CandidateSpec(
        expression=expression.strip(),
        rationale=rationale,
        kind=PropertyKind.REP_INVARIANT.value,
    )
    reason = _rejection(source.read_text(), type_name, spec.expression)
    prop = make_invariant(
        type_key=type_key,
        expression=spec.expression,
        provenance=Provenance(prompt_id, prompt_version, provider, model),
        description=rationale or None,
    )

    def ingest(store: PropertyStore | None) -> ProposalResult:
        if reason is None and store is not None:
            with contextlib.suppress(DuplicateProperty):
                store.add_type_invariant(prop)
        return ProposalResult(
            unit_id=type_key,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            accepted=(prop,) if reason is None else (),
            rejected=() if reason is None else (RejectedCandidate(spec, reason),),
            model_raw="",
        )

    return persist_proposal(
        ingest, persist=persist, store_root=store_root, channel="submitted"
    )


def _rejection(source_text: str, type_name: str, expression: str) -> str | None:
    if not valid_type_name(type_name):
        return f"{type_name!r} is not a struct type name (ring_t or 'struct node')"
    tag = type_name.split()[-1]
    if re.search(rf"\b{re.escape(tag)}\b", source_text) is None:
        return f"type {type_name!r} does not occur in the source"
    return invariant_reason(expression)


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
        properties_of=lambda store: InvariantsOfType(store, type_key),
    )
