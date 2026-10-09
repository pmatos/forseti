"""Forseti Core's `submit` operation — ingest a host-generated candidate (#213).

`submit_source` is `propose_source`'s LLM-free sibling: instead of asking an
`LLMClient` for candidates, the caller already has one (a Claude Code subagent's
own model call, a Codex host-model turn, or any other proposer a project wants
to configure) and hands it straight to Core as an already-formed expression +
domain. Core still applies the exact same static validation `propose_source`
would (`submit_candidates` -> `_accept_reject`, the shared gate both paths run
through) and persists survivors the same way -- a submitted candidate cannot
bypass a check an LLM-proposed one is held to.

This is what makes the semantic-check path provider-neutral: nothing here
shells out to `claude` or any other model binary. `provider`/`model` are
caller-supplied strings recorded on the persisted property's `Provenance` for
traceability -- never invoked, never guessed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from forseti.core.persistence import persist_proposals
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.properties import (
    CandidateSpec,
    HarnessError,
    PromptTemplate,
    PropertyStore,
    ProposalRequest,
    ProposalResult,
    UnitSignature,
    extract_signature,
    submit_candidates,
)
from forseti.properties.prompts import MAX_CANDIDATES_DEFAULT
from forseti.unit_id import make_unit_id

DEFAULT_MAX_CANDIDATES = MAX_CANDIDATES_DEFAULT
DEFAULT_PROMPT_ID = "host-submitted"
DEFAULT_PROMPT_VERSION = "1"


def submit_source(
    source: Path,
    *,
    function: str,
    expression: str,
    provider: str,
    model: str,
    domain: Sequence[str] = (),
    referenced_params: Sequence[str] = (),
    rationale: str = "",
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    persist: bool = True,
    store_root: Path = DEFAULT_STORE_ROOT,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> ProposalResult:
    """Validate and (optionally) store one host-supplied candidate property.

    Reads `source` as the unit text, keys the unit as ``<source>::<function>``,
    and best-effort parses the signature the same way `propose_source` does --
    a parse miss (`HarnessError`) degrades to signature-free static checks
    rather than failing the run. `provider`/`model` are required and must be
    nonblank (never defaulted to a Core-owned backend, `BlankProvenanceError`
    on a blank value) so a submitted property's provenance always names its
    real origin. `prompt_id`/`prompt_version` default to a
    `"host-submitted"` marker for a candidate with no versioned Core prompt
    behind it; a caller that *does* have one (e.g. a subagent following a
    published prompt spec) can pass it through instead. When `persist` is
    true the candidate is inserted idempotently into `store_root`'s
    `PropertyStore` as `CANDIDATE`; `persist=False` is a dry run that
    validates without touching the store. A raw `sqlite3.Error` from opening
    or writing the store is translated to `PropertyStoreError`, mirroring
    `propose_source`/`check_source`.
    """
    spec = CandidateSpec(
        expression=expression,
        domain=tuple(domain),
        referenced_params=tuple(referenced_params),
        rationale=rationale,
    )
    return _submit_sources(
        source,
        function=function,
        candidates=(spec,),
        provider=provider,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        persist=persist,
        store_root=store_root,
        max_candidates=max_candidates,
    )[0]


def _submit_sources(
    source: Path,
    *,
    function: str,
    candidates: Sequence[CandidateSpec],
    provider: str,
    model: str,
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    persist: bool = True,
    store_root: Path = DEFAULT_STORE_ROOT,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> tuple[ProposalResult, ...]:
    """Submit ordered candidates with one prepared unit and one store lifetime.

    Keep one result per candidate. Each call to ``submit_candidates`` retains the
    singleton validation and dedup semantics, while the accepted-count budget is
    shared across the sequence. ``kind`` is intentionally omitted: the existing
    semantic-loop submit face forwarded only the four candidate content fields.
    """
    source_text = source.read_text()
    unit_id = make_unit_id(source, function)
    signature: UnitSignature | None
    try:
        signature = extract_signature(source_text, function)
    except HarnessError:
        signature = None

    request = ProposalRequest(
        unit_id=unit_id,
        source_text=source_text,
        prompt=PromptTemplate(prompt_id=prompt_id, version=prompt_version, template=""),
        signature=signature,
    )

    def ingest(store: PropertyStore | None) -> Iterator[ProposalResult]:
        accepted_count = 0
        for candidate in candidates:
            spec = CandidateSpec(
                expression=candidate.expression,
                domain=candidate.domain,
                referenced_params=candidate.referenced_params,
                rationale=candidate.rationale,
            )
            result = submit_candidates(
                request,
                (spec,),
                provider=provider,
                model=model,
                store=store,
                max_candidates=max(max_candidates - accepted_count, 0),
            )
            accepted_count += len(result.accepted)
            yield result

    return persist_proposals(
        ingest,
        persist=persist,
        store_root=store_root,
        channel="submitted",
    )
