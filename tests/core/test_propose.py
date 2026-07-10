"""Hermetic tests for Core's `propose` face (`propose_source` + the CLI).

No LLM, no network, no esbmc: a `FakeLLMClient` returns a canned candidate JSON
envelope, and persistence goes to a real `PropertyStore` under `tmp_path`. The
CLI tests monkeypatch `forseti.core.propose.ClaudeCliClient` so the same fake
backs the argv path. Mirrors `tests/properties/test_proposer.py`'s fake-client
style. The live backend is covered by `tests/properties/test_proposer_live.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forseti.core import propose_source
from forseti.core.cli import main
from forseti.properties import LLMError, PropertyStore, PropertyStoreError

if TYPE_CHECKING:
    from forseti.properties import LLMClient

ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"

# Two acceptable candidates plus one that names an unknown identifier — the last
# is rejected only when the signature is parsed (the identifier check).
CANNED_REPLY = json.dumps(
    {
        "candidates": [
            {"expression": "result >= 0", "domain": ["x > INT64_MIN"]},
            {"expression": "result <= x || result <= -x"},
            {"expression": "bogus_ident >= 0"},
        ]
    }
)


class FakeLLMClient:
    """An `LLMClient` returning a fixed reply; accepts (and ignores) real kwargs.

    The extra ``**_kw`` lets it stand in for `ClaudeCliClient(model=..., ...)`
    when monkeypatched into the CLI path.
    """

    def __init__(
        self,
        reply: str = CANNED_REPLY,
        *,
        provider: str = "fake",
        model: str = "fake-1",
        **_kw: object,
    ) -> None:
        self._reply = reply
        self.provider = provider
        self.model = model
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._reply


class RaisingLLMClient:
    """An `LLMClient` whose call fails — to prove `LLMError` reaches the caller."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, *_a: object, **_kw: object) -> None: ...

    def complete(self, prompt: str) -> str:
        raise LLMError("backend unreachable")


def _write_unit(tmp_path: Path) -> Path:
    source = tmp_path / "abs_unit.c"
    source.write_text(ABS_SLICE)
    return source


def test_propose_source_accepts_and_rejects(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    result = propose_source(
        source,
        function="my_abs",
        persist=False,
        client=FakeLLMClient(),
    )
    exprs = [p.expression for p in result.accepted]
    assert "result >= 0" in exprs
    assert "result <= x || result <= -x" in exprs
    # The unknown-identifier candidate is rejected because the signature parsed.
    assert any("bogus_ident" in r.spec.expression for r in result.rejected)
    assert result.unit_id == f"{source}::my_abs"
    assert result.provider == "fake"


def test_propose_source_persists_candidates(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    result = propose_source(
        source, function="my_abs", store_root=root, client=FakeLLMClient()
    )
    unit_id = f"{source}::my_abs"
    # Re-open the store independently and confirm the survivors landed as CANDIDATE.
    store = PropertyStore.open(root)
    try:
        stored = store.list_for_unit(unit_id)
    finally:
        store.close()
    assert {p.property_id for p in stored} == {p.property_id for p in result.accepted}
    assert all(p.status.value == "candidate" for p in stored)


def test_propose_source_dry_run_does_not_persist(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    propose_source(
        source,
        function="my_abs",
        persist=False,
        store_root=root,
        client=FakeLLMClient(),
    )
    assert not root.exists()  # dry run never opened/created the store


def test_propose_source_to_dict_shape(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    payload = propose_source(
        source, function="my_abs", persist=False, client=FakeLLMClient()
    ).to_dict()
    assert set(payload) >= {
        "unit_id",
        "prompt_id",
        "prompt_version",
        "provider",
        "model",
        "accepted",
        "rejected",
    }
    assert json.loads(json.dumps(payload))["provider"] == "fake"  # JSON round-trips


def test_propose_source_propagates_llm_error(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(LLMError):
        propose_source(
            source, function="my_abs", persist=False, client=RaisingLLMClient()
        )


def _corrupt_store_root(tmp_path: Path) -> Path:
    """A .forseti dir whose forseti.db is not a valid SQLite database."""
    root = tmp_path / ".forseti"
    root.mkdir()
    (root / "forseti.db").write_text("this is not a database")
    return root


def test_propose_source_translates_store_error(tmp_path: Path) -> None:
    # A corrupt store must surface as PropertyStoreError, not a raw sqlite3.Error
    # traceback (sqlite3.Error is not an OSError, so it would otherwise escape).
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    with pytest.raises(PropertyStoreError):
        propose_source(
            source, function="my_abs", store_root=root, client=FakeLLMClient()
        )


def test_cli_propose_store_error_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", FakeLLMClient)
    code = main(
        ["propose", str(source), "--function", "my_abs", "--store-root", str(root)]
    )
    assert code == 1
    assert "forseti propose:" in capsys.readouterr().err


def test_cli_propose_json_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", FakeLLMClient)
    code = main(
        ["propose", str(source), "--function", "my_abs", "--no-store", "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_id"].endswith("abs_unit.c::my_abs")
    assert any(a["expression"] == "result >= 0" for a in payload["accepted"])


def test_cli_propose_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", FakeLLMClient)
    code = main(["propose", str(source), "--function", "my_abs", "--no-store"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Proposed 2 properties" in out
    assert "result >= 0" in out


def test_cli_propose_llm_error_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", RaisingLLMClient)
    code = main(["propose", str(source), "--function", "my_abs", "--no-store"])
    assert code == 1
    assert "forseti propose:" in capsys.readouterr().err


if TYPE_CHECKING:
    # mypy-only structural guards: the fakes must satisfy the LLMClient protocol.
    def _fake_is_client(c: FakeLLMClient) -> LLMClient:
        return c

    def _raising_is_client(c: RaisingLLMClient) -> LLMClient:
        return c
