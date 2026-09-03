"""Forseti Core's property-ingest persistence boundary — one home for it.

`propose_source`, `submit_source`, and (for the store-open concern) `check_source`
all cross the same boundary when they persist: open the `.forseti` `PropertyStore`,
translate a raw `sqlite3.Error` into a domain `PropertyStoreError`, and — for the two
proposer faces — honour the `persist=False` "touch nothing" dry run and emit one
`record_property_proposed` trace per accepted candidate. This module gives that
boundary a single seam so the policy lives once instead of being copied across the
faces and kept in step by hand.

Two seams, layered:

- `open_store` — the `sqlite3.Error -> PropertyStoreError` translation, shared by all
  three faces. This is a Core-*face* raise-policy, not a `PropertyStore` invariant:
  the Stop-gate's `property_gate` opens the same store and deliberately *swallows*
  the raw error to stay best-effort, so the translation belongs here with the faces
  that want it raised, not on the store type.
- `persist_proposal` — the dry-run skip and the trace dispatch, shared by the two
  proposer faces, built on top of `open_store`. The one thing that genuinely varies
  between them — which ingest runs, and under which channel — is injected.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

from forseti.core.events import record_property_proposed
from forseti.properties import PropertyStore, PropertyStoreError

if TYPE_CHECKING:
    from pathlib import Path

    from forseti.properties import ProposalResult

# The trace channels a persisted proposal can carry (`core/events.py`). A `Literal`
# so a typo can't reach `record_property_proposed`; add a member to register a new
# proposer channel.
ProposalChannel = Literal["llm", "submitted"]


@contextmanager
def open_store(store_root: Path) -> Iterator[PropertyStore]:
    """Open `store_root`'s `PropertyStore`, translating store failure to a domain error.

    Unlike `PropertyStore.open`, which lets a raw `sqlite3.Error` escape, this seam
    is the domain-safe entry point every Core face opens the store through: a
    `sqlite3.Error` raised at open, anywhere inside the `with` body (the ingest or
    check call), or on close surfaces as `PropertyStoreError`. Everything else —
    `LLMError`, an `OSError` from `PropertyStore.open`'s `mkdir`, a
    `DuplicateProperty` — passes through untouched, exactly as the inline
    `try/except` it replaces did.
    """
    try:
        with PropertyStore.open(store_root) as store:
            yield store
    except sqlite3.Error as exc:
        raise PropertyStoreError(
            f"property store error at {store_root}: {exc}"
        ) from exc


def persist_proposal(
    ingest: Callable[[PropertyStore | None], ProposalResult],
    *,
    persist: bool,
    store_root: Path,
    channel: ProposalChannel,
) -> ProposalResult:
    """Run a proposer `ingest` under the shared dry-run/persist/trace epilogue.

    `persist=False` is a dry run: `ingest(None)` runs and nothing on disk is touched
    — the store is never opened (so its `mkdir` never fires) and no event is recorded,
    because a proposal never persisted has nothing durable to trace. Otherwise
    `ingest(store)` runs inside `open_store` (so a `sqlite3.Error` surfaces as
    `PropertyStoreError`), and after the store closes each accepted candidate is traced
    via `record_property_proposed(..., channel=channel)`.

    `ingest` is the injected variance: it closes over the face's request/client/spec
    and takes only the store (or `None` on a dry run), which both `propose_properties`
    and `submit_candidates` accept as a no-op when `None`.
    """
    if not persist:
        return ingest(None)
    with open_store(store_root) as store:
        result = ingest(store)
    record_property_proposed(store_root, result, channel=channel)
    return result
