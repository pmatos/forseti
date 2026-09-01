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

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from forseti.core.events import record_property_proposed
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.properties import (
    CandidateSpec,
    HarnessError,
    PromptTemplate,
    PropertyStore,
    PropertyStoreError,
    ProposalRequest,
    ProposalResult,
    UnitSignature,
    extract_signature,
    submit_candidates,
)
from forseti.properties.prompts import MAX_CANDIDATES_DEFAULT

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
    rather than failing the run. `provider`/`model` are required (never
    defaulted to a Core-owned backend) so a submitted property's provenance
    always names its real origin. `prompt_id`/`prompt_version` default to a
    `"host-submitted"` marker for a candidate with no versioned Core prompt
    behind it; a caller that *does* have one (e.g. a subagent following a
    published prompt spec) can pass it through instead. When `persist` is
    true the candidate is inserted idempotently into `store_root`'s
    `PropertyStore` as `CANDIDATE`; `persist=False` is a dry run that
    validates without touching the store. A raw `sqlite3.Error` from opening
    or writing the store is translated to `PropertyStoreError`, mirroring
    `propose_source`/`check_source`.
    """
    source_text = source.read_text()
    unit_id = f"{source}::{function}"
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
    spec = CandidateSpec(
        expression=expression,
        domain=tuple(domain),
        referenced_params=tuple(referenced_params),
        rationale=rationale,
    )

    if not persist:
        # No event recorded: `record_event` would create `store_root` (its
        # parent `mkdir`), breaking the "dry run touches nothing" contract
        # `propose_source` also holds itself to -- a candidate never
        # persisted has nothing durable to trace either.
        return submit_candidates(
            request,
            (spec,),
            provider=provider,
            model=model,
            max_candidates=max_candidates,
        )

    try:
        with PropertyStore.open(store_root) as store:
            result = submit_candidates(
                request,
                (spec,),
                provider=provider,
                model=model,
                store=store,
                max_candidates=max_candidates,
            )
    except sqlite3.Error as exc:
        raise PropertyStoreError(
            f"property store error at {store_root}: {exc}"
        ) from exc
    record_property_proposed(store_root, result, channel="submitted")
    return result
