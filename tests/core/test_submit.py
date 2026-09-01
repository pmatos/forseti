"""Hermetic tests for Core's `submit` face (`submit_source` + the CLI, #213).

`submit_source` is `propose_source`'s LLM-free sibling: no `LLMClient`, no
`claude` binary, no subprocess at all -- `test_submit_source_never_invokes_a_subprocess`
pins that directly. Persistence goes to a real `PropertyStore` under `tmp_path`,
mirroring `tests/core/test_propose.py`'s style.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forseti.core import submit_source
from forseti.core.cli import main
from forseti.core.events import events_path
from forseti.properties import BlankProvenanceError, PropertyStore, PropertyStoreError

ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"


def _write_unit(tmp_path: Path) -> Path:
    source = tmp_path / "abs_unit.c"
    source.write_text(ABS_SLICE)
    return source


def test_submit_source_accepts_a_valid_candidate(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    result = submit_source(
        source,
        function="my_abs",
        expression="result >= 0",
        domain=("x > INT64_MIN",),
        provider="codex",
        model="gpt-5.1",
        persist=False,
    )
    assert [p.expression for p in result.accepted] == ["result >= 0"]
    assert not result.rejected
    assert result.provider == "codex"
    assert result.model == "gpt-5.1"
    prop = result.accepted[0]
    assert prop.provenance.provider == "codex"
    assert prop.provenance.model == "gpt-5.1"
    assert prop.provenance.prompt_id == "host-submitted"


def test_submit_source_rejects_the_same_way_propose_does(tmp_path: Path) -> None:
    # An unknown identifier fails the same static `validate_candidate` gate a
    # proposer-sourced candidate is held to (#213: submit must not be a way to
    # bypass a check propose enforces).
    source = _write_unit(tmp_path)
    result = submit_source(
        source,
        function="my_abs",
        expression="bogus_ident >= 0",
        provider="codex",
        model="gpt-5.1",
        persist=False,
    )
    assert not result.accepted
    assert "bogus_ident" in result.rejected[0].reason


@pytest.mark.parametrize(("provider", "model"), [("", "gpt-5.1"), ("codex", "  ")])
def test_submit_source_rejects_blank_provenance(
    tmp_path: Path, provider: str, model: str
) -> None:
    source = _write_unit(tmp_path)
    with pytest.raises(BlankProvenanceError):
        submit_source(
            source,
            function="my_abs",
            expression="result >= 0",
            provider=provider,
            model=model,
            persist=False,
        )


def test_submit_source_persists_the_candidate(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    result = submit_source(
        source,
        function="my_abs",
        expression="result >= 0",
        provider="codex",
        model="gpt-5.1",
        store_root=root,
    )
    unit_id = f"{source}::my_abs"
    store = PropertyStore.open(root)
    try:
        stored = store.list_for_unit(unit_id)
    finally:
        store.close()
    assert {p.property_id for p in stored} == {p.property_id for p in result.accepted}
    assert stored[0].provenance.provider == "codex"


def test_submit_source_dry_run_does_not_persist(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    submit_source(
        source,
        function="my_abs",
        expression="result >= 0",
        provider="codex",
        model="gpt-5.1",
        persist=False,
        store_root=root,
    )
    assert not root.exists()  # dry run never opened/created the store


def test_submit_source_records_a_property_proposed_event(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    result = submit_source(
        source,
        function="my_abs",
        expression="result >= 0",
        provider="codex",
        model="gpt-5.1",
        store_root=root,
    )
    lines = events_path(root).read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "property.proposed"
    assert event["channel"] == "submitted"
    assert event["provider"] == "codex"
    assert event["model"] == "gpt-5.1"
    assert event["property_id"] == result.accepted[0].property_id
    assert event["unit_id"] == f"{source}::my_abs"


def test_submit_source_never_invokes_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No LLM call: submit_source must never shell out (`claude -p` or otherwise)."""

    def _boom(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("submit_source must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    result = submit_source(
        source=_write_unit(tmp_path),
        function="my_abs",
        expression="result >= 0",
        provider="codex",
        model="gpt-5.1",
        persist=False,
    )
    assert result.accepted


def _corrupt_store_root(tmp_path: Path) -> Path:
    root = tmp_path / ".forseti"
    root.mkdir()
    (root / "forseti.db").write_text("this is not a database")
    return root


def test_submit_source_translates_store_error(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    with pytest.raises(PropertyStoreError):
        submit_source(
            source,
            function="my_abs",
            expression="result >= 0",
            provider="codex",
            model="gpt-5.1",
            store_root=root,
        )


def test_cli_submit_property_json_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    code = main(
        [
            "submit-property",
            str(source),
            "--function",
            "my_abs",
            "--expression",
            "result >= 0",
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--no-store",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["accepted"][0]["expression"] == "result >= 0"
    assert payload["accepted"][0]["provenance"]["provider"] == "codex"


def test_cli_submit_property_rejected_candidate_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    code = main(
        [
            "submit-property",
            str(source),
            "--function",
            "my_abs",
            "--expression",
            "bogus_ident >= 0",
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--no-store",
        ]
    )
    assert code == 1
    assert "Rejected 1" in capsys.readouterr().out


def test_cli_submit_property_blank_provider_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    code = main(
        [
            "submit-property",
            str(source),
            "--function",
            "my_abs",
            "--expression",
            "result >= 0",
            "--provider",
            "",
            "--model",
            "gpt-5.1",
            "--no-store",
        ]
    )
    assert code == 1
    assert "forseti submit-property:" in capsys.readouterr().err


def test_cli_submit_property_store_error_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    code = main(
        [
            "submit-property",
            str(source),
            "--function",
            "my_abs",
            "--expression",
            "result >= 0",
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
        ]
    )
    assert code == 1
    assert "forseti submit-property:" in capsys.readouterr().err
