"""Source preparation shared by LLM-proposed and host-submitted candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from forseti.core import propose_source, submit_source
from forseti.core._proposal_request import prepare_proposal_request
from forseti.properties import PromptTemplate, ProposalResult, UnitSignature
from forseti.properties.prompts import DEFAULT_PROMPT

_SCALAR_SOURCE = "\nint identity(int x) { return x; }\n"
_CALLBACK_SOURCE = "int apply(int (*fn)(int)) { return fn(1); }\n"
_EXPRESSION = "unknown_input >= 0"


def test_preparation_preserves_text_path_spelling_and_default_prompt(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    source = tmp_path / "nested" / ".." / "unit.c"
    source.write_text(_SCALAR_SOURCE)

    request = prepare_proposal_request(source, function="identity")

    assert request.unit_id == f"{source}::identity"
    assert request.source_text == _SCALAR_SOURCE
    assert request.prompt is DEFAULT_PROMPT
    assert request.signature is not None
    assert request.signature.symbol == "identity"
    assert request.signature.return_ctype == "int"
    assert request.signature.param_names == {"x"}


def test_preparation_preserves_explicit_prompt(tmp_path: Path) -> None:
    source = tmp_path / "unit.c"
    source.write_text(_SCALAR_SOURCE)
    prompt = PromptTemplate(prompt_id="host-contract", version="7", template="")

    request = prepare_proposal_request(source, function="identity", prompt=prompt)

    assert request.prompt is prompt


@pytest.mark.parametrize(
    ("source_text", "function"),
    [(_CALLBACK_SOURCE, "apply"), (_SCALAR_SOURCE, "absent")],
)
def test_preparation_degrades_unsupported_signatures(
    tmp_path: Path, source_text: str, function: str
) -> None:
    source = tmp_path / "unit.c"
    source.write_text(source_text)

    request = prepare_proposal_request(source, function=function)

    assert request.source_text == source_text
    assert request.unit_id == f"{source}::{function}"
    assert request.signature is None


def test_preparation_propagates_source_read_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        prepare_proposal_request(tmp_path / "missing.c", function="identity")


def test_preparation_propagates_unexpected_parser_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "unit.c"
    source.write_text(_SCALAR_SOURCE)
    failure = RuntimeError("unexpected parser failure")

    def broken_parser(source_text: str, function: str) -> UnitSignature:
        raise failure

    monkeypatch.setattr(
        "forseti.core._proposal_request.extract_signature", broken_parser
    )

    with pytest.raises(RuntimeError) as caught:
        prepare_proposal_request(source, function="identity")

    assert caught.value is failure


def test_preparation_observes_edits_between_calls(tmp_path: Path) -> None:
    source = tmp_path / "unit.c"
    source.write_text(_SCALAR_SOURCE)
    first = prepare_proposal_request(source, function="identity")
    edited = "int identity(int y) { return y; }\n"
    source.write_text(edited)

    second = prepare_proposal_request(source, function="identity")

    assert first.source_text == _SCALAR_SOURCE
    assert first.signature is not None
    assert first.signature.param_names == {"x"}
    assert second.source_text == edited
    assert second.signature is not None
    assert second.signature.param_names == {"y"}


class _CandidateClient:
    provider = "test-provider"
    model = "test-model"

    def complete(self, prompt: str) -> str:
        return '{"candidates": [{"expression": "unknown_input >= 0"}]}'


def _ingest(
    mode: Literal["propose", "submit"], source: Path, function: str
) -> ProposalResult:
    if mode == "propose":
        return propose_source(
            source, function=function, persist=False, client=_CandidateClient()
        )
    return submit_source(
        source,
        function=function,
        expression=_EXPRESSION,
        provider="test-provider",
        model="test-model",
        prompt_id="host-contract",
        prompt_version="7",
        persist=False,
    )


@pytest.mark.parametrize("mode", ["propose", "submit"])
@pytest.mark.parametrize(
    ("source_text", "function", "signature_supported"),
    [(_SCALAR_SOURCE, "identity", True), (_CALLBACK_SOURCE, "apply", False)],
)
def test_ingestion_preserves_signature_fallback_and_prompt_provenance(
    tmp_path: Path,
    mode: Literal["propose", "submit"],
    source_text: str,
    function: str,
    signature_supported: bool,
) -> None:
    source = tmp_path / "unit.c"
    source.write_text(source_text)

    result = _ingest(mode, source, function)

    assert result.unit_id == f"{source}::{function}"
    assert (result.provider, result.model) == ("test-provider", "test-model")
    assert (result.prompt_id, result.prompt_version) == (
        ("semantic", "1") if mode == "propose" else ("host-contract", "7")
    )
    if signature_supported:
        assert not result.accepted
        assert len(result.rejected) == 1
        assert "unknown identifier 'unknown_input'" in result.rejected[0].reason
    else:
        assert [prop.expression for prop in result.accepted] == [_EXPRESSION]
        assert not result.rejected
        provenance = result.accepted[0].provenance
        assert (provenance.prompt_id, provenance.prompt_version) == (
            result.prompt_id,
            result.prompt_version,
        )


@pytest.mark.parametrize("mode", ["propose", "submit"])
def test_ingestion_propagates_source_read_errors(
    tmp_path: Path, mode: Literal["propose", "submit"]
) -> None:
    with pytest.raises(FileNotFoundError):
        _ingest(mode, tmp_path / "missing.c", "identity")
