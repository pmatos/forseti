"""Tests for `forseti.precond.invariant_discharge` (no ESBMC).

The injection is checked on text; the sweep through `discharge_invariants` with the
clang listing and the verifier both injected, dispatching on the generated
harness's filename so each phase (a caller's obligation run, its site probe) is
answered independently. The property under test is the same as S3's: a discharge
fails closed, and an invariant that is not discharged is reported as assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forseti.esbmc import (
    CallerOpenings,
    EsbmcResult,
    RunMeta,
    Unit,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.esbmc.units import Param
from forseti.precond import CallerCheck, CallerOutcome, SynthError
from forseti.precond.discharge import open_caller_checks
from forseti.precond.invariant_discharge import (
    DISCHARGE_EXIT_CODES,
    DISCHARGE_LABEL_PREFIX,
    DISCHARGE_SITE_LABEL_PREFIX,
    DischargeStatus,
    InvariantDischarge,
    InvariantDischargeRun,
    assuming_functions,
    discharge_invariants,
    inject_invariant_obligation,
    invariant_discharge_transcript,
    render_caller_harness,
)
from forseti.properties import Property, Provenance, make_invariant

INVARIANT = "self->count <= 4"
SOURCE = """\
typedef struct { unsigned count; } ring_t;

static void rb_init(ring_t *r) { r->count = 0; }
static int rb_push(ring_t *r, int v) { r->count++; return 1; }
static int rb_len(const ring_t *self) { return self->count; }
int main(void) {
    ring_t r;
    rb_init(&r);
    rb_push(&r, 1);
    return 0;
}
"""
RING = "ring_t *"


def _unit(
    name: str,
    params: tuple[Param, ...] = (),
    calls: tuple[str, ...] = (),
    *,
    internal: bool = True,
) -> Unit:
    return Unit(name, params, calls, internal_linkage=internal)


R = Param("r", RING)
INIT = _unit("rb_init", (R,))
PUSH = _unit("rb_push", (R, Param("v", "int")), calls=())
LEN = _unit("rb_len", (Param("self", "const ring_t *"),))
MAIN = _unit("main", (), ("rb_init", "rb_push", "rb_len"), internal=False)


def _meta() -> RunMeta:
    return RunMeta("8.6.0", ("esbmc",), 0, 0.0, "", "")


def _verified() -> Verified:
    return Verified(_meta())


def _violated(label: str = "") -> Violated:
    return Violated(_meta(), f"Violated property:\n  {label}", None)


def _property() -> Property:
    return make_invariant(
        type_key="x.c::ring_t",
        expression=INVARIANT,
        provenance=Provenance("p", "1", "hand", "human"),
    )


class Port:
    """A canned verifier: one answer per phase, a log of every file it was given."""

    def __init__(
        self,
        caller: EsbmcResult | None = None,
        site: EsbmcResult | None = None,
    ) -> None:
        self.caller = caller or _verified()
        self.site = site or _violated(DISCHARGE_SITE_LABEL_PREFIX)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        self.calls.append((source.name, unwind))
        return self.site if source.name.endswith("__site.c") else self.caller


def _run(
    tmp_path: Path,
    port: Port,
    *,
    units: tuple[Unit, ...],
    callee_filter: str | None = None,
    openings: tuple[CallerCheck, ...] = (),
    initialisers: frozenset[str] = frozenset(),
    ladder: tuple[int, ...] = (),
) -> tuple[InvariantDischarge, ...]:
    source = tmp_path / "x.c"
    source.write_text(SOURCE)
    results = discharge_invariants(
        source,
        type_name="ring_t",
        invariants=[_property()],
        port=port,
        unwind=4,
        unwind_ladder=ladder,
        initialisers=initialisers,
        work_dir=tmp_path / "work",
        list_units_fn=lambda _path: list(units),
        open_callers_fn=lambda _path, _symbol: openings,
    )
    if callee_filter is None:
        return results
    return tuple(r for r in results if r.callee == callee_filter)


def _push(
    tmp_path: Path,
    port: Port,
    *,
    units: tuple[Unit, ...] = (INIT, PUSH, MAIN),
    openings: tuple[CallerCheck, ...] = (),
    ladder: tuple[int, ...] = (),
) -> InvariantDischarge:
    (result,) = _run(
        tmp_path,
        port,
        units=units,
        callee_filter="rb_push",
        openings=openings,
        ladder=ladder,
    )
    return result


def test_assuming_functions_skip_initialisers_and_other_types() -> None:
    other = _unit("takes_int", (Param("out", "int *"),))
    double = _unit("takes_ptr_ptr", (Param("p", "ring_t **"),))
    found = assuming_functions((INIT, PUSH, LEN, other, double, MAIN), "ring_t")
    assert [(u.name, names) for u, names in found] == [
        ("rb_push", ("r",)),
        ("rb_len", ("self",)),
    ]


def test_an_explicit_initialiser_is_skipped_too() -> None:
    build = _unit("ring_build", (R,))
    found = assuming_functions((build, PUSH), "ring_t", frozenset({"ring_build"}))
    assert [u.name for u, _ in found] == ["rb_push"]


def test_injection_is_a_same_line_block_that_leaves_every_line_number() -> None:
    copy = inject_invariant_obligation(
        SOURCE,
        PUSH,
        ("r",),
        type_name="ring_t",
        expression=f" {INVARIANT} ",
        source_path="/proj/x.c",
    )
    block = (
        f" {{ const ring_t *self = r; __ESBMC_assert(({INVARIANT}), "
        f'"{DISCHARGE_LABEL_PREFIX}rb_push:r"); }}'
    )
    assert block in copy
    assert copy.replace(block, "").endswith(SOURCE)
    prelude, _, rest = copy.partition("#line 1")
    assert "#define NDEBUG" in prelude
    assert rest.startswith(' "/proj/x.c"\n')
    original = SOURCE.splitlines()
    injected = rest.splitlines()[1:]
    assert len(injected) == len(original)
    assert injected[3].startswith("static int rb_push(ring_t *r, int v) {")


def test_a_parameter_named_self_is_asserted_over_directly() -> None:
    copy = inject_invariant_obligation(
        SOURCE,
        LEN,
        ("self",),
        type_name="ring_t",
        expression=INVARIANT,
        source_path="/proj/x.c",
    )
    assert (
        f' {{ __ESBMC_assert(({INVARIANT}), "{DISCHARGE_LABEL_PREFIX}rb_len:self"); }}'
        in copy
    )
    assert "const ring_t *self = self" not in copy


def test_every_parameter_of_the_type_gets_a_block() -> None:
    two = _unit("rb_merge", (Param("a", RING), Param("b", RING)))
    text = "void rb_merge(ring_t *a, ring_t *b) {\n}\n"
    copy = inject_invariant_obligation(
        text,
        two,
        ("a", "b"),
        type_name="ring_t",
        expression=INVARIANT,
        source_path="x.c",
    )
    assert f"{DISCHARGE_LABEL_PREFIX}rb_merge:a" in copy
    assert f"{DISCHARGE_LABEL_PREFIX}rb_merge:b" in copy


def test_site_probe_replaces_the_obligation_with_a_labelled_assert() -> None:
    copy = inject_invariant_obligation(
        SOURCE,
        PUSH,
        ("r",),
        type_name="ring_t",
        expression=INVARIANT,
        source_path="x.c",
        site_probe=True,
    )
    assert f'__ESBMC_assert(0, "{DISCHARGE_SITE_LABEL_PREFIX}rb_push");' in copy
    assert DISCHARGE_LABEL_PREFIX not in copy


def test_an_unnamed_parameter_or_missing_definition_is_a_synth_error() -> None:
    with pytest.raises(SynthError, match="unnamed"):
        inject_invariant_obligation(
            SOURCE, PUSH, ("",), type_name="ring_t", expression="1", source_path="x"
        )
    ghost = _unit("ghost", (R,))
    with pytest.raises(SynthError, match="no definition of ghost"):
        inject_invariant_obligation(
            SOURCE, ghost, ("r",), type_name="ring_t", expression="1", source_path="x"
        )


def test_the_caller_harness_runs_main_under_its_renamed_symbol(tmp_path: Path) -> None:
    text = render_caller_harness(tmp_path / "copy.c", "main")
    assert "#define main __forseti_source_main" in text
    assert f'#include "{tmp_path / "copy.c"}"' in text
    assert "    __forseti_source_main();" in text
    assert "    driver();" in render_caller_harness(tmp_path / "c.c", "driver")


def test_a_clean_sweep_of_a_static_callee_is_discharged(tmp_path: Path) -> None:
    port = Port()
    result = _push(tmp_path, port)
    assert result.status is DischargeStatus.DISCHARGED
    assert result.k == 4
    assert [c.outcome for c in result.callers] == [CallerOutcome.DISCHARGED]
    assert "VERIFIED up to k=4" in result.label
    assert result.to_dict()["discharged"] is True
    assert result.to_dict()["assumed"] is False
    assert [name for name, _ in port.calls] == [
        "rb_push__inv__main.c",
        "rb_push__inv__main__site.c",
    ]


def test_the_obligation_files_are_the_injected_copies(tmp_path: Path) -> None:
    _push(tmp_path, Port())
    work = tmp_path / "work"
    assert DISCHARGE_LABEL_PREFIX in (work / "rb_push__inv__obligations.c").read_text()
    assert (
        DISCHARGE_SITE_LABEL_PREFIX
        in (work / "rb_push__inv__obligation_site.c").read_text()
    )
    assert (tmp_path / "x.c").read_text() == SOURCE


def test_an_externally_visible_callee_stays_assumed(tmp_path: Path) -> None:
    public = _unit("rb_push", (R, Param("v", "int")), internal=False)
    result = _push(tmp_path, Port(), units=(INIT, public, MAIN))
    assert result.status is DischargeStatus.INCOMPLETE
    assert "externally visible" in result.detail
    assert result.label.startswith("ASSUMED (not discharged)")
    assert result.to_dict()["assumed"] is True


def test_a_caller_that_breaks_the_invariant_is_violated(tmp_path: Path) -> None:
    port = Port(caller=_violated(f"{DISCHARGE_LABEL_PREFIX}rb_push:r"))
    result = _push(tmp_path, port)
    assert result.status is DischargeStatus.VIOLATED
    assert [c.outcome for c in result.callers] == [CallerOutcome.OBLIGATION_VIOLATED]
    assert "main()" in result.detail
    assert len(port.calls) == 1, "a violated caller is not probed"
    assert "counterexample" in result.callers[0].to_dict()


def test_a_caller_faulting_on_something_else_is_unknown(tmp_path: Path) -> None:
    result = _push(tmp_path, Port(caller=_violated("array bounds violated")))
    assert result.status is DischargeStatus.UNKNOWN
    assert [c.outcome for c in result.callers] == [CallerOutcome.CALLER_VIOLATED]


def test_an_inconclusive_run_is_unknown_and_climbs_the_ladder(tmp_path: Path) -> None:
    port = Port(caller=Unknown(_meta(), UnknownReason.TIMEOUT))
    result = _push(tmp_path, port, ladder=(8,))
    assert result.status is DischargeStatus.UNKNOWN
    assert [unwind for _, unwind in port.calls] == [4, 8]
    assert "inconclusive up to k=8" in result.detail


def test_a_dead_call_site_discharges_nothing(tmp_path: Path) -> None:
    result = _push(tmp_path, Port(site=_verified()))
    assert result.status is DischargeStatus.INCOMPLETE
    assert [c.outcome for c in result.callers] == [CallerOutcome.UNREACHABLE]


def test_an_inconclusive_probe_withholds_the_discharge(tmp_path: Path) -> None:
    result = _push(tmp_path, Port(site=Unknown(_meta(), UnknownReason.TIMEOUT)))
    assert result.status is DischargeStatus.UNKNOWN
    assert [c.outcome for c in result.callers] == [CallerOutcome.UNCHECKED]


def test_a_caller_with_parameters_is_not_a_constructing_caller(
    tmp_path: Path,
) -> None:
    relay = _unit("rb_push_twice", (R,), ("rb_push",))
    port = Port()
    result = _push(tmp_path, port, units=(INIT, PUSH, relay))
    assert result.status is DischargeStatus.INCOMPLETE
    (check,) = result.callers
    assert check.outcome is CallerOutcome.UNCHECKED
    assert "takes parameters" in check.detail
    assert port.calls == [], "a caller with parameters is never verified"


def test_no_caller_in_the_translation_unit_exports_the_invariant(
    tmp_path: Path,
) -> None:
    quiet = _unit("main", (), ("rb_init",))
    result = _push(tmp_path, Port(), units=(INIT, PUSH, quiet))
    assert result.status is DischargeStatus.INCOMPLETE
    assert "no caller of rb_push()" in result.detail


def test_an_open_caller_set_withholds_the_discharge(tmp_path: Path) -> None:
    escaped = open_caller_checks(
        "rb_push",
        Path("x.c"),
        CallerOpenings((), ("fp",), (), (), ()),
    )
    result = _push(tmp_path, Port(), openings=escaped)
    assert result.status is DischargeStatus.INCOMPLETE
    assert "fp()" in result.detail


def test_a_failure_at_a_reentrant_callee_is_not_blamed_on_the_caller(
    tmp_path: Path,
) -> None:
    recursive = _unit("rb_push", (R, Param("v", "int")), ("rb_push",))
    port = Port(caller=_violated(f"{DISCHARGE_LABEL_PREFIX}rb_push:r"))
    result = _push(tmp_path, port, units=(INIT, recursive, MAIN_CALLING_PUSH))
    assert result.status is DischargeStatus.INCOMPLETE
    assert {c.outcome for c in result.callers} == {CallerOutcome.UNATTRIBUTED}


MAIN_CALLING_PUSH = _unit("main", (), ("rb_init", "rb_push"), internal=False)


def test_a_const_method_is_swept_like_any_other(tmp_path: Path) -> None:
    results = _run(tmp_path, Port(), units=(INIT, PUSH, LEN, MAIN))
    assert [r.callee for r in results] == ["rb_push", "rb_len"]
    assert all(r.status is DischargeStatus.DISCHARGED for r in results)


def test_an_initialiser_receives_no_obligation(tmp_path: Path) -> None:
    results = _run(tmp_path, Port(), units=(INIT, PUSH, MAIN))
    assert "rb_init" not in {r.callee for r in results}


def test_nothing_is_run_without_an_invariant_or_a_function_of_the_type(
    tmp_path: Path,
) -> None:
    source = tmp_path / "x.c"
    source.write_text(SOURCE)
    port = Port()

    def discharged(type_name: str, invariants: list[Property]) -> tuple[object, ...]:
        return discharge_invariants(
            source,
            type_name=type_name,
            invariants=invariants,
            port=port,
            unwind=4,
            list_units_fn=lambda _p: [INIT, PUSH, MAIN],
            open_callers_fn=lambda _p, _s: (),
        )

    assert discharged("ring_t", []) == ()
    assert discharged("other_t", [_property()]) == ()
    assert port.calls == []


def test_a_malformed_ladder_and_half_injected_hooks_are_value_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "x.c"
    source.write_text(SOURCE)
    with pytest.raises(ValueError, match="increasing"):
        discharge_invariants(
            source,
            type_name="ring_t",
            invariants=[_property()],
            port=Port(),
            unwind=4,
            unwind_ladder=(2,),
            list_units_fn=lambda _p: [],
            open_callers_fn=lambda _p, _s: (),
        )
    with pytest.raises(ValueError, match="both"):
        discharge_invariants(
            source,
            type_name="ring_t",
            invariants=[_property()],
            port=Port(),
            unwind=4,
            list_units_fn=lambda _p: [],
        )


def test_the_run_outcome_is_worst_wins_and_empty_is_not_a_discharge() -> None:
    def make(status: DischargeStatus) -> InvariantDischarge:
        return InvariantDischarge("id", "e", "f", status, "why")

    def outcome(*statuses: DischargeStatus) -> str:
        return InvariantDischargeRun("x.c::T", tuple(map(make, statuses))).outcome

    assert outcome() == "empty"
    assert outcome(DischargeStatus.DISCHARGED) == "discharged"
    assert (
        outcome(DischargeStatus.DISCHARGED, DischargeStatus.INCOMPLETE) == "incomplete"
    )
    assert outcome(DischargeStatus.INCOMPLETE, DischargeStatus.UNKNOWN) == "unknown"
    assert outcome(DischargeStatus.UNKNOWN, DischargeStatus.VIOLATED) == "violated"
    assert DISCHARGE_EXIT_CODES["violated"] == 1
    assert DISCHARGE_EXIT_CODES["unknown"] == 2
    assert DISCHARGE_EXIT_CODES["empty"] == 0


def test_transcript_tells_a_discharged_invariant_from_an_assumed_one(
    tmp_path: Path,
) -> None:
    public = _unit("rb_len", (Param("self", "const ring_t *"),), internal=False)
    results = _run(tmp_path, Port(), units=(INIT, PUSH, public, MAIN))
    text = invariant_discharge_transcript(InvariantDischargeRun("x.c::ring_t", results))
    assert text.startswith("Forseti invariant discharge — x.c::ring_t")
    assert f"Invariant {results[0].property_id}: {INVARIANT}" in text
    assert "  rb_push(): DISCHARGED (VERIFIED up to k=4)" in text
    assert "  rb_len(): ASSUMED (not discharged)" in text
    assert "    main() [discharged k=4]:" in text
    assert "Anything not DISCHARGED stays assumed." in text
    assert "proof" not in text.lower()


def test_transcript_of_an_empty_run_says_nothing_was_discharged() -> None:
    text = invariant_discharge_transcript(InvariantDischargeRun("x.c::ring_t", ()))
    assert "Nothing was discharged" in text
    assert "stays assumed" in text
