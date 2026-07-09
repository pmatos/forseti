"""Tests for the property proposer -- pure, with the LLM and store faked.

No subprocess, no network, no esbmc: a `FakeLLMClient` returns a canned JSON
string, a dict-backed `FakeStore` stands in for #62's SQLite store, and the real
`render_semantic_harness` (or a stub) serves the renderability gate. Mirrors
`tests/orchestrator/test_fix.py` (fakes + TYPE_CHECKING protocol guards).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest

from forseti.properties import (
    CandidateSpec,
    HarnessError,
    LLMError,
    Property,
    PropertyKind,
    PropertyStatus,
    ProposalParseError,
    ProposalRequest,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    parse_candidates,
    propose_properties,
    render_semantic_harness,
    validate_candidate,
)

if TYPE_CHECKING:
    from forseti.properties import CandidateStore, HarnessRenderer, LLMClient

ABS_SOURCE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"
ABS_UNIT = "examples/abs.c::my_abs"


def abs_sig() -> UnitSignature:
    return UnitSignature(
        symbol="my_abs",
        return_ctype="int64_t",
        params=(ScalarParam(ctype="int64_t", name="x"),),
    )


class FakeLLMClient:
    """An `LLMClient` that returns a fixed reply and records the prompt it saw."""

    def __init__(
        self, reply: str, *, provider: str = "fake", model: str = "fake-1"
    ) -> None:
        self._reply = reply
        self.provider = provider
        self.model = model
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._reply


class RaisingLLMClient:
    """An `LLMClient` whose call fails -- to prove `LLMError` propagates."""

    provider = "fake"
    model = "fake-1"

    def complete(self, prompt: str) -> str:
        raise LLMError("no backend")


class FakeStore:
    """A dict-backed `CandidateStore` (get/add) standing in for #62's store."""

    def __init__(self) -> None:
        self.items: dict[str, Property] = {}

    def get(self, property_id: str) -> Property | None:
        return self.items.get(property_id)

    def add(self, prop: Property) -> None:
        self.items[prop.property_id] = prop


def reply(*candidates: Mapping[str, object]) -> str:
    return json.dumps({"candidates": list(candidates)})


TWO_GOOD = reply(
    {
        "expression": "result >= 0",
        "domain": ["x > INT64_MIN"],
        "referenced_params": ["x"],
        "rationale": "abs is non-negative away from the minimum",
    },
    {
        "expression": "result >= x",
        "domain": ["x <= 0"],
        "referenced_params": ["x"],
        "rationale": "abs dominates its argument",
    },
)


def test_happy_path_two_candidates() -> None:
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(TWO_GOOD),
        renderer=render_semantic_harness,
    )
    assert len(result.accepted) == 2
    assert not result.rejected
    for prop in result.accepted:
        assert prop.kind is PropertyKind.SEMANTIC
        assert prop.status is PropertyStatus.CANDIDATE
        assert prop.unit_id == ABS_UNIT
        assert prop.provenance.prompt_id == "semantic"
        assert prop.provenance.prompt_version == "1"
    assert result.accepted[0].domain == ("x > INT64_MIN",)
    assert result.provider == "fake"
    assert result.model == "fake-1"


def test_persistence_is_idempotent() -> None:
    store = FakeStore()
    request = ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig())
    first = propose_properties(request, client=FakeLLMClient(TWO_GOOD), store=store)
    assert len(store.items) == 2
    assert {p.property_id for p in first.accepted} == set(store.items)

    # A second identical run adds nothing: the content id already exists.
    propose_properties(request, client=FakeLLMClient(TWO_GOOD), store=store)
    assert len(store.items) == 2


def test_llm_error_propagates() -> None:
    with pytest.raises(LLMError):
        propose_properties(
            ProposalRequest(ABS_UNIT, ABS_SOURCE),
            client=RaisingLLMClient(),
        )


def test_parse_candidates_strips_markdown_fence() -> None:
    fenced = "```json\n" + reply({"expression": "result >= 0"}) + "\n```"
    specs = parse_candidates(fenced)
    assert len(specs) == 1
    assert specs[0].expression == "result >= 0"


def test_parse_candidates_accepts_bare_list() -> None:
    specs = parse_candidates(json.dumps([{"expression": "result >= 0"}]))
    assert specs[0].expression == "result >= 0"


def test_parse_candidates_rejects_non_json() -> None:
    with pytest.raises(ProposalParseError, match="not JSON"):
        parse_candidates("definitely not json")


def test_parse_candidates_requires_expression() -> None:
    with pytest.raises(ProposalParseError, match="expression"):
        parse_candidates(reply({"domain": ["x > 0"]}))


def test_parse_candidates_rejects_bad_domain_type() -> None:
    with pytest.raises(ProposalParseError, match="domain"):
        parse_candidates(reply({"expression": "result >= 0", "domain": "x > 0"}))


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        (CandidateSpec(expression="result >= 0", kind="reachability"), "non-semantic"),
        (CandidateSpec(expression="x = 0"), "assignment"),
        (CandidateSpec(expression="result >= abs(x)"), "function call"),
        (CandidateSpec(expression="result >= 0; x"), "';'"),
        (CandidateSpec(expression="1 == 1"), "vacuous expression"),
        (
            CandidateSpec(expression="result >= 0", domain=("result > 0",)),
            "references the result",
        ),
        (
            CandidateSpec(expression="result >= 0", domain=("1 == 1",)),
            "vacuous domain",
        ),
    ],
)
def test_validate_rejects_without_signature(spec: CandidateSpec, fragment: str) -> None:
    # These checks do not need a signature; they apply even when it is None.
    reason = validate_candidate(spec, None)
    assert reason is not None and fragment in reason


def test_validate_identifier_checks_need_signature() -> None:
    spec = CandidateSpec(expression="result >= y", referenced_params=("y",))
    # Without a signature, unknown-identifier / non-param checks are skipped.
    assert validate_candidate(spec, None) is None
    # With one, both fire.
    reason = validate_candidate(spec, abs_sig())
    assert reason is not None and "unknown identifier 'y'" in reason


def test_validate_referenced_params_subset() -> None:
    spec = CandidateSpec(expression="result >= x", referenced_params=("x", "z"))
    reason = validate_candidate(spec, abs_sig())
    assert reason is not None and "non-parameter 'z'" in reason


def test_rejections_land_in_result_not_raised() -> None:
    bad = reply(
        {"expression": "x = 0"},  # unsafe assignment
        {"expression": "result >= 0", "domain": ["x > INT64_MIN"]},  # good
    )
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(bad),
        renderer=render_semantic_harness,
    )
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1
    assert "assignment" in result.rejected[0].reason


def test_v6_unrenderable_candidate_rejected() -> None:
    def raising_renderer(
        *, unit_source: str, signature: UnitSignature, spec: SemanticSpec
    ) -> str:
        raise HarnessError("cannot render")

    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(reply({"expression": "result >= 0"})),
        renderer=raising_renderer,
    )
    assert not result.accepted
    assert "unrenderable" in result.rejected[0].reason


def test_renderer_none_skips_v6() -> None:
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(reply({"expression": "result >= 0"})),
        renderer=None,
    )
    assert len(result.accepted) == 1


def test_duplicate_in_batch_kept_once() -> None:
    dup = reply(
        {"expression": "result >= 0", "domain": ["x > INT64_MIN"]},
        {"expression": "result >= 0", "domain": ["x > INT64_MIN"]},
    )
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(dup),
    )
    assert len(result.accepted) == 1
    assert any("duplicate in batch" in r.reason for r in result.rejected)


def test_over_max_candidates_capped() -> None:
    many = reply(*({"expression": f"result >= {i}"} for i in range(5)))
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(many),
        max_candidates=2,
    )
    assert len(result.accepted) == 2
    assert any("over max_candidates" in r.reason for r in result.rejected)


def test_to_dict_is_json_serialisable() -> None:
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=abs_sig()),
        client=FakeLLMClient(TWO_GOOD),
    )
    payload = result.to_dict()
    # Round-trips through json without error and preserves the counts.
    round_tripped = json.loads(json.dumps(payload))
    assert len(round_tripped["accepted"]) == 2
    assert round_tripped["prompt_id"] == "semantic"
    assert round_tripped["provider"] == "fake"


if TYPE_CHECKING:
    # mypy-only structural guards (mirrors fix.py / test_loop.py): the fakes must
    # satisfy the seams they stand in for.
    def _fake_client_is_llmclient(c: FakeLLMClient) -> LLMClient:
        return c

    def _fake_store_is_candidatestore(s: FakeStore) -> CandidateStore:
        return s

    def _real_renderer_is_harnessrenderer() -> HarnessRenderer:
        return render_semantic_harness
