"""The representation-invariant seams on the `check_properties` driver (#387).

Real `PropertyStore` (in memory) and the real `InvariantHarnessWriter`; only ESBMC is
scripted, so the verdict mapping, the non-vacuity probe, the skip/error split and
the *assumed* labelling are exercised without a binary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forseti.esbmc import EsbmcResult, Verified, Violated
from forseti.esbmc.result import RunMeta
from forseti.orchestrator import (
    InvariantHarnessWriter,
    InvariantsOfType,
    PropertyCheckRun,
    PropertyOutcome,
    SemanticHarnessWriter,
    Unit,
    check_properties,
    property_check_transcript,
)
from forseti.precond.reachability import ProbeReachability
from forseti.properties import (
    INVARIANT_ASSUMPTION_NOTE,
    REP_INVARIANT_LABEL,
    HarnessError,
    InvariantRole,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    make_invariant,
    make_property_id,
)

SOURCE = """\
#define CAP 4
typedef struct { unsigned head, tail, count; } ring_t;
void rb_init(ring_t *r) { r->head = r->tail = r->count = 0; }
int rb_push(ring_t *r, int v) { return 1; }
unsigned rb_count(const ring_t *r) { return r->count; }
int rb_cap(int x) { return x; }
"""
TYPE_KEY = "r.c::ring_t"
INV = "self->count <= CAP"


def _meta() -> RunMeta:
    return RunMeta("8.6.0", ("esbmc",), 0, 0.0, "", "")


class Scripted:
    """Replays a verdict for the harness and another for its probe twin."""

    def __init__(self, harness: EsbmcResult, probe: EsbmcResult) -> None:
        self._harness, self._probe = harness, probe
        self.sources: list[str] = []

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        self.sources.append(source.read_text())
        return self._probe if source.name.endswith(".probe.c") else self._harness


def reached() -> Violated:
    return Violated(_meta(), "Violated property:\n  forseti:non-vacuity", None)


def exit_assert_failed() -> Violated:
    return Violated(_meta(), f"Violated property:\n  {REP_INVARIANT_LABEL}", None)


def _store(*expressions: str) -> PropertyStore:
    store = PropertyStore(sqlite3.connect(":memory:"))
    for expression in expressions:
        store.add_type_invariant(
            make_invariant(
                type_key=TYPE_KEY,
                expression=expression,
                provenance=Provenance("host-submitted", "1", "hand", "human"),
            )
        )
    return store


def _check(
    symbol: str,
    verify: Scripted,
    tmp_path: Path,
    *expressions: str,
    role: InvariantRole | None = None,
) -> PropertyCheckRun:
    unit = Unit(f"r.c::{symbol}", Path("r.c"), symbol, SOURCE)
    return check_properties(
        unit,
        store=InvariantsOfType(_store(*(expressions or (INV,))), TYPE_KEY),
        render=InvariantHarnessWriter(role=role),
        verify=verify,
        work_dir=tmp_path,
        unwind=4,
    )


def test_held_needs_a_reached_probe_and_is_labelled_assumed(tmp_path: Path) -> None:
    verify = Scripted(Verified(_meta()), reached())
    run = _check("rb_push", verify, tmp_path)
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.probe is ProbeReachability.REACHED
    assert verdict.kind == "rep_invariant"
    assert verdict.unit_id == "r.c::rb_push"
    assert verdict.to_dict()["assumption"] == INVARIANT_ASSUMPTION_NOTE
    assert run.outcome == "held"
    # the harness checked is the invariant harness, not a semantic one
    assert REP_INVARIANT_LABEL in verify.sources[0]
    assert "forseti:non-vacuity" in verify.sources[1]


def test_a_vacuous_invariant_is_never_held(tmp_path: Path) -> None:
    verify = Scripted(Verified(_meta()), Verified(_meta()))
    run = _check("rb_push", verify, tmp_path, "0")
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.VACUOUS
    assert verdict.probe is ProbeReachability.UNREACHABLE
    assert verdict.skip_reason is not None
    assert "vacuous" in verdict.skip_reason
    assert run.outcome == "unknown"
    assert "__ESBMC_assume(__forseti_inv(&r))" in verify.sources[0]
    assert "return (0);" in verify.sources[0]


def test_a_violated_exit_assertion_is_recorded_as_violated(tmp_path: Path) -> None:
    verify = Scripted(exit_assert_failed(), reached())
    run = _check("rb_push", verify, tmp_path)
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.VIOLATED
    assert verdict.probe is None
    assert isinstance(verdict.result, Violated)
    assert REP_INVARIANT_LABEL in verdict.result.raw_counterexample


def test_initialiser_role_is_inferred_and_overridable(tmp_path: Path) -> None:
    verify = Scripted(Verified(_meta()), reached())
    _check("rb_init", verify, tmp_path)
    assert "__ESBMC_assume" not in verify.sources[0]
    forced = Scripted(Verified(_meta()), reached())
    _check("rb_init", forced, tmp_path, role=InvariantRole.METHOD)
    assert "__ESBMC_assume(__forseti_inv(&r))" in forced.sources[0]


def test_const_only_method_is_skipped_with_a_reason_and_never_verified(
    tmp_path: Path,
) -> None:
    verify = Scripted(Verified(_meta()), reached())
    run = _check("rb_count", verify, tmp_path)
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.SKIPPED
    assert verdict.skip_reason is not None
    assert "const ring_t" in verdict.skip_reason
    assert verify.sources == []
    assert run.outcome == "empty"
    # nothing was assumed or checked, so neither the payload nor the transcript says so
    assert "assumption" not in verdict.to_dict()
    assert "Assumed:" not in property_check_transcript(run)


def test_unrenderable_function_is_an_error_not_a_skip(tmp_path: Path) -> None:
    verify = Scripted(Verified(_meta()), reached())
    unit = Unit("r.c::missing", Path("r.c"), "missing", SOURCE)
    run = check_properties(
        unit,
        store=InvariantsOfType(_store(INV), TYPE_KEY),
        render=InvariantHarnessWriter(),
        verify=verify,
        work_dir=tmp_path,
        unwind=4,
    )
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.ERROR
    assert "assumption" not in verdict.to_dict()
    assert verify.sources == []


def test_every_stored_invariant_of_the_type_is_checked_in_order(tmp_path: Path) -> None:
    run = _check(
        "rb_push",
        Scripted(Verified(_meta()), reached()),
        tmp_path,
        "self->count <= CAP",
        "self->tail < CAP",
    )
    assert [v.outcome for v in run.verdicts] == [PropertyOutcome.HELD] * 2
    assert len({v.property_id for v in run.verdicts}) == 2


def test_terminal_invariants_are_not_rechecked(tmp_path: Path) -> None:
    store = _store()
    store.add_type_invariant(
        Property(
            property_id=make_property_id(TYPE_KEY, PropertyKind.REP_INVARIANT, INV),
            unit_id=TYPE_KEY,
            kind=PropertyKind.REP_INVARIANT,
            expression=INV,
            status=PropertyStatus.REJECTED,
            provenance=Provenance("p", "1", "hand", "human"),
        )
    )
    unit = Unit("r.c::rb_push", Path("r.c"), "rb_push", SOURCE)
    run = check_properties(
        unit,
        store=InvariantsOfType(store, TYPE_KEY),
        render=InvariantHarnessWriter(),
        verify=Scripted(Verified(_meta()), reached()),
        work_dir=tmp_path,
        unwind=4,
    )
    assert run.verdicts == ()


def test_transcript_states_the_invariant_is_assumed(tmp_path: Path) -> None:
    run = _check("rb_push", Scripted(Verified(_meta()), reached()), tmp_path)
    transcript = property_check_transcript(run)
    assert "rep_invariant" in transcript
    assert f"Assumed: {INVARIANT_ASSUMPTION_NOTE}" in transcript


def _semantic_run(symbol: str, tmp_path: Path) -> PropertyCheckRun:
    store = PropertyStore(sqlite3.connect(":memory:"))
    unit = Unit("r.c::" + symbol, Path("r.c"), symbol, SOURCE)
    store.add(
        Property(
            property_id="sem1",
            unit_id=unit.unit_id,
            kind=PropertyKind.SEMANTIC,
            expression="result >= 0",
            status=PropertyStatus.CANDIDATE,
            provenance=Provenance("t", "1"),
        )
    )
    return check_properties(
        unit,
        store=store,
        render=SemanticHarnessWriter(),
        verify=Scripted(Verified(_meta()), reached()),
        work_dir=tmp_path,
        unwind=4,
    )


def test_scalar_semantic_verdicts_carry_no_assumption_field(tmp_path: Path) -> None:
    run = _semantic_run("rb_cap", tmp_path)
    assert "assumption" not in run.verdicts[0].to_dict()
    assert "Assumed:" not in property_check_transcript(run)


def test_struct_semantic_verdicts_assume_a_nondet_struct_not_the_invariant(
    tmp_path: Path,
) -> None:
    run = _semantic_run("rb_count", tmp_path)
    assumption = run.verdicts[0].to_dict()["assumption"]
    assert "r (ring_t)" in assumption and "nondet" in assumption
    assert assumption != INVARIANT_ASSUMPTION_NOTE
    assert f"Assumed: {assumption}" in property_check_transcript(run)


def test_writers_refuse_each_others_kinds(tmp_path: Path) -> None:
    unit = Unit("r.c::rb_push", Path("r.c"), "rb_push", SOURCE)
    run = check_properties(
        unit,
        store=InvariantsOfType(_store(INV), TYPE_KEY),
        render=SemanticHarnessWriter(),
        verify=Scripted(Verified(_meta()), reached()),
        work_dir=tmp_path,
        unwind=4,
    )
    assert run.verdicts[0].outcome is PropertyOutcome.ERROR
    semantic = Property(
        "s",
        unit.unit_id,
        PropertyKind.SEMANTIC,
        "result",
        PropertyStatus.CANDIDATE,
        Provenance("t", "1"),
    )
    with pytest.raises(HarnessError, match="semantic"):
        InvariantHarnessWriter().render(unit, semantic)


def _stored(expression: str, status: PropertyStatus) -> Property:
    return Property(
        property_id=make_property_id(TYPE_KEY, PropertyKind.REP_INVARIANT, expression),
        unit_id=TYPE_KEY,
        kind=PropertyKind.REP_INVARIANT,
        expression=expression,
        status=status,
        provenance=Provenance("p", "1", "hand", "human"),
    )


def test_an_accepted_invariant_is_still_checked_a_rejected_one_is(
    tmp_path: Path,
) -> None:
    store = _store()
    kept = _stored("self->count <= CAP", PropertyStatus.ACCEPTED)
    dropped = _stored("self->tail < CAP", PropertyStatus.REJECTED)
    store.add_type_invariant(kept)
    store.add_type_invariant(dropped)
    unit = Unit("r.c::rb_push", Path("r.c"), "rb_push", SOURCE)
    run = check_properties(
        unit,
        store=InvariantsOfType(store, TYPE_KEY),
        render=InvariantHarnessWriter(),
        verify=Scripted(Verified(_meta()), reached()),
        work_dir=tmp_path,
        unwind=4,
    )
    assert [v.property_id for v in run.verdicts] == [kept.property_id]
    assert run.verdicts[0].outcome is PropertyOutcome.HELD


def test_the_invariant_view_honours_an_empty_status_scope() -> None:
    store = _store(INV)
    view = InvariantsOfType(store, TYPE_KEY)
    assert view.list_for_unit("r.c::rb_push", ()) == ()
    assert len(view.list_for_unit("r.c::rb_push")) == 1
