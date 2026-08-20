"""Hermetic tests for Core's `check` face (`check_source` + the CLI).

No esbmc, no network: a scripted `FakeVerify` (mirrors
`tests/orchestrator/test_check.py`'s) stands in for ESBMC via `check_source`'s
`verify_port` injection seam, while a real `PropertyStore` (under `tmp_path`)
and the real `SemanticHarnessWriter` exercise the actual wiring. The
esbmc-driven round trip (real properties, real ESBMC, real quoted include) is
covered by `test_check_integration.py`.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from forseti.core import check_source
from forseti.core.cli import main
from forseti.esbmc import EsbmcResult, RunMeta, Unknown, UnknownReason, Verified
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    PropertyStoreError,
    Provenance,
    make_property_id,
)

UNIT_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"


def _meta(unwind: int) -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "harness.c", "--unwind", str(unwind)),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts, recording bounds."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.unwinds: list[int] = []

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.unwinds.append(unwind)
        return self._results.pop(0)


def _write_unit(tmp_path: Path) -> Path:
    source = tmp_path / "abs_unit.c"
    source.write_text(UNIT_SLICE)
    return source


def _semantic(unit_id: str, expression: str, domain: tuple[str, ...] = ()) -> Property:
    pid = make_property_id(unit_id, PropertyKind.SEMANTIC, expression, domain)
    return Property(
        property_id=pid,
        unit_id=unit_id,
        kind=PropertyKind.SEMANTIC,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
        domain=domain,
    )


def _reachability(unit_id: str) -> Property:
    pid = make_property_id(unit_id, PropertyKind.REACHABILITY, "reach_label")
    return Property(
        property_id=pid,
        unit_id=unit_id,
        kind=PropertyKind.REACHABILITY,
        expression="reach_label",
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
    )


def _seed_store(root: Path, prop: Property) -> None:
    store = PropertyStore.open(root)
    store.add(prop)
    store.close()


def test_check_source_held(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    fake = FakeVerify([Verified(_meta(4))])
    run = check_source(source, function="my_abs", store_root=root, verify_port=fake)

    assert run.counts()["held"] == 1
    assert fake.unwinds == [4]  # DEFAULT_UNWIND, no escalation needed


def test_check_source_ladders_on_unknown(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    fake = FakeVerify([Unknown(_meta(4), UnknownReason.TIMEOUT), Verified(_meta(8))])
    run = check_source(
        source,
        function="my_abs",
        store_root=root,
        unwind_ladder=(8,),
        verify_port=fake,
    )

    assert fake.unwinds == [4, 8]
    assert run.counts()["held"] == 1


def test_check_source_no_properties_is_empty_run(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    fake = FakeVerify([])  # never called -- nothing to check

    run = check_source(source, function="my_abs", store_root=root, verify_port=fake)

    assert run.verdicts == ()


def test_check_source_reachability_only_is_skipped_not_checked(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _reachability(unit_id))

    fake = FakeVerify([])  # reachability is SKIPPED, never verified
    run = check_source(source, function="my_abs", store_root=root, verify_port=fake)

    assert run.counts()["skipped"] == 1
    assert run.counts()["held"] == 0


def test_check_source_work_dir_defaults_beside_the_store_not_the_source(
    tmp_path: Path,
) -> None:
    # The harness must never land beside `source`: a plain `.c` sitting next to
    # a tracked source is exactly what the Claude Code adapter's git-status-
    # driven discovery would pick up as a new, unverified unit (own harness
    # verified as if it were source).
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source = source_dir / "abs_unit.c"
    source.write_text(UNIT_SLICE)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    fake = FakeVerify([Verified(_meta(4))])
    check_source(source, function="my_abs", store_root=root, verify_port=fake)

    assert list(source_dir.glob("*.c")) == [source]  # no harness landed beside it
    assert any((root / "check-work").glob("*.c"))


def _corrupt_store_root(tmp_path: Path) -> Path:
    root = tmp_path / ".forseti"
    root.mkdir()
    (root / "forseti.db").write_text("this is not a database")
    return root


def test_check_source_translates_store_error(tmp_path: Path) -> None:
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    with pytest.raises(PropertyStoreError):
        check_source(
            source, function="my_abs", store_root=root, verify_port=FakeVerify([])
        )


# --- CLI (`forseti check`) ---------------------------------------------------


def test_cli_check_json_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    fake = FakeVerify([Verified(_meta(4))])
    monkeypatch.setattr(
        "forseti.core.cli.check_source", partial(check_source, verify_port=fake)
    )
    code = main(
        [
            "check",
            str(source),
            "--function",
            "my_abs",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["held"] == 1


def test_cli_check_unwind_above_default_ladder_floor_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--unwind 8` alone (no explicit `--unwind-ladder`) used to build the fixed
    default ladder `(8, 16)` regardless, producing the non-increasing `(8, 8,
    16)` and an uncaught `ValueError` (issue #95 review). The derived default
    should drop `8` and keep the rungs above it -- `(16,)`."""
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    fake = FakeVerify([Verified(_meta(8))])
    monkeypatch.setattr(
        "forseti.core.cli.check_source", partial(check_source, verify_port=fake)
    )
    code = main(
        [
            "check",
            str(source),
            "--function",
            "my_abs",
            "--store-root",
            str(root),
            "--unwind",
            "8",
        ]
    )
    assert code == 0
    assert fake.unwinds == [8]  # no escalation needed -- (16,) never used
    assert "Traceback" not in capsys.readouterr().err


def test_cli_check_explicit_ladder_still_raises_a_clean_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicitly-supplied non-increasing ladder is still a user error, but
    the CLI must report it, not crash."""
    source = _write_unit(tmp_path)
    unit_id = f"{source}::my_abs"
    root = tmp_path / ".forseti"
    _seed_store(root, _semantic(unit_id, "result >= 0", ("x > INT64_MIN",)))

    code = main(
        [
            "check",
            str(source),
            "--function",
            "my_abs",
            "--store-root",
            str(root),
            "--unwind",
            "10",
            "--unwind-ladder",
            "8,16",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "forseti check:" in err
    assert "Traceback" not in err


def test_cli_check_store_error_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = _corrupt_store_root(tmp_path)
    code = main(
        ["check", str(source), "--function", "my_abs", "--store-root", str(root)]
    )
    assert code == 1
    assert "forseti check:" in capsys.readouterr().err


def test_cli_check_missing_source_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "check",
            str(tmp_path / "nope.c"),
            "--function",
            "my_abs",
            "--store-root",
            str(tmp_path / ".forseti"),
        ]
    )
    assert code == 1
    assert "forseti check:" in capsys.readouterr().err


def test_cli_check_no_properties_names_it_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    code = main(
        ["check", str(source), "--function", "my_abs", "--store-root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "nothing was checked" in out
    assert "forseti propose" in out


def test_parse_ladder_rejects_non_int() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "check",
                "x.c",
                "--function",
                "f",
                "--unwind-ladder",
                "8,not-a-number",
            ]
        )
