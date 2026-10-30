"""Forseti Core's `propose` operation — the harness-neutral property proposer face.

`propose_source` is to the proposer (#65) what `verify_source` is to ESBMC: a thin
Core wrapper that the unified CLI and the MCP tool share, so an adapter sees one
proposal shape regardless of transport. It reads the unit source, best-effort
parses the target function's signature (to unlock the proposer's identifier and
parameter checks), builds an injected `ClaudeCliClient` (ADR-0009 D3), and — when
persisting — opens the `.forseti` `PropertyStore` so each accepted candidate lands
as `status=CANDIDATE`. The returned `ProposalResult.to_dict()` *is* the #44 wire
shape (it already mirrors `core/verify.py`'s payload style), so no separate
serializer lives here.

Scope: this face proposes and (optionally) stores; it does **not** grade
(kill-rate is #4) and does **not** wire the #64 renderability gate — that gate
needs a main-free kernel slice, which the CLI cannot assume of an arbitrary
source, so renderability is enforced downstream at check time (#66). The one
effect is the LLM call, behind the injected `client` seam so tests stay hermetic.
"""

from __future__ import annotations

from pathlib import Path

from forseti.core._proposal_request import (
    prepare_invariant_request,
    prepare_proposal_request,
)
from forseti.core.persistence import persist_proposal
from forseti.properties import (
    MAX_CANDIDATES_DEFAULT,
    ClaudeCliClient,
    LLMClient,
    ProposalResult,
    propose_invariants,
    propose_properties,
)

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 240.0
DEFAULT_MAX_CANDIDATES = MAX_CANDIDATES_DEFAULT
DEFAULT_STORE_ROOT = Path(".forseti")


PROPOSE_KINDS = ("semantic", "rep-invariant")


def invariant_type_name(kind: str, type_name: str | None) -> str | None:
    """The struct type to propose invariants for, or `None` for a semantic proposal.

    The one kind/type consistency rule the CLI and the MCP tools share. `rep_invariant`
    (the stored kind a payload echoes) reads as `rep-invariant`. `ValueError` for an
    unknown kind, a `rep-invariant` without a type, or a type with a semantic kind.
    """
    kind = kind.replace("_", "-")
    if kind not in PROPOSE_KINDS:
        raise ValueError(
            f"kind must be one of {', '.join(PROPOSE_KINDS)}, got {kind!r}"
        )
    if kind == "rep-invariant":
        if not type_name:
            raise ValueError("kind rep-invariant requires a type (--type / type_name)")
        return type_name
    if type_name is not None:
        raise ValueError("a type applies to kind rep-invariant only")
    return None


def propose_source(
    source: Path,
    *,
    function: str,
    persist: bool = True,
    store_root: Path = DEFAULT_STORE_ROOT,
    model: str = DEFAULT_MODEL,
    claude_bin: str = "claude",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    client: LLMClient | None = None,
    recipe: str | None = None,
) -> ProposalResult:
    """Propose candidate properties for a unit's function; optionally store them.

    Reads `source` as the unit text (fed to the prompt verbatim), keys the unit as
    ``<source>::<function>``, and best-effort parses the signature — a parse miss
    (`HarnessError`) degrades to signature-free static checks rather than failing
    the run. The LLM call goes through `client` (a default `ClaudeCliClient` when
    none is injected; an `LLMError` propagates — the proposer never silently yields
    nothing). When `persist` is true each accepted candidate is inserted
    idempotently into `store_root`'s `PropertyStore` as `CANDIDATE`; `persist=False`
    is a dry run that proposes and validates without touching the store. A raw
    `sqlite3.Error` from opening or writing the store (e.g. a corrupt `forseti.db`)
    is translated to `PropertyStoreError`, so both Core faces get a stable
    domain-level failure instead of an uncaught SQLite traceback.

    `recipe` selects what the prompt shows instead of the whole file (`baseline`,
    `sighted`, `blind`; `RecipeError` if unknown or it cannot be built); `None`
    sends the whole file. The recipe's project root is `store_root`'s parent, and
    `callees` shows the accepted contracts `store_root`'s store holds when the
    request is prepared (its digest is in the recorded recipe ref).
    """
    request = prepare_proposal_request(
        source,
        function=function,
        recipe=recipe,
        project_root=store_root.parent,
        store_root=store_root,
    )
    llm = client or ClaudeCliClient(
        model=model, claude_bin=claude_bin, timeout_s=timeout_s
    )

    return persist_proposal(
        lambda store: propose_properties(
            request, client=llm, store=store, max_candidates=max_candidates
        ),
        persist=persist,
        store_root=store_root,
        channel="llm",
    )


def propose_invariant_source(
    source: Path,
    *,
    function: str,
    type_name: str,
    persist: bool = True,
    store_root: Path = DEFAULT_STORE_ROOT,
    model: str = DEFAULT_MODEL,
    claude_bin: str = "claude",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    client: LLMClient | None = None,
    recipe: str | None = None,
) -> ProposalResult:
    """Propose candidate representation invariants for `type_name`; optionally store.

    The invariant counterpart of `propose_source` (RFC-0007 kind 1, #420). `function`
    is a function of `source` that takes a ``type_name *``: the recipe's context is
    built for it (``sighted`` shows it with the type definition; ``baseline`` the
    whole file, i.e. every mutator; ``blind`` only its signature and the type) and the
    prompt names it as the example. `HarnessError` if the type name or function does
    not fit. Survivors of the shared static gate (`invariant_rejection`) are stored as
    ``CANDIDATE`` in the per-type table, keyed ``<source>::<type_name>``; accepting
    one stays a human act (`set_invariant_status`). A stored invariant is *assumed*
    at entry by every check, never discharged at the callers that construct the type.
    The other parameters are `propose_source`'s.
    """
    request = prepare_invariant_request(
        source,
        function=function,
        type_name=type_name,
        recipe=recipe,
        project_root=store_root.parent,
        store_root=store_root,
    )
    llm = client or ClaudeCliClient(
        model=model, claude_bin=claude_bin, timeout_s=timeout_s
    )
    return persist_proposal(
        lambda store: propose_invariants(
            request, client=llm, store=store, max_candidates=max_candidates
        ),
        persist=persist,
        store_root=store_root,
        channel="llm",
    )
