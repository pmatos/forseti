"""Core's representation-invariant face: submit + check, and the CLI route (#387).

Hermetic: the verifier is a scripted `verify_port`; persistence is a real store under
`tmp_path`. The ESBMC-backed pair is `test_invariant_integration.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forseti.core import (
    accepted_invariants,
    check_invariant_source,
    set_invariant_status,
    submit_invariant_source,
)
from forseti.core.cli import main
from forseti.core.events import events_path
from forseti.esbmc import EsbmcResult, Verified, Violated
from forseti.esbmc.result import RunMeta
from forseti.orchestrator import PropertyCheckRun, PropertyOutcome
from forseti.properties import (
    BlankProvenanceError,
    InvalidStatusTransition,
    InvariantRole,
    PropertyKind,
    PropertyNotFound,
    PropertyStatus,
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


def test_type_name_spelling_does_not_split_the_key(tmp_path: Path) -> None:
    source, root = tmp_path / "node.c", tmp_path / ".forseti"
    source.write_text(
        "struct node { int v; };\nvoid bump(struct node *n) { n->v++; }\n"
    )
    result = submit_invariant_source(
        source,
        type_name="struct  node",
        expression="self->v >= 0",
        provider="hand",
        model="human",
        store_root=root,
    )
    assert result.unit_id == f"{source}::struct node"
    run = check_invariant_source(
        source,
        function="bump",
        type_name="struct   node",
        store_root=root,
        verify_port=_verify,
    )
    assert [v.outcome for v in run.verdicts] == [PropertyOutcome.HELD]


# --- lifecycle: accept / reject ---------------------------------------------


def _submitted_id(source: Path, root: Path) -> str:
    _submit(source, root)
    with PropertyStore.open(root) as store:
        (prop,) = store.list_for_type(f"{source}::ring_t")
    return prop.property_id


def test_set_invariant_status_accepts_a_candidate_and_it_becomes_the_context(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    property_id = _submitted_id(source, root)
    assert accepted_invariants(source, store_root=root) == {}

    prop = set_invariant_status(
        source,
        type_name="ring_t",
        property_id=property_id,
        status=PropertyStatus.ACCEPTED,
        store_root=root,
    )

    assert prop.status is PropertyStatus.ACCEPTED
    assert accepted_invariants(source, store_root=root) == {"ring_t": (INV,)}


def test_a_rejected_invariant_is_not_the_context_and_is_final(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    property_id = _submitted_id(source, root)
    set_invariant_status(
        source,
        type_name="ring_t",
        property_id=property_id,
        status=PropertyStatus.REJECTED,
        store_root=root,
    )
    assert accepted_invariants(source, store_root=root) == {}
    with pytest.raises(InvalidStatusTransition):
        set_invariant_status(
            source,
            type_name="ring_t",
            property_id=property_id,
            status=PropertyStatus.ACCEPTED,
            store_root=root,
        )


def test_set_invariant_status_is_scoped_to_the_named_type_and_file(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    property_id = _submitted_id(source, root)
    other = tmp_path / "other.c"
    other.write_text(SOURCE)
    for wrong_source, wrong_type in ((other, "ring_t"), (source, "struct nope")):
        with pytest.raises(PropertyNotFound):
            set_invariant_status(
                wrong_source,
                type_name=wrong_type,
                property_id=property_id,
                status=PropertyStatus.ACCEPTED,
                store_root=root,
            )
    assert accepted_invariants(source, store_root=root) == {}


def test_accepted_invariants_without_a_store_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    assert accepted_invariants(_source(tmp_path), store_root=root) == {}
    assert not root.exists()


def test_accepted_invariants_groups_by_type_and_ignores_other_files(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    other = tmp_path / "other.c"
    other.write_text(SOURCE)
    for path, expression in ((source, INV), (source, "self->head < CAP"), (other, INV)):
        submit_invariant_source(
            path,
            type_name="ring_t",
            expression=expression,
            provider="hand",
            model="human",
            store_root=root,
        )
    with PropertyStore.open(root) as store:
        for prop in store.list_for_type(f"{source}::ring_t") + store.list_for_type(
            f"{other}::ring_t"
        ):
            store.update_type_invariant_status(
                prop.property_id, PropertyStatus.ACCEPTED
            )
    assert accepted_invariants(source, store_root=root) == {
        "ring_t": (INV, "self->head < CAP")
    }


def test_an_accepted_invariant_is_still_checked(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    set_invariant_status(
        source,
        type_name="ring_t",
        property_id=_submitted_id(source, root),
        status=PropertyStatus.ACCEPTED,
        store_root=root,
    )
    run = check_invariant_source(
        source,
        function="rb_push",
        type_name="ring_t",
        store_root=root,
        verify_port=_verify,
    )
    assert [v.outcome for v in run.verdicts] == [PropertyOutcome.HELD]


# --- CLI: check --type, invariant-status ------------------------------------


def _check_argv(
    source: Path, root: Path, *extra: str, function: str = "rb_push"
) -> list[str]:
    return [
        "check",
        str(source),
        "--function",
        function,
        "--type",
        "ring_t",
        "--store-root",
        str(root),
        *extra,
    ]


def test_cli_check_type_routes_to_the_invariant_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _submit(source, root)
    seen: dict[str, object] = {}

    def fake(path: Path, **kwargs: Any) -> PropertyCheckRun:
        seen.update(kwargs)
        return check_invariant_source(path, **kwargs, verify_port=_verify)

    monkeypatch.setattr("forseti.core.cli.check_invariant_source", fake)
    code = main(_check_argv(source, root, "--role", "initialiser", "--json"))

    assert code == 0
    assert seen["type_name"] == "ring_t"
    assert seen["role"] is InvariantRole.INITIALISER
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdicts"][0]["kind"] == "rep_invariant"
    assert "assumption" in payload["verdicts"][0]


def test_cli_check_type_text_output_says_assumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _submit(source, root)
    monkeypatch.setattr(
        "forseti.core.cli.check_invariant_source",
        lambda path, **kw: check_invariant_source(path, **kw, verify_port=_verify),
    )
    assert main(_check_argv(source, root)) == 0
    out = capsys.readouterr().out
    assert "HELD" in out
    assert "Assumed:" in out


def test_cli_check_type_with_nothing_stored_says_so_and_exits_zero_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    assert main(_check_argv(source, root)) == 0
    out = capsys.readouterr().out
    assert "No candidate or accepted representation invariant stored for ring_t" in out
    assert "submit-property --kind rep-invariant --type ring_t" in out
    assert "propose" not in out


def test_cli_check_type_with_only_skips_is_not_reported_as_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, root = tmp_path / "ring.c", tmp_path / ".forseti"
    source.write_text(SOURCE + "unsigned rb_count(const ring_t *r) { return 0; }\n")
    _submit(source, root)
    code = main(_check_argv(source, root, function="rb_count"))
    out = capsys.readouterr().out
    assert code == 0
    assert "no invariant was actually checked" in out
    assert "reachability" not in out
    assert "SKIPPED" in out


def test_cli_check_role_without_type_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "check",
            str(_source(tmp_path)),
            "--function",
            "rb_push",
            "--role",
            "method",
            "--store-root",
            str(tmp_path / ".forseti"),
        ]
    )
    assert code == 2
    assert "--role applies to --type only" in capsys.readouterr().err


def test_cli_check_without_type_still_checks_the_units_own_properties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _submit(source, root)
    code = main(
        ["check", str(source), "--function", "rb_push", "--store-root", str(root)]
    )
    assert code == 0
    assert "No properties stored for" in capsys.readouterr().out


def test_cli_invariant_status_accepts_then_refuses_a_second_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    property_id = _submitted_id(source, root)
    base = [
        "invariant-status",
        str(source),
        "--type",
        "ring_t",
        "--property-id",
        property_id,
        "--store-root",
        str(root),
    ]

    assert main([*base, "accepted"]) == 0
    out = capsys.readouterr().out
    assert "ACCEPTED" in out
    assert "assumed, not discharged" in out

    assert main([*base, "rejected", "--json"]) == 1
    assert "not an allowed status transition" in capsys.readouterr().err
    assert accepted_invariants(source, store_root=root) == {"ring_t": (INV,)}


def test_cli_invariant_status_json_and_unknown_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    property_id = _submitted_id(source, root)
    argv = ["invariant-status", str(source), "--type", "ring_t", "--store-root"]
    assert (
        main([*argv, str(root), "--property-id", property_id, "rejected", "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "rejected"
    assert payload["kind"] == "rep_invariant"

    assert main([*argv, str(root), "--property-id", "nope", "accepted"]) == 1
    assert "no such property: nope" in capsys.readouterr().err


def test_cli_invariant_status_store_error_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x")
    code = main(
        [
            "invariant-status",
            str(_source(tmp_path)),
            "--type",
            "ring_t",
            "--property-id",
            "x",
            "--store-root",
            str(blocked / "sub"),
            "accepted",
        ]
    )
    assert code == 1
    assert "forseti invariant-status" in capsys.readouterr().err
