"""Core's `discharge_invariant_source` and the `invariant-discharge` CLI (#430).

Hermetic: the clang listing and the verifier are injected, the store is real under
`tmp_path`. The ESBMC-backed pair is `test_invariant_discharge_integration.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forseti.core import (
    discharge_invariant_source,
    set_invariant_status,
    submit_invariant_source,
)
from forseti.core.cli import main
from forseti.esbmc import EsbmcResult, ListUnitsError, RunMeta, Unit, Verified, Violated
from forseti.esbmc.units import Param
from forseti.orchestrator import VerifyPort
from forseti.precond.invariant_discharge import (
    DISCHARGE_LABEL_PREFIX,
    DISCHARGE_SITE_LABEL_PREFIX,
    DischargeStatus,
    InvariantDischarge,
    InvariantDischargeRun,
)
from forseti.properties import PropertyStatus

SOURCE = """\
typedef struct { unsigned count; } ring_t;
static void rb_init(ring_t *r) { r->count = 0; }
static int rb_push(ring_t *r, int v) { r->count++; return 1; }
int main(void) { ring_t r; rb_init(&r); rb_push(&r, 1); return 0; }
"""
INV = "self->count <= 4"
R = Param("r", "ring_t *")
UNITS = [
    Unit("rb_init", (R,), (), internal_linkage=True),
    Unit("rb_push", (R, Param("v", "int")), (), internal_linkage=True),
    Unit("main", (), ("rb_init", "rb_push")),
]


def _meta() -> RunMeta:
    return RunMeta("8.6.0", ("esbmc",), 0, 0.0, "", "")


def _port(caller: EsbmcResult) -> VerifyPort:
    def port(source: Path, /, *, unwind: int) -> EsbmcResult:
        if source.name.endswith("__site.c"):
            return Violated(_meta(), DISCHARGE_SITE_LABEL_PREFIX, None)
        return caller

    return port


def _setup(tmp_path: Path, *, submit: bool = True) -> tuple[Path, Path]:
    source, root = tmp_path / "ring.c", tmp_path / ".forseti"
    source.write_text(SOURCE)
    if submit:
        submit_invariant_source(
            source,
            type_name="ring_t",
            expression=INV,
            provider="hand",
            model="human",
            store_root=root,
        )
    return source, root


def _discharge(
    source: Path,
    root: Path,
    caller: EsbmcResult,
    initialisers: tuple[str, ...] = (),
) -> InvariantDischargeRun:
    return discharge_invariant_source(
        source,
        type_name=" ring_t ",
        store_root=root,
        initialisers=initialisers,
        verify_port=_port(caller),
        list_units_fn=lambda _p: list(UNITS),
        open_callers_fn=lambda _p, _s: (),
    )


def test_the_stored_invariant_is_discharged_at_the_static_callee(
    tmp_path: Path,
) -> None:
    source, root = _setup(tmp_path)
    run = _discharge(source, root, Verified(_meta()))
    assert run.type_key == f"{source}::ring_t"
    (discharge,) = run.discharges
    assert (discharge.callee, discharge.status) == (
        "rb_push",
        DischargeStatus.DISCHARGED,
    )
    assert run.outcome == "discharged"


def test_a_caller_that_breaks_it_is_reported(tmp_path: Path) -> None:
    source, root = _setup(tmp_path)
    broken = Violated(_meta(), f"  {DISCHARGE_LABEL_PREFIX}rb_push:r", None)
    assert _discharge(source, root, broken).outcome == "violated"


def test_a_named_initialiser_is_not_swept(tmp_path: Path) -> None:
    source, root = _setup(tmp_path)
    run = _discharge(source, root, Verified(_meta()), initialisers=("rb_push",))
    assert run.discharges == ()


def test_a_rejected_invariant_is_not_discharged(tmp_path: Path) -> None:
    source, root = _setup(tmp_path)
    (stored,) = [
        d.property_id for d in _discharge(source, root, Verified(_meta())).discharges
    ]
    set_invariant_status(
        source,
        type_name="ring_t",
        property_id=stored,
        status=PropertyStatus.REJECTED,
        store_root=root,
    )
    assert _discharge(source, root, Verified(_meta())).outcome == "empty"


def test_without_a_store_nothing_is_created_and_nothing_is_discharged(
    tmp_path: Path,
) -> None:
    source, root = _setup(tmp_path, submit=False)
    run = _discharge(source, root, Verified(_meta()))
    assert run.outcome == "empty"
    assert not root.exists()


def _patched(
    monkeypatch: pytest.MonkeyPatch, discharges: tuple[InvariantDischarge, ...]
) -> None:
    def fake(source: Path, **kw: object) -> InvariantDischargeRun:
        return InvariantDischargeRun(f"{source}::ring_t", discharges)

    monkeypatch.setattr("forseti.core.cli.discharge_invariant_source", fake)


def _one(status: DischargeStatus) -> tuple[InvariantDischarge, ...]:
    return (InvariantDischarge("pid", INV, "rb_push", status, "because", (), 4),)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DischargeStatus.DISCHARGED, 0),
        (DischargeStatus.INCOMPLETE, 0),
        (DischargeStatus.VIOLATED, 1),
        (DischargeStatus.UNKNOWN, 2),
    ],
)
def test_cli_exit_codes_and_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: DischargeStatus,
    code: int,
) -> None:
    _patched(monkeypatch, _one(status))
    assert main(["invariant-discharge", "x.c", "--type", "ring_t"]) == code
    out = capsys.readouterr().out
    assert "rb_push():" in out
    assert "Invariant pid: self->count <= 4" in out


def test_cli_json_carries_discharged_and_assumed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patched(monkeypatch, _one(DischargeStatus.INCOMPLETE))
    assert main(["invariant-discharge", "x.c", "--type", "ring_t", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "incomplete"
    (entry,) = payload["discharges"]
    assert (entry["discharged"], entry["assumed"]) == (False, True)


def test_cli_passes_its_flags_to_core(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def fake(source: Path, **kw: object) -> InvariantDischargeRun:
        seen.update(kw)
        return InvariantDischargeRun("x.c::ring_t", ())

    monkeypatch.setattr("forseti.core.cli.discharge_invariant_source", fake)
    argv = ["invariant-discharge", "x.c", "--type", "ring_t", "-k", "6"]
    assert main([*argv, "--initialiser", "mk", "--initialiser", "boot"]) == 0
    assert seen["initialisers"] == ("mk", "boot")
    assert seen["type_name"] == "ring_t"
    assert seen["unwind"] == 6
    assert "Nothing was discharged" in capsys.readouterr().out


def test_cli_failures_exit_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(source: Path, **kw: object) -> InvariantDischargeRun:
        raise ListUnitsError("esbmc parse failed")

    monkeypatch.setattr("forseti.core.cli.discharge_invariant_source", broken)
    assert main(["invariant-discharge", "x.c", "--type", "ring_t"]) == 1
    assert "esbmc parse failed" in capsys.readouterr().err


def test_the_parse_sees_the_same_flags_and_budget_the_verifier_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, root = _setup(tmp_path)
    seen: dict[str, object] = {}

    def fake_analyze(path: Path, **kw: object) -> object:
        seen.update(kw)
        raise ListUnitsError("stop here")

    monkeypatch.setattr(
        "forseti.precond.invariant_discharge.analyze_translation_unit", fake_analyze
    )
    with pytest.raises(ListUnitsError):
        discharge_invariant_source(
            source,
            type_name="ring_t",
            store_root=root,
            timeout_s=300.0,
            extra_flags=("-DFEATURE",),
            verify_port=_port(Verified(_meta())),
        )
    assert seen["extra_flags"] == ("-DFEATURE",)
    assert seen["timeout_s"] == 300.0
