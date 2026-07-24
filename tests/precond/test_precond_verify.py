"""Tests for `forseti.precond.verify` — the driver's decision logic (no ESBMC).

Both esbmc seams are injected: `list_units_fn` supplies canned units and
`raw_verify` supplies canned verdicts (inspecting the harness path to tell the
primary run from the non-vacuity probe). This exercises the escalate-on-
unwinding remap, the k-ladder settling, the non-vacuity discharge, and the
honest labelling without invoking esbmc. The real end-to-end behaviour is the
esbmc-gated acceptance test.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unit,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.esbmc.units import Param
from forseti.precond.synth import NON_VACUITY_LABEL
from forseti.precond.verify import (
    Assessment,
    PreconditionResult,
    verify_precondition,
)

RawVerify = Callable[..., EsbmcResult]


def _meta() -> RunMeta:
    return RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")


def _verified() -> Verified:
    return Verified(_meta())


def _violated(text: str) -> Violated:
    return Violated(_meta(), text, None)


def _unwinding() -> Violated:
    return _violated("Violated property:\n  unwinding assertion loop 4")


def _oob() -> Violated:
    return _violated("Violated property:\n  dereference failure: array bounds violated")


def _is_nonvacuity(source: Path) -> bool:
    return "nonvacuity" in source.name


def _lister(*units: Unit) -> Callable[[Path], list[Unit]]:
    return lambda _source: list(units)


UPDATE = Unit(
    "sha1_update",
    (
        Param("ctx", "sha1_ctx *"),
        Param("data", "const uint8_t *"),
        Param("len", "unsigned long"),
    ),
)


def _run(
    raw: RawVerify,
    *,
    unit: Unit = UPDATE,
    function: str = "sha1_update",
    tmp: Path,
    max_len: int = 8,
    ladder_cap: int = 32,
) -> PreconditionResult:
    src = tmp / "sha1.c"
    src.write_text("/* sha1 */\n")
    return verify_precondition(
        src,
        function=function,
        max_len=max_len,
        ladder_cap=ladder_cap,
        work_dir=tmp,
        raw_verify=raw,
        list_units_fn=_lister(unit),
    )


def test_assumed_verified_after_escalation(tmp_path: Path) -> None:
    # Under-unwound (unwinding assertion) at k=9, then VERIFIED at k=17; the
    # non-vacuity probe is reachable → ASSUMED_VERIFIED at the settled k.
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        if _is_nonvacuity(source):
            return _violated(f"Violated property:\n  {NON_VACUITY_LABEL}")
        return _verified() if unwind >= 17 else _unwinding()

    result = _run(raw, tmp=tmp_path)
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.settled_k == 18
    assert result.max_len == 8
    assert "assuming valid caller pointers" in result.label


def test_real_violation_is_reported_without_nonvacuity(tmp_path: Path) -> None:
    seen: list[Path] = []

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        seen.append(source)
        return _oob()

    result = _run(raw, tmp=tmp_path)
    assert result.assessment is Assessment.VIOLATED
    # a real out-of-bounds stops the ladder at the first rung and never runs the
    # non-vacuity probe (the counterexample is itself reachability evidence).
    assert all(not _is_nonvacuity(p) for p in seen)
    assert result.settled_k == 9


def test_vacuous_when_call_site_unreachable(tmp_path: Path) -> None:
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        # primary VERIFIED, but the assert-at-call-site is also unreachable.
        return _verified()

    result = _run(raw, tmp=tmp_path)
    assert result.assessment is Assessment.VACUOUS
    assert "not a pass" in result.label


def test_ladder_exhausted_is_unknown(tmp_path: Path) -> None:
    # Every rung under-unwinds → remapped to UNKNOWN → terminal UNKNOWN.
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        return _unwinding()

    result = _run(raw, tmp=tmp_path, ladder_cap=32)
    assert result.assessment is Assessment.UNKNOWN
    assert result.settled_k == 32  # the cap (top rung)


def test_nonvacuity_inconclusive_is_unknown(tmp_path: Path) -> None:
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        if _is_nonvacuity(source):
            return Unknown(_meta(), UnknownReason.TIMEOUT)
        return _verified()

    result = _run(raw, tmp=tmp_path)
    assert result.assessment is Assessment.UNKNOWN


def test_needs_contract_for_unresolved_pointer(tmp_path: Path) -> None:
    called = False

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        nonlocal called
        called = True
        return _verified()

    unit = Unit("g", (Param("p", "void *"),))
    result = _run(raw, unit=unit, function="g", tmp=tmp_path)
    assert result.assessment is Assessment.NEEDS_CONTRACT
    assert "p" in result.detail
    assert not called  # never shelled out to esbmc


def test_source_defining_main_is_refused(tmp_path: Path) -> None:
    unit = Unit("g", (Param("p", "int *", array_extent=4),))
    main = Unit("main", ())
    src = tmp_path / "sha1.c"
    src.write_text("int main(void){}\n")
    result = verify_precondition(
        src,
        function="g",
        work_dir=tmp_path,
        raw_verify=lambda s, *, unwind: _verified(),
        list_units_fn=_lister(unit, main),
    )
    assert result.assessment is Assessment.ERROR
    assert "main" in result.detail


def test_unknown_function_is_error(tmp_path: Path) -> None:
    result = _run(lambda s, *, unwind: _verified(), function="missing", tmp=tmp_path)
    assert result.assessment is Assessment.ERROR
    assert "missing" in result.detail


def test_to_dict_shape(tmp_path: Path) -> None:
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        if _is_nonvacuity(source):
            return _violated(f"  {NON_VACUITY_LABEL}")
        return _verified() if unwind >= 17 else _unwinding()

    payload = _run(raw, tmp=tmp_path).to_dict()
    assert payload["assessment"] == "assumed_verified"
    assert payload["assumed"] is True
    assert payload["settled_k"] == 18
    assert {p["name"] for p in payload["params"]} == {"ctx", "data", "len"}
