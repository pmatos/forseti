"""Hermetic tests for Core's property-ingest persistence seam.

`open_store` and `persist_proposal` are the shared home for the persistence
boundary `propose_source`/`submit_source` (and, for the store-open concern,
`check_source`) used to each carry inline. `open_store` owns the
`sqlite3.Error -> PropertyStoreError` translation; `persist_proposal` owns the
`persist=False` dry-run "touch nothing" invariant and the
`record_property_proposed` trace dispatch. The faces' own behaviour is pinned in
`test_propose.py`/`test_submit.py`; this pins the seam directly, through its
interface, with no LLM, no esbmc, and a fake ingest.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from forseti.core.events import events_path
from forseti.core.persistence import open_store, persist_proposal
from forseti.properties import (
    CandidateSpec,
    PropertyStore,
    PropertyStoreError,
    ProposalRequest,
    ProposalResult,
    extract_signature,
    submit_candidates,
)

ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"


def _real_result() -> ProposalResult:
    """A genuine one-accepted-candidate ProposalResult, built with no LLM/store."""
    request = ProposalRequest(
        unit_id="u.c::my_abs",
        source_text=ABS_SLICE,
        signature=extract_signature(ABS_SLICE, "my_abs"),
    )
    result = submit_candidates(
        request, (CandidateSpec(expression="result >= 0"),), provider="codex", model="x"
    )
    assert result.accepted, "fixture expects the candidate to be accepted"
    return result


# --- open_store: the sqlite3.Error -> PropertyStoreError translation seam ---


def test_open_store_yields_usable_store(tmp_path: Path) -> None:
    with open_store(tmp_path / ".forseti") as store:
        assert isinstance(store, PropertyStore)
        assert not store.list_for_unit("nobody::x")  # empty; a real, usable store


def test_open_store_translates_body_sqlite_error(tmp_path: Path) -> None:
    # A sqlite3.Error raised *inside* the block, not just at open, is translated:
    # the inline try/except this replaces wrapped the whole `with` — the ingest
    # call for the faces, `record_event` + `check_properties` for check_source.
    with pytest.raises(PropertyStoreError):  # noqa: SIM117
        with open_store(tmp_path / ".forseti"):
            raise sqlite3.DatabaseError("boom")


def test_open_store_passes_non_sqlite_error_through(tmp_path: Path) -> None:
    # Only sqlite3.Error is a store failure; everything else (LLMError, OSError
    # from mkdir, a DuplicateProperty) must surface untouched.
    with pytest.raises(ValueError):  # noqa: SIM117
        with open_store(tmp_path / ".forseti"):
            raise ValueError("not a store failure")


# --- persist_proposal: dry-run invariant + trace dispatch seam ---


def test_persist_proposal_dry_run_touches_nothing(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    result = _real_result()
    seen: list[PropertyStore | None] = []

    def ingest(store: PropertyStore | None) -> ProposalResult:
        seen.append(store)
        return result

    out = persist_proposal(ingest, persist=False, store_root=root, channel="llm")

    assert out is result
    assert seen == [None]  # ingest ran against no store
    assert not root.exists()  # no open, no mkdir
    assert not events_path(root).exists()  # nothing durable to trace


def test_persist_proposal_threads_store_and_dispatches_channel(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    result = _real_result()
    seen: list[PropertyStore | None] = []

    def ingest(store: PropertyStore | None) -> ProposalResult:
        seen.append(store)
        return result

    out = persist_proposal(ingest, persist=True, store_root=root, channel="submitted")

    assert out is result
    assert isinstance(seen[0], PropertyStore)  # a real, open store was threaded in
    events = [json.loads(line) for line in events_path(root).read_text().splitlines()]
    assert len(events) == len(result.accepted)
    assert events[0]["type"] == "property.proposed"
    assert events[0]["channel"] == "submitted"  # the caller's channel is dispatched
    assert events[0]["property_id"] == result.accepted[0].property_id


def test_persist_proposal_translates_store_error(tmp_path: Path) -> None:
    # The persist path runs the ingest inside `open_store`, so a corrupt store
    # surfaces as PropertyStoreError through the seam, not a raw sqlite3.Error.
    root = tmp_path / ".forseti"
    root.mkdir()
    (root / "forseti.db").write_text("this is not a database")

    def ingest(store: PropertyStore | None) -> ProposalResult:
        assert store is not None
        store.list_for_unit("nobody::x")  # forces the corrupt DB to be read
        raise AssertionError("unreachable: the query above raises sqlite3.Error")

    with pytest.raises(PropertyStoreError):
        persist_proposal(ingest, persist=True, store_root=root, channel="llm")
