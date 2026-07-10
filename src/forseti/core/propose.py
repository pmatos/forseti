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

import sqlite3
from pathlib import Path

from forseti.properties import (
    MAX_CANDIDATES_DEFAULT,
    ClaudeCliClient,
    HarnessError,
    LLMClient,
    PropertyStore,
    PropertyStoreError,
    ProposalRequest,
    ProposalResult,
    UnitSignature,
    extract_signature,
    propose_properties,
)

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_CANDIDATES = MAX_CANDIDATES_DEFAULT
DEFAULT_STORE_ROOT = Path(".forseti")


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
    """
    source_text = source.read_text()
    unit_id = f"{source}::{function}"
    signature: UnitSignature | None
    try:
        signature = extract_signature(source_text, function)
    except HarnessError:
        signature = None

    request = ProposalRequest(
        unit_id=unit_id, source_text=source_text, signature=signature
    )
    llm = client or ClaudeCliClient(
        model=model, claude_bin=claude_bin, timeout_s=timeout_s
    )

    if not persist:
        return propose_properties(request, client=llm, max_candidates=max_candidates)

    store: PropertyStore | None = None
    try:
        store = PropertyStore.open(store_root)
        return propose_properties(
            request, client=llm, store=store, max_candidates=max_candidates
        )
    except sqlite3.Error as exc:
        raise PropertyStoreError(
            f"property store error at {store_root}: {exc}"
        ) from exc
    finally:
        if store is not None:
            store.close()
