"""Behavioural tests for the `check_properties` driver — no esbmc, no network.

Every seam is faked: an `InMemoryPropertyStore` yields stored `Property` objects,
a `FakeHarnessWriter` returns placeholder text, and a scripted `FakeVerify`
replays verdicts. This exercises the driver's control flow — verdict mapping,
the UNKNOWN ladder, the reachability skip, telemetry, and the JSON/transcript
projections — in isolation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forseti.esbmc import (
    Counterexample,
    Error,
    EsbmcResult,
    RunMeta,
    SourceLoc,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    ViolatedProperty,
)
from forseti.orchestrator import (
    ListSink,
    PropertyCheckRun,
    PropertyOutcome,
    RenderedHarness,
    Unit,
    check_properties,
    persist_property_check,
    property_check_transcript,
)
from forseti.properties import Property, PropertyKind, PropertyStatus, Provenance

UNIT = Unit("u.c::f", Path("u.c"), "f", "int f(int x) { return x; }")


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "harness.c", "--unwind", "8"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def cex() -> Counterexample:
    loc = SourceLoc("u.c", 1, 20, "f")
    return Counterexample(
        steps=(),
        violated_property=ViolatedProperty(loc, "assertion", "result >= 0", ()),
    )


def violated() -> Violated:
    return Violated(meta(), "[Counterexample]\n", cex())


def unknown() -> Unknown:
    return Unknown(meta(), UnknownReason.TIMEOUT)


def semantic_prop(
    property_id: str,
    *,
    unit_id: str = UNIT.unit_id,
    expression: str = "result >= 0",
    domain: tuple[str, ...] = (),
) -> Property:
    return Property(
        property_id=property_id,
        unit_id=unit_id,
        kind=PropertyKind.SEMANTIC,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
        domain=domain,
    )


def reachability_prop(property_id: str, *, unit_id: str = UNIT.unit_id) -> Property:
    return Property(
        property_id=property_id,
        unit_id=unit_id,
        kind=PropertyKind.REACHABILITY,
        expression="reach_label",
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
    )


class InMemoryPropertyStore:
    """A PropertyStorePort backed by a list, filtering by unit (like the real WHERE)."""

    def __init__(self, props: Sequence[Property]) -> None:
        self._props = list(props)

    def list_for_unit(self, unit_id: str) -> Sequence[Property]:
        return tuple(p for p in self._props if p.unit_id == unit_id)


class FakeHarnessWriter:
    """A HarnessWriterPort returning placeholder text; counts renders."""

    def __init__(self) -> None:
        self.calls = 0

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        self.calls += 1
        return RenderedHarness(source_text=f"/* harness {prop.property_id} */")


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts, recording bounds."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.unwinds: list[int] = []

    @property
    def calls(self) -> int:
        return len(self.unwinds)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.unwinds.append(unwind)
        return self._results.pop(0)


def test_all_held(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1"), semantic_prop("p2")])
    render = FakeHarnessWriter()
    verify = FakeVerify([Verified(meta()), Verified(meta())])
    sink = ListSink()

    run = check_properties(
        UNIT,
        store=store,
        render=render,
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=8,
        sink=sink,
    )

    assert [v.outcome for v in run.verdicts] == [
        PropertyOutcome.HELD,
        PropertyOutcome.HELD,
    ]
    assert run.counts() == {
        "held": 2,
        "violated": 0,
        "unknown": 0,
        "error": 0,
        "skipped": 0,
    }
    assert render.calls == 2
    # a harness file was materialized per property (the file esbmc reads).
    assert len(list((tmp_path / "work").glob("*.c"))) == 2

    loaded = [e for e in sink.events if e.type == "properties.loaded"]
    assert loaded[0].detail == {"unit": "u.c::f", "count": 2}
    verdict_events = [e for e in sink.events if e.type == "property.verdict"]
    assert [e.verdict for e in verdict_events] == ["held", "held"]
    checked = [e for e in sink.events if e.type == "properties.checked"]
    assert checked[0].detail["held"] == 2


def test_mixed_outcomes_and_ladder_restarts(tmp_path: Path) -> None:
    store = InMemoryPropertyStore(
        [semantic_prop("p1"), semantic_prop("p2"), semantic_prop("p3")]
    )
    verify = FakeVerify(
        # p1 -> Verified@8; p2 -> Violated@8; p3 -> Unknown@8 escalate Verified@16
        [Verified(meta()), violated(), unknown(), Verified(meta())]
    )
    sink = ListSink()

    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=8,
        unwind_ladder=(16,),
        sink=sink,
    )

    assert [v.outcome for v in run.verdicts] == [
        PropertyOutcome.HELD,
        PropertyOutcome.VIOLATED,
        PropertyOutcome.HELD,
    ]
    # k restarts at the base bound for every property, then escalates on its own.
    assert verify.unwinds == [8, 8, 8, 16]
    assert [v.k for v in run.verdicts] == [8, 8, 16]

    escalations = [e for e in sink.events if e.type == "unknown.policy.decision"]
    assert len(escalations) == 1
    assert escalations[0].detail == {
        "decision": "escalate",
        "property_id": "p3",
        "from_k": 8,
        "to_k": 16,
    }


def test_violated_verdict_carries_typed_cex_and_round_trips(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1")])
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([violated()]),
        work_dir=tmp_path / "work",
        unwind=8,
    )
    payload = run.verdicts[0].to_dict()
    assert payload["outcome"] == "violated"
    assert payload["result"]["counterexample"] is not None
    assert payload["result"]["raw_counterexample"] == "[Counterexample]\n"
    assert payload["harness_source"] == "/* harness p1 */"
    # the whole run must be JSON-serializable — the grading (#4) contract.
    assert json.loads(json.dumps(run.to_dict()))["counts"]["violated"] == 1


def test_unknown_exhausts_ladder_is_never_held(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1")])
    verify = FakeVerify([unknown(), unknown()])
    sink = ListSink()
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=8,
        unwind_ladder=(16,),
        sink=sink,
    )
    assert run.verdicts[0].outcome is PropertyOutcome.UNKNOWN
    assert run.verdicts[0].k == 16  # settled at the exhausted rung
    verdict_events = [e for e in sink.events if e.type == "property.verdict"]
    assert verdict_events[0].verdict == "unknown"


def test_esbmc_error_is_surfaced(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1")])
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([Error(meta(), "esbmc: parse error")]),
        work_dir=tmp_path / "work",
        unwind=8,
    )
    assert run.verdicts[0].outcome is PropertyOutcome.ERROR
    assert run.verdicts[0].to_dict()["result"]["message"] == "esbmc: parse error"


def test_reachability_property_is_skipped(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([reachability_prop("r1")])
    render = FakeHarnessWriter()
    verify = FakeVerify([])  # must never be called
    sink = ListSink()

    run = check_properties(
        UNIT,
        store=store,
        render=render,
        verify=verify,
        work_dir=tmp_path / "work",
        unwind=8,
        sink=sink,
    )

    verdict = run.verdicts[0]
    assert verdict.outcome is PropertyOutcome.SKIPPED
    assert verdict.k is None
    assert verdict.result is None
    assert verdict.skip_reason is not None
    assert render.calls == 0  # reachability is never rendered
    assert verify.calls == 0  # nor verified
    assert any(e.type == "property.skipped" for e in sink.events)


def test_empty_store_is_an_honest_no_op(tmp_path: Path) -> None:
    sink = ListSink()
    run = check_properties(
        UNIT,
        store=InMemoryPropertyStore([]),
        render=FakeHarnessWriter(),
        verify=FakeVerify([]),
        work_dir=tmp_path / "work",
        unwind=8,
        sink=sink,
    )
    assert run.verdicts == ()
    assert run.counts() == {
        "held": 0,
        "violated": 0,
        "unknown": 0,
        "error": 0,
        "skipped": 0,
    }
    loaded = [e for e in sink.events if e.type == "properties.loaded"]
    assert loaded[0].detail == {"unit": "u.c::f", "count": 0}


def test_store_filters_foreign_units(tmp_path: Path) -> None:
    # A property keyed to a different unit is not returned for this unit.
    store = InMemoryPropertyStore(
        [semantic_prop("p1"), semantic_prop("other", unit_id="v.c::g")]
    )
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([Verified(meta())]),
        work_dir=tmp_path / "work",
        unwind=8,
    )
    assert [v.property_id for v in run.verdicts] == ["p1"]


def test_invalid_ladder_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        check_properties(
            UNIT,
            store=InMemoryPropertyStore([semantic_prop("p1")]),
            render=FakeHarnessWriter(),
            verify=FakeVerify([Verified(meta())]),
            work_dir=tmp_path / "work",
            unwind=8,
            unwind_ladder=(4,),  # not increasing over the base bound
        )


def test_held_subset_helper(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1"), semantic_prop("p2")])
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([Verified(meta()), violated()]),
        work_dir=tmp_path / "work",
        unwind=8,
    )
    held = run.held()
    assert [v.property_id for v in held] == ["p1"]


def test_transcript_shows_every_outcome(tmp_path: Path) -> None:
    store = InMemoryPropertyStore(
        [semantic_prop("p1"), semantic_prop("p2"), reachability_prop("r1")]
    )
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([Verified(meta()), violated()]),
        work_dir=tmp_path / "work",
        unwind=8,
    )
    text = property_check_transcript(run)
    assert "u.c::f" in text
    assert "HELD" in text
    assert "VIOLATED" in text
    assert "SKIPPED" in text
    assert "Counts: held=1, violated=1, unknown=0, error=0, skipped=1" in text


def test_persist_property_check_writes_jsonl(tmp_path: Path) -> None:
    store = InMemoryPropertyStore([semantic_prop("p1")])
    sink = ListSink()
    run = check_properties(
        UNIT,
        store=store,
        render=FakeHarnessWriter(),
        verify=FakeVerify([Verified(meta())]),
        work_dir=tmp_path / "work",
        unwind=8,
        sink=sink,
    )
    dest = persist_property_check(run, events=sink.events, root=tmp_path / ".forseti")
    assert dest.parent.name == "property-checks"
    record = json.loads(dest.read_text().strip())
    assert record["unit"] == "u.c::f"
    assert record["run"]["counts"]["held"] == 1
    assert len(record["events"]) == len(sink.events)


if TYPE_CHECKING:
    # mypy-only structural guards: fail type-checking if a concrete class drifts
    # from the port it must satisfy (mirrors the fix.py / test_loop.py pattern).
    from forseti.esbmc import verify as _real_verify
    from forseti.orchestrator import (
        HarnessWriterPort,
        PropertyStorePort,
        SemanticHarnessWriter,
        VerifyPort,
    )
    from forseti.properties import PropertyStore

    _real_verify_is_port: VerifyPort = _real_verify

    def _property_store_is_port(s: PropertyStore) -> PropertyStorePort:
        return s

    def _inmem_is_port(s: InMemoryPropertyStore) -> PropertyStorePort:
        return s

    def _semantic_writer_is_port(w: SemanticHarnessWriter) -> HarnessWriterPort:
        return w

    def _fake_writer_is_port(w: FakeHarnessWriter) -> HarnessWriterPort:
        return w

    def _run_is_check_run(r: PropertyCheckRun) -> PropertyCheckRun:
        return r
