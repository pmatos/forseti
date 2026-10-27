"""End-to-end: check store-sourced properties on the real my_abs kernel with esbmc.

The #66 acceptance tracer bullet. Two semantic properties are read from a real
`PropertyStore`, rendered by the real #64 `SemanticHarnessWriter`, and verified
by the real `forseti.esbmc.verify` — with **no** hand-written property in the
checked path (the fixture is a main-free kernel slice; the property comes from
the store, the assert from the synthesized harness). Skipped automatically when
esbmc is not on PATH, mirroring the other integration suites.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify
from forseti.orchestrator import (
    ListSink,
    PropertyOutcome,
    PropertyVerdict,
    SemanticHarnessWriter,
    Unit,
    check_properties,
    persist_property_check,
    property_check_transcript,
)
from forseti.precond.reachability import ProbeReachability
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    make_property_id,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# INT64_MIN as esbmc's two's-complement bit string (top bit set, 63 zeros) — the
# counterexample input pinned by bit pattern, not the decimal literal esbmc 8.3.0
# renders as "-9223372036854775807 - 1" (same pin as the esbmc/harness suites).
_INT64_MIN_BITS = "1" + "0" * 63


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


def test_abs_store_sourced_properties_end_to_end(tmp_path: Path) -> None:
    unit = Unit.from_path(FIXTURES / "abs_unit.c", "my_abs")

    # Two store-sourced semantic properties over the SAME unit:
    #   P1: result >= 0, no precondition           -> VIOLATED (my_abs(INT64_MIN) < 0)
    #   P2: result >= 0 given x > INT64_MIN        -> HELD (non-vacuous precondition)
    p1 = _semantic(unit.unit_id, "result >= 0")
    p2 = _semantic(unit.unit_id, "result >= 0", ("x > INT64_MIN",))

    store = PropertyStore.open(tmp_path / ".forseti")
    store.add(p1)
    store.add(p2)

    sink = ListSink()
    run = check_properties(
        unit,
        store=store,
        render=SemanticHarnessWriter(),
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=1,
        sink=sink,
    )
    store.close()

    by_id = {v.property_id: v for v in run.verdicts}

    # Acceptance 1: store-sourced properties checked end-to-end, one each way.
    assert by_id[p1.property_id].outcome is PropertyOutcome.VIOLATED
    assert by_id[p2.property_id].outcome is PropertyOutcome.HELD
    assert run.counts()["held"] == 1
    assert run.counts()["violated"] == 1

    # The VIOLATED verdict carries the INT64_MIN counterexample (bit-pinned).
    violated = by_id[p1.property_id].result
    assert isinstance(violated, Violated)
    cex = violated.counterexample
    assert cex is not None
    assert any(
        a.binary is not None and a.binary.replace(" ", "") == _INT64_MIN_BITS
        for a in cex.inputs
    )
    assert isinstance(by_id[p2.property_id].result, Verified)

    # Acceptance 2: each verdict is persisted and visible in the transcript.
    dest = persist_property_check(run, events=sink.events, root=tmp_path / ".forseti")
    assert dest.parent.name == "property-checks"
    assert dest.exists()
    text = property_check_transcript(run)
    assert "HELD" in text
    assert "VIOLATED" in text


# --- W3.4: the post-call vacuity probe, against real esbmc ---------------------

_LOOP_SLICE = """\
int count_to_ten(int x) {
    int i = 0;
    while (i < 10) {
        i++;
    }
    return i + (x - x);
}
"""


def _check_with_real_esbmc(
    unit: Unit, prop: Property, tmp_path: Path, *, unwind: int
) -> PropertyVerdict:
    store = PropertyStore.open(tmp_path / ".forseti")
    store.add(prop)
    run = check_properties(
        unit,
        store=store,
        render=SemanticHarnessWriter(),
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=unwind,
    )
    store.close()
    (verdict,) = run.verdicts
    return verdict


def test_unsatisfiable_domain_is_vacuous_not_held(tmp_path: Path) -> None:
    unit = Unit.from_path(FIXTURES / "abs_unit.c", "my_abs")
    prop = _semantic(unit.unit_id, "result >= 0", ("x > 0 && x < 0",))

    verdict = _check_with_real_esbmc(unit, prop, tmp_path, unwind=1)

    assert isinstance(verdict.result, Verified)  # ESBMC alone would say HELD
    assert verdict.outcome is PropertyOutcome.VACUOUS
    assert verdict.probe is ProbeReachability.UNREACHABLE


def test_vacuous_even_for_a_postcondition_that_would_be_violated(
    tmp_path: Path,
) -> None:
    # The domain is empty, so no input exists that could violate this either:
    # the verdict must not depend on the (false) postcondition.
    unit = Unit.from_path(FIXTURES / "abs_unit.c", "my_abs")
    prop = _semantic(unit.unit_id, "result < 0", ("x > 0 && x < 0",))

    verdict = _check_with_real_esbmc(unit, prop, tmp_path, unwind=1)

    assert verdict.outcome is PropertyOutcome.VACUOUS


def test_loop_needing_more_than_k_iterations_is_vacuous_at_that_k(
    tmp_path: Path,
) -> None:
    # `forseti.esbmc.verify` cuts paths past k (--no-unwinding-assertions): every
    # path through the 10-iteration loop is cut at k=2, so the postcondition is
    # never evaluated and the bare verdict is a (false) VERIFIED.
    unit = Unit("u.c::count_to_ten", Path("u.c"), "count_to_ten", _LOOP_SLICE)
    prop = _semantic(unit.unit_id, "result == 10")

    verdict = _check_with_real_esbmc(unit, prop, tmp_path, unwind=2)

    assert isinstance(verdict.result, Verified)
    assert verdict.outcome is PropertyOutcome.VACUOUS
    assert verdict.k == 2
    assert verdict.skip_reason is not None
    assert "k=2" in verdict.skip_reason


def test_same_loop_is_held_once_k_covers_its_iterations(tmp_path: Path) -> None:
    unit = Unit("u.c::count_to_ten", Path("u.c"), "count_to_ten", _LOOP_SLICE)
    prop = _semantic(unit.unit_id, "result == 10")

    verdict = _check_with_real_esbmc(unit, prop, tmp_path, unwind=11)

    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.probe is ProbeReachability.REACHED


def test_a_falsified_property_is_still_violated_not_probed(tmp_path: Path) -> None:
    unit = Unit.from_path(FIXTURES / "abs_unit.c", "my_abs")
    prop = _semantic(unit.unit_id, "result >= 0")

    verdict = _check_with_real_esbmc(unit, prop, tmp_path, unwind=1)

    assert verdict.outcome is PropertyOutcome.VIOLATED
    assert verdict.probe is None
