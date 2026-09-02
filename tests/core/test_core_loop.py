"""Hermetic tests for Core's composed semantic loop (`run_semantic_loop`, #213).

No LLM, no network, no esbmc: a `FakeLLMClient` (mirrors `test_propose.py`'s)
stands in for the proposer and a scripted `FakeVerify` (mirrors
`test_core_check.py`'s) stands in for ESBMC, while a real `PropertyStore`
(under `tmp_path`) and the real `SemanticHarnessWriter` exercise the actual
persistence/render wiring `run_semantic_loop` composes from `propose_source`/
`submit_source`/`check_source`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forseti.core.loop import run_semantic_loop
from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.properties import CandidateSpec

ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"

CANNED_REPLY = json.dumps(
    {
        "candidates": [
            {"expression": "result >= 0", "domain": ["x > INT64_MIN"]},
        ]
    }
)


class FakeLLMClient:
    """An `LLMClient` returning a fixed reply -- never invokes `claude -p`."""

    def __init__(self, reply: str = CANNED_REPLY) -> None:
        self._reply = reply
        self.provider = "fake"
        self.model = "fake-1"

    def complete(self, prompt: str) -> str:
        return self._reply


def _meta(unwind: int = 4) -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "harness.c", "--unwind", str(unwind)),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        return self._results.pop(0)


def _write_unit(tmp_path: Path) -> Path:
    source = tmp_path / "abs_unit.c"
    source.write_text(ABS_SLICE)
    return source


# --- "submit" mode ------------------------------------------------------------


def test_submit_mode_ingests_and_checks(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(
            CandidateSpec(expression="result >= 0", domain=("x > INT64_MIN",)),
        ),
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        verify_port=FakeVerify([Verified(_meta())]),
    )

    assert result.mode == "submit"
    assert result.unit_id == f"{source}::my_abs"
    assert len(result.ingestion) == 1
    assert [p.expression for p in result.ingestion[0].accepted] == ["result >= 0"]
    assert result.outcome == "held"
    assert result.check.counts()["held"] == 1


def test_submit_mode_batches_candidates_without_swallowing_a_rejection(
    tmp_path: Path,
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(
            CandidateSpec(expression="result >= 0", domain=("x > INT64_MIN",)),
            CandidateSpec(expression="bogus_ident >= 0"),
        ),
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        verify_port=FakeVerify([Verified(_meta())]),
    )

    # Two candidates in, two independent ingestion results out -- the rejected
    # one stays visible instead of vanishing into a merged batch result.
    assert len(result.ingestion) == 2
    assert result.ingestion[0].accepted and not result.ingestion[0].rejected
    assert result.ingestion[1].rejected and not result.ingestion[1].accepted
    assert "bogus_ident" in result.ingestion[1].rejected[0].reason
    # Only the accepted candidate reached the store and was checked.
    assert result.check.counts()["held"] == 1


def test_submit_mode_requires_at_least_one_candidate(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(ValueError, match="at least one candidate"):
        run_semantic_loop(
            source,
            function="my_abs",
            mode="submit",
            candidates=(),
            provider="codex",
            model="gpt-5.1",
            store_root=tmp_path / ".forseti",
        )


@pytest.mark.parametrize(("provider", "model"), [(None, "gpt-5.1"), ("codex", None)])
def test_submit_mode_requires_provider_and_model(
    tmp_path: Path, provider: str | None, model: str | None
) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(ValueError, match="provider and model"):
        run_semantic_loop(
            source,
            function="my_abs",
            mode="submit",
            candidates=(CandidateSpec(expression="result >= 0"),),
            provider=provider,
            model=model,
            store_root=tmp_path / ".forseti",
        )


# --- "propose" mode ------------------------------------------------------------


def test_propose_mode_ingests_via_the_injected_client(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="propose",
        store_root=root,
        client=FakeLLMClient(),
        verify_port=FakeVerify([Verified(_meta())]),
    )

    assert len(result.ingestion) == 1
    assert [p.expression for p in result.ingestion[0].accepted] == ["result >= 0"]
    assert result.outcome == "held"


def test_propose_mode_rejects_candidates(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(ValueError, match="does not take candidates"):
        run_semantic_loop(
            source,
            function="my_abs",
            mode="propose",
            candidates=(CandidateSpec(expression="result >= 0"),),
            client=FakeLLMClient(),
            store_root=tmp_path / ".forseti",
        )


# --- "check_only" mode ---------------------------------------------------------


def test_check_only_mode_skips_ingestion_and_checks_the_stored_candidate(
    tmp_path: Path,
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    # Pre-seed the store the way a prior "submit"/"propose" call already would.
    run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(
            CandidateSpec(expression="result >= 0", domain=("x > INT64_MIN",)),
        ),
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        verify_port=FakeVerify([Verified(_meta())]),
    )

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="check_only",
        store_root=root,
        verify_port=FakeVerify([Verified(_meta())]),
    )

    assert result.ingestion == ()
    assert result.outcome == "held"


def test_check_only_mode_rejects_candidates(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(ValueError, match="does not take candidates"):
        run_semantic_loop(
            source,
            function="my_abs",
            mode="check_only",
            candidates=(CandidateSpec(expression="result >= 0"),),
            store_root=tmp_path / ".forseti",
        )


# --- outcome policy (Core-owned, #213) -----------------------------------------


def test_outcome_violated(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(CandidateSpec(expression="result < 0"),),  # always false
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        verify_port=FakeVerify([Violated(_meta(), "[Counterexample]\n", None)]),
    )

    assert result.outcome == "violated"


def test_outcome_unknown(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(CandidateSpec(expression="result >= 0"),),
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        unwind_ladder=(),  # settle at the base bound, no escalation
        verify_port=FakeVerify([Unknown(_meta(), UnknownReason.TIMEOUT)]),
    )

    assert result.outcome == "unknown"


def test_outcome_empty_when_nothing_checkable(tmp_path: Path) -> None:
    # CLAUDE.md "never silently pass": check_only over an empty store must not
    # read as "held".
    source = _write_unit(tmp_path)
    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="check_only",
        store_root=tmp_path / ".forseti",
        verify_port=FakeVerify([]),
    )
    assert result.outcome == "empty"


def test_to_dict_shape(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"

    result = run_semantic_loop(
        source,
        function="my_abs",
        mode="submit",
        candidates=(
            CandidateSpec(expression="result >= 0", domain=("x > INT64_MIN",)),
        ),
        provider="codex",
        model="gpt-5.1",
        store_root=root,
        verify_port=FakeVerify([Verified(_meta())]),
    )
    payload = result.to_dict()
    assert payload["unit_id"] == f"{source}::my_abs"
    assert payload["mode"] == "submit"
    assert payload["outcome"] == "held"
    assert len(payload["ingestion"]) == 1
    assert payload["check"]["counts"]["held"] == 1
