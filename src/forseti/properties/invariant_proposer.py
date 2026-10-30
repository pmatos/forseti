"""The representation-invariant proposer: a struct type in, candidate invariants out.

The `proposer.py` counterpart for RFC-0007 kind 1 (#420). `propose_invariants`
renders the versioned `rep-invariant` prompt for one struct type, asks an
`LLMClient` for candidate predicates over the reserved identifier ``self``, parses
them with the same envelope parser the semantic proposer uses, runs each through the
one static gate every invariant ingest path shares (`invariant_rejection`), and
stores the survivors as ``CANDIDATE`` in the per-type table
(`PropertyStore.add_type_invariant`).

An invariant is keyed by the *type* (``<source>::T``) and shared by every function
that takes ``T *``, so the request carries both the type key and the function whose
recipe context the prompt shows. Accepting a proposed invariant stays a human act
(`forseti invariant-status`); a stored one is *assumed*, never discharged at the
callers that construct the type. Effect-free without a store; the LLM call is the
one effect, behind the injected `client` seam.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .invariant import invariant_rejection, make_invariant, normalised_type_name
from .llm import LLMClient
from .model import Property, PropertyKind, Provenance
from .prompts import (
    DEFAULT_INVARIANT_PROMPT,
    MAX_CANDIDATES_DEFAULT,
    PromptTemplate,
    render_prompt,
)
from .proposer import CandidateSpec, ProposalResult, RejectedCandidate, parse_candidates
from .recipes import ProposalContext
from .store import DuplicateProperty

if TYPE_CHECKING:
    from .store import PropertyStore


# The CLI/prompt spelling and the enum's own value: a host may send either.
_INVARIANT_KINDS = frozenset({"rep-invariant", PropertyKind.REP_INVARIANT.value})


class InvariantStore(Protocol):
    """The store subset the invariant proposer persists into (`PropertyStore` fits)."""

    def get_type_invariant(self, property_id: str) -> Property | None: ...

    def add_type_invariant(self, prop: Property) -> None: ...


@dataclass(frozen=True)
class InvariantRequest:
    """One struct type to propose representation invariants for.

    `type_key` is ``<source>::<type_name>`` (the store key) and `type_name` the
    normalised type. `function` is a function taking ``type_name *``: the recipe
    `context` was built for it, and it is what the prompt names as the example.
    `source_text` is shown to the model when there is no `context`.
    """

    type_key: str
    type_name: str
    function: str
    source_text: str
    prompt: PromptTemplate = DEFAULT_INVARIANT_PROMPT
    context: ProposalContext | None = None


def propose_invariants(
    request: InvariantRequest,
    *,
    client: LLMClient,
    store: InvariantStore | None = None,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> ProposalResult:
    """Propose candidate representation invariants for `request`'s type.

    An `LLMError` from the client or a `ProposalParseError` from a malformed reply
    propagates -- the proposer never silently yields nothing. A candidate that fails
    a static check is returned in `rejected` with its reason and never stored.
    With a `store`, each survivor is inserted idempotently as a ``CANDIDATE`` (its
    content id makes a re-run a no-op); the result's ``unit_id`` is the type key.
    """
    template = request.prompt
    prompt = render_prompt(
        template,
        unit_id=request.type_key,
        symbol=request.function,
        source_text=request.source_text,
        max_candidates=max_candidates,
        context=None if request.context is None else request.context.text,
        type_name=request.type_name,
    )
    raw = client.complete(prompt)
    specs = parse_candidates(raw)
    provenance = Provenance(
        prompt_id=template.prompt_id,
        prompt_version=template.version,
        provider=client.provider,
        model=client.model,
        recipe="" if request.context is None else request.context.recipe,
    )
    accepted, rejected = _accept_reject(
        specs, request=request, provenance=provenance, max_candidates=max_candidates
    )
    if store is not None:
        for prop in accepted:
            if store.get_type_invariant(prop.property_id) is None:
                with contextlib.suppress(DuplicateProperty):
                    store.add_type_invariant(prop)
    return ProposalResult(
        unit_id=request.type_key,
        prompt_id=template.prompt_id,
        prompt_version=template.version,
        provider=client.provider,
        model=client.model,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        model_raw=raw,
    )


def _spec_reason(spec: CandidateSpec, request: InvariantRequest) -> str | None:
    if spec.kind is not None and spec.kind not in _INVARIANT_KINDS:
        return f"non-invariant kind {spec.kind!r} (this run proposes invariants only)"
    if spec.type_name is not None and (
        normalised_type_name(spec.type_name) != request.type_name
    ):
        return f"candidate type {spec.type_name!r} is not {request.type_name!r}"
    if spec.domain or spec.referenced_params:
        return "domain and referenced_params do not apply to an invariant (per type)"
    return invariant_rejection(request.source_text, request.type_name, spec.expression)


def _accept_reject(
    specs: Sequence[CandidateSpec],
    *,
    request: InvariantRequest,
    provenance: Provenance,
    max_candidates: int,
) -> tuple[list[Property], list[RejectedCandidate]]:
    accepted: list[Property] = []
    rejected: list[RejectedCandidate] = []
    seen: set[str] = set()
    for spec in specs:
        if len(accepted) >= max_candidates:
            rejected.append(RejectedCandidate(spec, "over max_candidates"))
            continue
        reason = _spec_reason(spec, request)
        if reason is not None:
            rejected.append(RejectedCandidate(spec, reason))
            continue
        prop = make_invariant(
            type_key=request.type_key,
            expression=spec.expression,
            provenance=provenance,
            description=spec.rationale or None,
        )
        if prop.property_id in seen:
            rejected.append(RejectedCandidate(spec, "duplicate in batch"))
            continue
        seen.add(prop.property_id)
        accepted.append(prop)
    return accepted, rejected


if TYPE_CHECKING:

    def _store_is_invariantstore(s: PropertyStore) -> InvariantStore:
        return s
