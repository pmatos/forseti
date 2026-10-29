"""Tests for `forseti.precond.verify` — the driver's decision logic (no ESBMC).

Both esbmc seams are injected: `list_units_fn` supplies canned units and
`raw_verify` supplies canned verdicts (inspecting the harness path to tell the
primary run from the non-vacuity probe). This exercises the escalate-on-
unwinding remap, the k-ladder settling, the non-vacuity discharge, and the
honest labelling without invoking esbmc. The real end-to-end behaviour is the
esbmc-gated acceptance test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
from forseti.precond.synth import NON_VACUITY_LABEL, RENAMED_MAIN
from forseti.precond.verify import (
    Assessment,
    PreconditionResult,
    synthesize,
    verify_precondition,
)
from forseti.properties import InvariantRole

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


def test_source_defining_main_is_checked_with_main_renamed_away(
    tmp_path: Path,
) -> None:
    # A complete program is the natural shape of real C: its own `main` must not
    # make every helper unreachable to the gate. The sidecar renames it away
    # around the `#include` instead of the driver refusing the source (#290).
    unit = Unit("g", (Param("p", "int *", array_extent=4),))
    main = Unit("main", ())
    src = tmp_path / "prog.c"
    src.write_text("int main(void){}\n")
    harnesses: list[str] = []

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        harnesses.append(source.read_text())
        return (
            _violated(f"  {NON_VACUITY_LABEL}")
            if _is_nonvacuity(source)
            else _verified()
        )

    result = verify_precondition(
        src,
        function="g",
        work_dir=tmp_path,
        raw_verify=raw,
        list_units_fn=_lister(unit, main),
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert harnesses
    assert all(f"#define main {RENAMED_MAIN}" in h for h in harnesses)


def test_the_unit_under_test_may_be_main_itself(tmp_path: Path) -> None:
    main = Unit("main", ())
    src = tmp_path / "prog.c"
    src.write_text("int main(void){}\n")
    text = synthesize(src, function="main", list_units_fn=_lister(main))
    assert f"    {RENAMED_MAIN}();" in text


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


# --- S0 under an accepted representation invariant (#387) -------------------

RING_PUSH = Unit("rb_push", (Param("r", "ring_t *"), Param("v", "int")))
RING_INV = {"ring_t": ("self->count <= CAP",)}


def _run_ring(
    raw: RawVerify, *, tmp: Path, invariants: Mapping[str, Sequence[str]] | None
) -> PreconditionResult:
    src = tmp / "ring.c"
    src.write_text("/* ring */\n")
    return verify_precondition(
        src,
        function="rb_push",
        max_len=4,
        ladder_cap=16,
        work_dir=tmp,
        raw_verify=raw,
        list_units_fn=_lister(RING_PUSH),
        invariants=invariants,
    )


def _reached_raw(harnesses: list[str] | None = None) -> RawVerify:
    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        if harnesses is not None:
            harnesses.append(source.read_text())
        if _is_nonvacuity(source):
            return _violated(f"Violated property:\n  {NON_VACUITY_LABEL}")
        return _verified()

    return raw


def test_an_accepted_invariant_is_assumed_and_the_verdict_says_so(
    tmp_path: Path,
) -> None:
    harnesses: list[str] = []
    result = _run_ring(_reached_raw(harnesses), tmp=tmp_path, invariants=RING_INV)

    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert all("__ESBMC_assume(__forseti_inv_ring_t(r));" in h for h in harnesses)
    assert len(harnesses) == 2  # the verify and its non-vacuity probe
    assert "accepted representation invariant of ring_t" in result.label
    assert "not discharged" in result.label
    payload = result.to_dict()
    assert payload["assumed"] is True
    assert payload["assumed_invariants"] == [
        {"type": "ring_t", "expressions": ["self->count <= CAP"], "params": ["r"]}
    ]


def test_without_invariants_the_verdict_and_payload_are_as_before(
    tmp_path: Path,
) -> None:
    harnesses: list[str] = []
    result = _run_ring(_reached_raw(harnesses), tmp=tmp_path, invariants=None)

    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert not any("__forseti_inv" in h for h in harnesses)
    assert "invariant" not in result.label
    assert result.to_dict()["assumed_invariants"] == []


def test_an_invariant_of_another_type_changes_nothing(tmp_path: Path) -> None:
    harnesses: list[str] = []
    result = _run_ring(
        _reached_raw(harnesses), tmp=tmp_path, invariants={"other_t": ("1",)}
    )
    assert not any("__forseti_inv" in h for h in harnesses)
    assert "invariant" not in result.label


def test_a_violation_under_the_invariant_is_labelled_conditional(
    tmp_path: Path,
) -> None:
    result = _run_ring(lambda s, *, unwind: _oob(), tmp=tmp_path, invariants=RING_INV)
    assert result.assessment is Assessment.VIOLATED
    assert "accepted representation invariant of ring_t" in result.label


def test_an_unsatisfiable_invariant_is_vacuous_never_assumed_verified(
    tmp_path: Path,
) -> None:
    result = _run_ring(
        lambda s, *, unwind: _verified(),
        tmp=tmp_path,
        invariants={"ring_t": ("self->count > self->count",)},
    )
    assert result.assessment is Assessment.VACUOUS
    assert "assumed representation invariant is contradictory" in result.detail
    assert "accepted representation invariant of ring_t" in result.label
    assert "not a pass" in result.label


def test_synthesize_renders_the_invariant_assumption(tmp_path: Path) -> None:
    src = tmp_path / "ring.c"
    src.write_text("/* ring */\n")
    text = synthesize(
        src,
        function="rb_push",
        list_units_fn=_lister(RING_PUSH),
        invariants=RING_INV,
    )
    assert "__ESBMC_assume(__forseti_inv_ring_t(r));" in text


def test_discharge_never_assumes_an_invariant(tmp_path: Path) -> None:
    import inspect

    from forseti.precond import discharge_precondition

    assert "invariants" not in inspect.signature(discharge_precondition).parameters
    harnesses: list[str] = []
    src = tmp_path / "ring.c"
    src.write_text("/* ring */\n")
    discharge_precondition(
        src,
        function="rb_push",
        max_len=4,
        ladder_cap=16,
        work_dir=tmp_path,
        raw_verify=_reached_raw(harnesses),
        list_units_fn=_lister(RING_PUSH),
        open_callers_fn=lambda _s, _sym: (),
    )
    assert harnesses
    assert not any("__forseti_inv" in h for h in harnesses)


def test_an_initialiser_is_not_assumed_the_invariant_it_establishes(
    tmp_path: Path,
) -> None:
    harnesses: list[str] = []
    src = tmp_path / "ring.c"
    src.write_text("/* ring */\n")
    init = Unit("rb_init", (Param("r", "ring_t *"),))

    def run(role: InvariantRole | None) -> PreconditionResult:
        return verify_precondition(
            src,
            function="rb_init",
            max_len=4,
            ladder_cap=16,
            work_dir=tmp_path,
            raw_verify=_reached_raw(harnesses),
            list_units_fn=_lister(init),
            invariants=RING_INV,
            invariant_role=role,
        )

    result = run(None)
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.invariants == ()
    assert not any("__forseti_inv" in h for h in harnesses)

    forced = run(InvariantRole.METHOD)
    assert [i.type_name for i in forced.invariants] == ["ring_t"]
