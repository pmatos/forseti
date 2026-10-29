"""Core's representation-invariant face: submit + check, and the CLI route (#387).

Hermetic: the verifier is a scripted `verify_port`; persistence is a real store under
`tmp_path`. The ESBMC-backed pair is `test_invariant_integration.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forseti.core import check_invariant_source, submit_invariant_source
from forseti.core.cli import main
from forseti.core.events import events_path
from forseti.esbmc import EsbmcResult, Verified, Violated
from forseti.esbmc.result import RunMeta
from forseti.orchestrator import PropertyOutcome
from forseti.properties import (
    BlankProvenanceError,
    PropertyKind,
    PropertyStore,
)

SOURCE = """\
#define CAP 4
typedef struct { unsigned head, tail, count; } ring_t;
int rb_push(ring_t *r, int v) { return 1; }
"""
INV = "self->count <= CAP && self->tail < CAP"


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "ring.c"
    path.write_text(SOURCE)
    return path


def _submit(source: Path, root: Path) -> None:
    submit_invariant_source(
        source,
        type_name="ring_t",
        expression=INV,
        provider="hand",
        model="human",
        store_root=root,
    )


def test_submit_stores_a_candidate_keyed_by_type(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    result = submit_invariant_source(
        source,
        type_name="ring_t",
        expression=f"  {INV}  ",
        provider="hand",
        model="human",
        rationale="count and tail stay in range",
        store_root=root,
    )
    (prop,) = result.accepted
    assert not result.rejected
    assert result.unit_id == f"{source}::ring_t"
    assert prop.kind is PropertyKind.REP_INVARIANT
    assert prop.expression == INV
    assert prop.description == "count and tail stay in range"
    assert prop.provenance.provider == "hand"
    with PropertyStore.open(root) as store:
        assert store.list_for_type(f"{source}::ring_t") == (prop,)
        assert store.list_for_unit(f"{source}::ring_t") == ()
    events = [json.loads(line) for line in events_path(root).read_text().splitlines()]
    assert [e["type"] for e in events] == ["property.proposed"]
    assert events[0]["channel"] == "submitted"


def test_submit_is_idempotent_and_dry_run_touches_nothing(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    dry = submit_invariant_source(
        source,
        type_name="ring_t",
        expression=INV,
        provider="hand",
        model="human",
        persist=False,
        store_root=root,
    )
    assert dry.accepted
    assert not root.exists()
    for _ in range(2):
        _submit(source, root)
    with PropertyStore.open(root) as store:
        assert len(store.list_for_type(f"{source}::ring_t")) == 1


@pytest.mark.parametrize(
    ("type_name", "expression", "fragment"),
    [
        ("ring_t", "0", "vacuous"),
        ("ring_t", "", "empty"),
        ("ring_t", "self->count++ < CAP", "unsafe"),
        ("ring_t", "check(self)", "unsafe"),
        ("nosuch_t", INV, "does not occur"),
        ("ring_t *", INV, "not a struct type name"),
    ],
)
def test_submit_rejects_statically_and_stores_nothing(
    tmp_path: Path, type_name: str, expression: str, fragment: str
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    result = submit_invariant_source(
        source,
        type_name=type_name,
        expression=expression,
        provider="hand",
        model="human",
        store_root=root,
    )
    assert not result.accepted
    assert fragment in result.rejected[0].reason
    with PropertyStore.open(root) as store:
        assert store.list_for_type(f"{source}::{type_name}") == ()


@pytest.mark.parametrize(("provider", "model"), [("", "m"), ("p", " ")])
def test_submit_rejects_blank_provenance(
    tmp_path: Path, provider: str, model: str
) -> None:
    with pytest.raises(BlankProvenanceError):
        submit_invariant_source(
            _source(tmp_path),
            type_name="ring_t",
            expression=INV,
            provider=provider,
            model=model,
            persist=False,
        )


def _meta() -> RunMeta:
    return RunMeta("8.6.0", ("esbmc",), 0, 0.0, "", "")


def _verify(source: Path, /, *, unwind: int) -> EsbmcResult:
    if source.name.endswith(".probe.c"):
        return Violated(_meta(), "Violated property:\n  forseti:non-vacuity", None)
    return Verified(_meta())


def test_check_runs_the_types_invariants_against_one_function(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _submit(source, root)
    run = check_invariant_source(
        source,
        function="rb_push",
        type_name="ring_t",
        store_root=root,
        verify_port=_verify,
    )
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.unit_id == f"{source}::rb_push"
    assert "assumption" in verdict.to_dict()
    assert any((root / "check-work").rglob("*.probe.c"))
    events = [json.loads(line) for line in events_path(root).read_text().splitlines()]
    assert [e["type"] for e in events][-2:] == [
        "property.check.start",
        "property.verdict",
    ]


def test_check_without_a_stored_invariant_is_empty_not_held(tmp_path: Path) -> None:
    run = check_invariant_source(
        _source(tmp_path),
        function="rb_push",
        type_name="ring_t",
        store_root=tmp_path / ".forseti",
        verify_port=_verify,
    )
    assert run.verdicts == ()
    assert run.outcome == "empty"


def test_check_rejects_a_negative_length_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_len"):
        check_invariant_source(
            _source(tmp_path),
            function="rb_push",
            type_name="ring_t",
            store_root=tmp_path / ".forseti",
            verify_port=_verify,
            max_len=-1,
        )


def _cli(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "submit-property",
        str(_source(tmp_path)),
        "--expression",
        INV,
        "--provider",
        "hand",
        "--model",
        "human",
        *extra,
    ]


def test_cli_rep_invariant_json_and_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".forseti"
    code = main(
        _cli(
            tmp_path,
            "--kind",
            "rep-invariant",
            "--type",
            "ring_t",
            "--store-root",
            str(root),
            "--json",
        )
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_id"].endswith("ring.c::ring_t")
    assert payload["accepted"][0]["kind"] == "rep_invariant"
    with PropertyStore.open(root) as store:
        assert len(store.list_for_type(payload["unit_id"])) == 1


def test_cli_rep_invariant_vacuous_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _cli(tmp_path, "--kind", "rep-invariant", "--type", "ring_t", "--no-store")
    argv[argv.index(INV)] = "0"
    assert main(argv) == 1
    assert "vacuous" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        (("--kind", "rep-invariant"), "requires --type"),
        (
            ("--kind", "rep-invariant", "--type", "ring_t", "--function", "rb_push"),
            "per type",
        ),
        (
            ("--kind", "rep-invariant", "--type", "ring_t", "--domain", "self"),
            "per type",
        ),
        ((), "--function"),
        (("--function", "rb_push", "--type", "ring_t"), "rep-invariant only"),
    ],
)
def test_cli_submit_property_flag_misuse_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
    fragment: str,
) -> None:
    assert main(_cli(tmp_path, "--no-store", *extra)) == 2
    assert fragment in capsys.readouterr().err


def test_cli_store_error_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x")
    code = main(
        _cli(
            tmp_path,
            "--kind",
            "rep-invariant",
            "--type",
            "ring_t",
            "--store-root",
            str(blocked / "sub"),
        )
    )
    assert code == 1
    assert "forseti submit-property" in capsys.readouterr().err
