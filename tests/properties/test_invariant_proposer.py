"""The representation-invariant proposer: prompt, parser and routing (#420).

Hermetic: a fake `LLMClient` returns a canned envelope, the store is an in-memory
`PropertyStore`. Nothing runs ESBMC.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forseti.properties import (
    DEFAULT_INVARIANT_PROMPT,
    INVARIANT_ASSUMPTION_NOTE,
    PROMPTS,
    REP_INVARIANT_V1,
    InvariantRequest,
    LLMError,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    ProposalContext,
    ProposalParseError,
    parse_candidate_list,
    parse_candidates,
    propose_invariants,
    render_prompt,
    validate_candidate,
)

if TYPE_CHECKING:
    from forseti.properties import LLMClient

SOURCE = """\
#define CAP 4
typedef struct { unsigned head, tail, count; int buf[CAP]; } ring_t;
int rb_push(ring_t *r, int v) { return 1; }
"""
TYPE_KEY = "ring.c::ring_t"
INV = "self->count <= CAP && self->tail < CAP"


class FakeLLMClient:
    provider = "fake"
    model = "fake-1"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._reply


class RaisingLLMClient:
    provider = "fake"
    model = "fake-1"

    def complete(self, prompt: str) -> str:
        raise LLMError("backend unreachable")


def _reply(*candidates: dict[str, object]) -> str:
    return json.dumps({"candidates": list(candidates)})


def _request(
    *, type_key: str = TYPE_KEY, type_name: str = "ring_t", **overrides: Any
) -> InvariantRequest:
    return InvariantRequest(
        type_key=type_key,
        type_name=type_name,
        function="rb_push",
        source_text=overrides.pop("source_text", SOURCE),
        **overrides,
    )


def _store() -> PropertyStore:
    return PropertyStore(sqlite3.connect(":memory:"))


def test_the_invariant_prompt_is_versioned_registered_and_not_the_default() -> None:
    assert DEFAULT_INVARIANT_PROMPT is REP_INVARIANT_V1
    assert REP_INVARIANT_V1.ref == "rep-invariant/v1"
    assert PROMPTS[REP_INVARIANT_V1.ref] is REP_INVARIANT_V1


def test_the_prompt_asks_for_a_predicate_over_self_for_the_type() -> None:
    rendered = render_prompt(
        REP_INVARIANT_V1,
        unit_id=TYPE_KEY,
        symbol="rb_push",
        source_text=SOURCE,
        type_name="ring_t",
        max_candidates=3,
    )
    assert "<<" not in rendered
    assert "`const ring_t *`" in rendered
    assert "Type: ring_t" in rendered
    assert TYPE_KEY in rendered
    assert "rb_push" in rendered
    assert "typedef struct" in rendered
    assert "up to 3" in rendered
    assert "ASSUMED" in rendered
    assert "proof" not in rendered.lower()


def test_a_run_sends_the_prompt_and_stores_each_survivor_as_a_type_candidate() -> None:
    client = FakeLLMClient(
        _reply({"expression": INV, "rationale": "indices stay below CAP"})
    )
    store = _store()

    result = propose_invariants(_request(), client=client, store=store)

    (prop,) = result.accepted
    assert not result.rejected
    assert "const ring_t *" in client.prompts[0]
    assert result.unit_id == TYPE_KEY
    assert (result.prompt_id, result.prompt_version) == ("rep-invariant", "1")
    assert prop.kind is PropertyKind.REP_INVARIANT
    assert prop.status is PropertyStatus.CANDIDATE
    assert prop.unit_id == TYPE_KEY
    assert prop.expression == INV
    assert prop.description == "indices stay below CAP"
    assert prop.provenance.provider == "fake"
    assert store.list_for_type(TYPE_KEY) == (prop,)
    assert store.list_for_unit(TYPE_KEY) == ()


def test_a_dry_run_without_a_store_touches_nothing() -> None:
    result = propose_invariants(
        _request(), client=FakeLLMClient(_reply({"expression": INV}))
    )
    assert len(result.accepted) == 1


def test_rerunning_is_idempotent() -> None:
    store = _store()
    client = FakeLLMClient(_reply({"expression": INV}))
    propose_invariants(_request(), client=client, store=store)
    propose_invariants(_request(), client=client, store=store)
    assert len(store.list_for_type(TYPE_KEY)) == 1


@pytest.mark.parametrize("status", [PropertyStatus.ACCEPTED, PropertyStatus.REJECTED])
def test_an_invariant_already_settled_is_not_proposed_again(
    status: PropertyStatus,
) -> None:
    store = _store()
    client = FakeLLMClient(_reply({"expression": INV}))
    (first,) = propose_invariants(_request(), client=client, store=store).accepted
    store.update_type_invariant_status(first.property_id, status)

    again = propose_invariants(_request(), client=client, store=store)

    assert again.accepted == ()
    assert again.rejected[0].reason == f"already stored as {status.value}"
    assert store.list_for_type(TYPE_KEY)[0].status is status


def test_a_recipe_context_replaces_the_source_and_its_ref_is_stored() -> None:
    client = FakeLLMClient(_reply({"expression": INV}))
    store = _store()
    context = ProposalContext(recipe="sighted/v1", text="CONTEXT-ONLY-TEXT\n")

    result = propose_invariants(_request(context=context), client=client, store=store)

    assert "CONTEXT-ONLY-TEXT" in client.prompts[0]
    assert "typedef struct" not in client.prompts[0]
    (stored,) = store.list_for_type(TYPE_KEY)
    assert stored.provenance.recipe == "sighted/v1"
    assert result.accepted[0].provenance.recipe == "sighted/v1"


@pytest.mark.parametrize(
    ("candidate", "fragment"),
    [
        ({"expression": "1 == 1"}, "vacuous"),
        ({"expression": "self->count++ < CAP"}, "unsafe"),
        ({"expression": "check(self)"}, "unsafe"),
        ({"expression": "r->count <= CAP"}, "vacuous"),
        ({"expression": INV, "kind": "semantic"}, "non-invariant kind"),
        ({"expression": INV, "type": "other_t"}, "is not 'ring_t'"),
        ({"expression": INV, "domain": ["x > 0"]}, "do not apply"),
        ({"expression": INV, "referenced_params": ["r"]}, "do not apply"),
    ],
)
def test_a_malformed_or_vacuous_candidate_is_rejected_with_a_reason_and_not_stored(
    candidate: dict[str, object], fragment: str
) -> None:
    store = _store()

    result = propose_invariants(
        _request(), client=FakeLLMClient(_reply(candidate)), store=store
    )

    assert result.accepted == ()
    (rejected,) = result.rejected
    assert fragment in rejected.reason
    assert store.list_for_type(TYPE_KEY) == ()


def test_a_type_that_does_not_occur_in_the_source_rejects_every_candidate() -> None:
    store = _store()
    result = propose_invariants(
        _request(type_key="ring.c::nosuch_t", type_name="nosuch_t"),
        client=FakeLLMClient(_reply({"expression": INV})),
        store=store,
    )
    assert result.accepted == ()
    assert "does not occur" in result.rejected[0].reason
    assert store.list_for_type("ring.c::nosuch_t") == ()


@pytest.mark.parametrize("kind", ["rep-invariant", "rep_invariant"])
def test_candidate_kind_and_matching_type_are_accepted_whatever_the_spacing(
    kind: str,
) -> None:
    request = _request(
        type_key="n.c::struct node",
        type_name="struct node",
        source_text="struct node { int v; };\nint f(struct node *n) { return 0; }\n",
    )
    reply = _reply({"expression": "self->v >= 0", "kind": kind, "type": "struct  node"})
    result = propose_invariants(request, client=FakeLLMClient(reply))
    assert len(result.accepted) == 1


def test_duplicates_in_a_batch_and_the_cap_are_reported() -> None:
    reply = _reply(
        {"expression": INV},
        {"expression": f"  {INV} "},
        {"expression": "self->head < CAP"},
        {"expression": "self->tail < CAP"},
    )
    result = propose_invariants(
        _request(), client=FakeLLMClient(reply), max_candidates=2
    )
    assert [p.expression for p in result.accepted] == [INV, "self->head < CAP"]
    assert [r.reason for r in result.rejected] == [
        "duplicate in batch",
        "over max_candidates",
    ]


def test_llm_and_parse_failures_propagate() -> None:
    with pytest.raises(LLMError):
        propose_invariants(_request(), client=RaisingLLMClient())
    with pytest.raises(ProposalParseError):
        propose_invariants(_request(), client=FakeLLMClient("not json"))


def test_the_payload_labels_an_accepted_invariant_as_assumed() -> None:
    payload = propose_invariants(
        _request(), client=FakeLLMClient(_reply({"expression": INV}))
    ).to_dict()
    assert payload["assumption"] == INVARIANT_ASSUMPTION_NOTE
    assert "proof" not in json.dumps(payload["assumption"]).lower()


def test_a_run_that_accepts_nothing_carries_no_assumption_note() -> None:
    payload = propose_invariants(
        _request(), client=FakeLLMClient(_reply({"expression": "1 == 1"}))
    ).to_dict()
    assert "assumption" not in payload


def test_the_parser_reads_a_kind_and_a_type_and_keeps_them_apart_from_semantic() -> (
    None
):
    (spec,) = parse_candidate_list(
        [{"expression": INV, "kind": "rep-invariant", "type": " ring_t "}]
    )
    assert (spec.kind, spec.type_name) == ("rep-invariant", "ring_t")
    (plain,) = parse_candidates(_reply({"expression": "result >= 0"}))
    assert plain.type_name is None
    assert validate_candidate(spec, None) is not None  # not a semantic candidate


@pytest.mark.parametrize("bad", [3, "", "  ", ["ring_t"]])
def test_a_malformed_type_is_ignored_not_a_parse_error(bad: object) -> None:
    (spec,) = parse_candidate_list([{"expression": INV, "type": bad}])
    assert spec.type_name is None


def test_a_type_invariant_row_keeps_its_recipe_across_reopen(tmp_path: Path) -> None:
    context = ProposalContext(recipe="blind/v1", text=SOURCE)
    root = tmp_path / ".forseti"
    with PropertyStore.open(root) as store:
        propose_invariants(
            _request(context=context),
            client=FakeLLMClient(_reply({"expression": INV})),
            store=store,
        )
    with PropertyStore.open(root) as again:
        (stored,) = again.list_for_type(TYPE_KEY)
    assert stored.provenance.recipe == "blind/v1"


def test_a_pre_recipe_type_table_gains_the_column_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".forseti"
    with PropertyStore.open(root) as store:
        propose_invariants(
            _request(),
            client=FakeLLMClient(_reply({"expression": INV})),
            store=store,
        )
    conn = sqlite3.connect(root / "forseti.db")
    conn.execute("ALTER TABLE type_invariants DROP COLUMN recipe")
    conn.commit()
    conn.close()

    with PropertyStore.open(root) as migrated:
        (old,) = migrated.list_for_type(TYPE_KEY)
        assert old.provenance.recipe == ""
        propose_invariants(
            _request(context=ProposalContext("sighted/v1", SOURCE)),
            client=FakeLLMClient(_reply({"expression": "self->head < CAP"})),
            store=migrated,
        )
        recipes = {p.provenance.recipe for p in migrated.list_for_type(TYPE_KEY)}
    assert recipes == {"", "sighted/v1"}


if TYPE_CHECKING:

    def _fake_is_client(c: FakeLLMClient) -> LLMClient:
        return c
