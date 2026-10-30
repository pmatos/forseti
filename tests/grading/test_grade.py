"""The kill-rate scorer with every port faked: no esbmc, no network.

The mutation source, the equivalence checker, the harness writer and the verifier
are scripted, against a real in-memory `PropertyStore`, so these tests pin the
scorer's own rules: the candidate set, the outcome mapping, the rate and its
denominator, the reason, the transactional store write, the JSON and trace shapes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from forseti.esbmc import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.grading import grade_trace_rows, grade_transcript
from forseti.grading.equivalence import (
    EquivalenceRefused,
    EquivalenceUnknown,
    EquivalenceVerified,
)
from forseti.grading.equivalence import UnknownReason as EquivalenceUnknownReason
from forseti.grading.grade import (
    BOUNDED_LIMITATION,
    EQUIVALENCE_BIAS,
    MUTANT_EVENT,
    PROPERTY_EVENT,
    RUN_EVENT,
    GradeOutcome,
    GradeRun,
    MutantOutcome,
    MutantReading,
    grade_unit,
    kill_reason,
    mutant_reading,
    record_trace,
)
from forseti.grading.mutate import Mutant, MutantId, MutationResult
from forseti.orchestrator import PropertyOutcome, PropertyVerdict, RenderedHarness, Unit
from forseti.properties import (
    Grading,
    GradingVerdict,
    HarnessError,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
)

SEED_SOURCE = "int f(int x) { /*orig*/ return x; }\nint main(void) { return f(0); }\n"
STEP = 0.5  # the ESBMC seconds every scripted run reports


def meta() -> RunMeta:
    return RunMeta("8.5.0", ("esbmc",), 0, STEP, "", "")


def violated() -> Violated:
    return Violated(meta(), "[Counterexample]", None)


def reached_probe() -> Violated:
    return Violated(meta(), "Violated property:\n  forseti:non-vacuity", None)


def make_mutant(
    index: int, description: str, operator: str = "relational-flip"
) -> Mutant:
    return Mutant(
        MutantId(operator, 10 + index, 5, 0),
        SEED_SOURCE.replace("/*orig*/", f"/*m{index}*/"),
        description,
        (),
    )


def make_prop(name: str, status: PropertyStatus = PropertyStatus.CANDIDATE) -> Property:
    return Property(
        name,
        "placeholder",
        PropertyKind.SEMANTIC,
        f"result >= {name}",
        status,
        Provenance("t", "1"),
        grading=(
            Grading(GradingVerdict.HELD, 0.5, "earlier")
            if status is PropertyStatus.GRADED
            else None
        ),
    )


class FakeVerify:
    """Scripted by (marker, property id); a probe reads as reached unless told."""

    def __init__(
        self,
        verdicts: Mapping[tuple[str, str], EsbmcResult],
        unreachable: Collection[tuple[str, str]] = (),
    ) -> None:
        self.verdicts = verdicts
        self.unreachable = set(unreachable)
        self.seen: list[tuple[str, str]] = []

    def __call__(self, source: Path, /) -> Any:
        def port(path: Path, /, *, unwind: int) -> EsbmcResult:
            text = path.read_text()
            marker = text.split("/*")[1].split("*/")[0]
            name = path.name.removesuffix(".probe.c").removesuffix(".c")
            key = (marker, name.split("__")[-1])
            self.seen.append(key)
            if path.name.endswith(".probe.c"):
                return Verified(meta()) if key in self.unreachable else reached_probe()
            return self.verdicts.get(key, Verified(meta()))

        return port


class FakeWriter:
    def __init__(self, fail_on: Collection[str] = ()) -> None:
        self.fail_on = set(fail_on)

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        marker = unit.source_text.split("/*")[1].split("*/")[0]
        if marker in self.fail_on:
            raise HarnessError("not renderable")
        text = f"/*{marker}*/"
        return RenderedHarness(text, probe_source_text=text)


class FakeEquivalence:
    """Equivalent for the listed markers; everything else is undecided. A source
    compared with itself is always verified, as the real filter's baseline needs."""

    def __init__(
        self,
        equivalent: Collection[str] = (),
        refuse: str | None = None,
        assumptions: tuple[str, ...] = (),
    ) -> None:
        self.equivalent = set(equivalent)
        self.refuse = refuse
        self.assumptions = assumptions
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        candidate = kwargs["candidate"]
        if self.refuse is not None and candidate == kwargs["reference"]:
            return EquivalenceRefused(self.refuse)
        k = kwargs["k"]
        marker = candidate.split("/*")[1].split("*/")[0]
        if candidate == kwargs["reference"] or marker in self.equivalent:
            return EquivalenceVerified(k, (), meta(), self.assumptions)
        return EquivalenceUnknown(k, (), EquivalenceUnknownReason.TIMEOUT, "t", meta())


class FakeMutants:
    def __init__(self, mutants: Sequence[Mutant], dropped: int = 0) -> None:
        self.result = MutationResult(tuple(mutants), dropped)
        self.calls = 0

    def __call__(self, source: Path, symbol: str, /) -> MutationResult:
        self.calls += 1
        return self.result


def store_with(source: Path, props: Sequence[Property]) -> PropertyStore:
    store = PropertyStore(sqlite3.connect(":memory:"))
    unit_id = Unit.from_path(source, "f").unit_id
    for prop in props:
        store.add(
            Property(
                prop.property_id,
                unit_id,
                prop.kind,
                prop.expression,
                prop.status,
                prop.provenance,
                grading=prop.grading,
            )
        )
    return store


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "u.c"
    path.write_text(SEED_SOURCE)
    return path


def run_grade(
    source: Path,
    store: PropertyStore,
    tmp_path: Path,
    *,
    mutants: Sequence[Mutant],
    verify: FakeVerify | None = None,
    equivalence: FakeEquivalence | None = None,
    **kwargs: Any,
) -> GradeRun:
    return grade_unit(
        source,
        "f",
        store=store,
        work_dir=tmp_path / "work",
        mutants=FakeMutants(mutants),
        equivalence=equivalence or FakeEquivalence(),
        verify_for=(verify or FakeVerify({})),
        render=kwargs.pop("render", FakeWriter()),
        **kwargs,
    )


def unit_props(store: PropertyStore, source: Path) -> dict[str, Property]:
    unit_id = Unit.from_path(source, "f").unit_id
    return {p.property_id: p for p in store.list_for_unit(unit_id)}


def test_grades_held_properties_and_stores_each_grading(
    source: Path, tmp_path: Path
) -> None:
    m1 = make_mutant(1, "`<` -> `<=` at line 11")
    m2 = make_mutant(2, "drop guard `if (x) return 0;` at line 12", "guard-drop")
    m3 = make_mutant(3, "`+` -> `-` at line 13", "arith-swap")
    m4 = make_mutant(4, "`1` -> `2` at line 14", "const-off-by-one")
    m5 = make_mutant(5, "havoc returned value at line 15", "output-havoc")
    store = store_with(
        source,
        [
            make_prop("p1"),
            make_prop("p2"),
            make_prop("bad"),
            make_prop("old", PropertyStatus.REJECTED),
        ],
    )
    verify = FakeVerify(
        {
            ("orig", "bad"): violated(),
            ("m1", "p1"): violated(),
            ("m1", "p2"): violated(),
            ("m2", "p2"): violated(),
            ("m4", "p1"): Unknown(meta(), UnknownReason.TIMEOUT),
            ("m5", "p2"): Error(meta(), "esbmc crashed"),
        }
    )

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[m1, m2, m3, m4, m5],
        verify=verify,
        equivalence=FakeEquivalence(equivalent={"m3"}),
    )

    by_id = {g.prop.property_id: g for g in got.properties}
    p1, p2 = by_id["p1"], by_id["p2"]
    assert p1.outcome is p2.outcome is GradeOutcome.GRADED
    assert p1.counts() == {
        "killed": 1, "survived": 2, "unknown": 1, "error": 0, "skipped": 1,
    }  # fmt: skip
    assert p1.kill_rate == pytest.approx(1 / 3)
    assert p2.counts() == {
        "killed": 2, "survived": 1, "unknown": 0, "error": 1, "skipped": 1,
    }  # fmt: skip
    assert p2.kill_rate == pytest.approx(2 / 3)
    assert got.outcome == "graded"
    assert by_id["bad"].outcome is GradeOutcome.NOT_HELD
    assert by_id["old"].outcome is GradeOutcome.NOT_CANDIDATE
    assert "rejected" in by_id["old"].reason

    stored = unit_props(store, source)
    for name, grade in (("p1", p1), ("p2", p2)):
        assert stored[name].status is PropertyStatus.GRADED
        stored_grading = stored[name].grading
        assert stored_grading == grade.grading
        assert stored_grading is not None
        assert stored_grading.verdict is GradingVerdict.HELD
    assert stored["p1"].grading is not None
    assert stored["p1"].grading.kill_rate == pytest.approx(1 / 3)
    assert stored["bad"].status is PropertyStatus.CANDIDATE
    assert stored["bad"].grading is None
    assert stored["old"].status is PropertyStatus.REJECTED
    gradings = store._conn.execute(
        "SELECT grading_kill_rate FROM properties WHERE status = 'graded'"
    ).fetchall()
    assert len(gradings) == 2
    assert all(row[0] is not None for row in gradings)


def test_the_reason_names_each_survivor_and_what_was_left_out(
    source: Path, tmp_path: Path
) -> None:
    mutants = [
        make_mutant(1, "`<` -> `<=` at line 11"),
        make_mutant(2, "drop guard `if (x) return 0;` at line 12", "guard-drop"),
        make_mutant(3, "`+` -> `-` at line 13", "arith-swap"),
        make_mutant(4, "`1` -> `2` at line 14", "const-off-by-one"),
    ]
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify(
        {
            ("m3", "p1"): violated(),
            ("m4", "p1"): Unknown(meta(), UnknownReason.TIMEOUT),
        }
    )

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=mutants,
        verify=verify,
        equivalence=FakeEquivalence(equivalent={"m3"}),
    )

    (grade,) = got.properties
    assert grade.reason == (
        "killed 0/2; survived: `<` -> `<=` at line 11 [relational-flip@11:5#0]; "
        "drop guard `if (x) return 0;` at line 12 [guard-drop@12:5#0]; "
        "not counted: 1 unknown, 1 skipped as equivalent"
    )
    assert grade.grading is not None
    assert grade.grading.reason == grade.reason
    assert grade.grading.kill_rate == 0.0


def test_kill_reason_without_survivors_or_extras() -> None:
    killed = MutantReading("a", "op", "d", MutantOutcome.KILLED)
    assert kill_reason([killed, killed]) == "killed 2/2; no survivors"


def test_rate_never_counts_unknown_error_or_skipped(
    source: Path, tmp_path: Path
) -> None:
    mutants = [make_mutant(i, f"mutant {i}") for i in range(1, 6)]
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify(
        {
            ("m1", "p1"): violated(),
            ("m2", "p1"): Unknown(meta(), UnknownReason.TIMEOUT),
            ("m3", "p1"): Error(meta(), "boom"),
        }
    )

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=mutants,
        verify=verify,
        equivalence=FakeEquivalence(equivalent={"m5"}),
    )

    (grade,) = got.properties
    assert grade.counts() == {
        "killed": 1, "survived": 1, "unknown": 1, "error": 1, "skipped": 1,
    }  # fmt: skip
    assert grade.kill_rate == 0.5  # 1 / (1 + 1), nothing else in the denominator


def test_no_killed_or_survived_mutant_stores_nothing(
    source: Path, tmp_path: Path
) -> None:
    mutants = [make_mutant(1, "m1"), make_mutant(2, "m2")]
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify({("m1", "p1"): Unknown(meta(), UnknownReason.TIMEOUT)})

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=mutants,
        verify=verify,
        equivalence=FakeEquivalence(equivalent={"m2"}),
    )

    (grade,) = got.properties
    assert grade.outcome is GradeOutcome.UNDECIDED
    assert grade.kill_rate is None
    assert grade.grading is None
    assert "no kill rate" in grade.reason
    assert "1 unknown" in grade.reason
    assert "1 skipped" in grade.reason
    assert got.outcome == "ungraded"
    stored = unit_props(store, source)["p1"]
    assert stored.status is PropertyStatus.CANDIDATE
    assert stored.grading is None
    payload = got.to_dict()
    assert payload["properties"][0]["kill_rate"] is None
    assert payload["properties"][0]["stored"] is False


def test_zero_mutants_is_undecided_not_a_perfect_score(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])

    got = run_grade(source, store, tmp_path, mutants=[])

    (grade,) = got.properties
    assert grade.outcome is GradeOutcome.UNDECIDED
    assert unit_props(store, source)["p1"].status is PropertyStatus.CANDIDATE


def test_partial_when_only_some_held_properties_get_a_rate(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1"), make_prop("p2")])
    verify = FakeVerify(
        {
            ("m1", "p1"): violated(),
            ("m1", "p2"): Unknown(meta(), UnknownReason.TIMEOUT),
        }
    )

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    assert {g.prop.property_id: g.outcome for g in got.properties} == {
        "p1": GradeOutcome.GRADED,
        "p2": GradeOutcome.UNDECIDED,
    }
    assert got.outcome == "partial"


def test_a_vacuous_mutant_verdict_is_unknown_not_survived(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify({}, unreachable={("m1", "p1")})

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    (grade,) = got.properties
    (reading,) = grade.readings
    assert reading.outcome is MutantOutcome.UNKNOWN
    assert "vacuous" in (reading.detail or "")
    assert grade.outcome is GradeOutcome.UNDECIDED


def test_a_mutant_the_harness_cannot_render_is_an_error(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1"), make_mutant(2, "m2")],
        verify=FakeVerify({("m2", "p1"): violated()}),
        render=FakeWriter(fail_on={"m1"}),
    )

    (grade,) = got.properties
    first, second = grade.readings
    assert first.outcome is MutantOutcome.ERROR
    assert "render failed" in (first.detail or "")
    assert second.outcome is MutantOutcome.KILLED


def test_nothing_held_generates_no_mutants(source: Path, tmp_path: Path) -> None:
    store = store_with(source, [make_prop("bad")])
    mutants = FakeMutants([make_mutant(1, "m1")])
    equivalence = FakeEquivalence()

    got = grade_unit(
        source,
        "f",
        store=store,
        work_dir=tmp_path / "work",
        mutants=mutants,
        equivalence=equivalence,
        verify_for=FakeVerify({("orig", "bad"): violated()}),
        render=FakeWriter(),
    )

    assert mutants.calls == 0
    assert equivalence.calls == []
    assert got.outcome == "ungraded"
    assert got.counts()["not_held"] == 1
    assert got.equivalence_k is None


def test_no_stored_property_runs_nothing(source: Path, tmp_path: Path) -> None:
    store = store_with(source, [])
    verify = FakeVerify({})

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    assert got.properties == ()
    assert verify.seen == []
    assert got.outcome == "ungraded"


def test_a_graded_property_is_skipped_and_never_checked(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("old", PropertyStatus.GRADED)])
    verify = FakeVerify({})

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    (grade,) = got.properties
    assert grade.outcome is GradeOutcome.NOT_CANDIDATE
    assert "graded" in grade.reason
    assert verify.seen == []
    stored = unit_props(store, source)["old"]
    assert stored.grading == Grading(GradingVerdict.HELD, 0.5, "earlier")


def test_a_property_that_stops_being_a_candidate_is_reported_not_crashed(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_with(source, [make_prop("p1")])
    real_grade = store.grade

    def racing(property_id: str, grading: Grading) -> Property:
        store.update_status(property_id, PropertyStatus.REJECTED)
        return real_grade(property_id, grading)

    monkeypatch.setattr(store, "grade", racing)
    verify = FakeVerify({("m1", "p1"): violated()})

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    (grade,) = got.properties
    assert grade.outcome is GradeOutcome.NOT_CANDIDATE
    assert "changed during grading" in grade.reason
    assert grade.grading is None
    assert got.outcome == "ungraded"


def test_a_held_property_lost_to_a_race_makes_the_run_partial(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_with(source, [make_prop("p1"), make_prop("p2")])
    real_grade = store.grade

    def racing(property_id: str, grading: Grading) -> Property:
        if property_id == "p1":
            store.update_status(property_id, PropertyStatus.REJECTED)
        return real_grade(property_id, grading)

    monkeypatch.setattr(store, "grade", racing)
    verify = FakeVerify({("m1", "p1"): violated(), ("m1", "p2"): violated()})

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=verify
    )

    assert got.counts()["not_candidate"] == 1
    assert got.counts()["graded"] == 1
    assert got.outcome == "partial"


def test_the_equivalence_filter_sees_main_renamed_sources(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])
    equivalence = FakeEquivalence(equivalent={"m1"})

    run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({}),
        equivalence=equivalence,
    )

    assert equivalence.calls
    for call in equivalence.calls:
        assert "int main(" not in call["reference"]
        assert "int main(" not in call["candidate"]
        assert call["symbol"] == "f"


def test_equivalence_uses_the_top_rung_and_the_check_max_len(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])
    equivalence = FakeEquivalence()

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({("m1", "p1"): violated()}),
        equivalence=equivalence,
        unwind=2,
        unwind_ladder=(3, 5),
        max_len=6,
    )

    assert {call["k"] for call in equivalence.calls} == {5}
    assert {call["max_len"] for call in equivalence.calls} == {6}
    assert got.equivalence_k == 5
    assert got.equivalence_max_len == 6


def test_an_unbounded_max_len_falls_back_to_the_default_for_equivalence(
    source: Path, tmp_path: Path
) -> None:
    from forseti.precond import DEFAULT_MAX_LEN

    store = store_with(source, [make_prop("p1")])

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({}),
        max_len=None,
    )

    assert got.max_len is None
    assert got.equivalence_max_len == DEFAULT_MAX_LEN


def test_a_refused_original_skips_nothing_and_says_so(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({("m1", "p1"): violated()}),
        equivalence=FakeEquivalence(equivalent={"m1"}, refuse="file-scope state"),
    )

    assert got.equivalent == ()
    assert got.original_refusal == "file-scope state"
    assert got.properties[0].counts()["killed"] == 1
    assert "file-scope state" in grade_transcript(got)
    assert got.to_dict()["equivalence"]["original_refusal"] == "file-scope state"


def test_assumed_equivalence_domain_is_surfaced(source: Path, tmp_path: Path) -> None:
    store = store_with(source, [make_prop("p1")])
    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({("m1", "p1"): violated()}),
        equivalence=FakeEquivalence(equivalent={"m1"}, assumptions=("0 <= lo <= hi",)),
    )

    assert got.equivalence_assumed == ("0 <= lo <= hi",)
    assert got.to_dict()["equivalence"]["assumed"] == ["0 <= lo <= hi"]
    assert "assumed: 0 <= lo <= hi" in grade_transcript(got)
    run_row = grade_trace_rows(got)[0][1]
    assert run_row["equivalence_assumed"] == ["0 <= lo <= hi"]


def test_no_assumed_domain_is_not_reported(source: Path, tmp_path: Path) -> None:
    store = store_with(source, [make_prop("p1")])
    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1")],
        verify=FakeVerify({("m1", "p1"): violated()}),
        equivalence=FakeEquivalence(equivalent={"m1"}),
    )

    assert got.equivalence_assumed == ()
    assert "equivalence check assumed" not in grade_transcript(got)


def test_esbmc_time_is_recorded_per_check_and_in_total(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify({("m1", "p1"): violated()})

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1"), make_mutant(2, "m2")],
        verify=verify,
        equivalence=FakeEquivalence(equivalent=set()),
    )

    killed, survived = got.properties[0].readings
    assert killed.esbmc_time_s == STEP  # one violated run
    assert survived.esbmc_time_s == 2 * STEP  # the run plus its vacuity probe
    assert got.baseline_esbmc_time_s == 2 * STEP
    assert got.equivalence_esbmc_time_s == 3 * STEP  # the baseline and two mutants
    assert got.mutant_esbmc_time_s == 3 * STEP
    assert got.esbmc_time_s == 8 * STEP
    times = got.to_dict()["esbmc_time_s"]
    assert times["total"] == got.esbmc_time_s
    assert set(times) == {"baseline", "equivalence", "mutants", "total"}


def test_json_carries_counts_totals_and_limitations(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1"), make_prop("bad")])
    verify = FakeVerify({("orig", "bad"): violated(), ("m1", "p1"): violated()})
    mutants = FakeMutants(
        [make_mutant(1, "m1"), make_mutant(2, "m2"), make_mutant(3, "m3")], dropped=4
    )

    got = grade_unit(
        source,
        "f",
        store=store,
        work_dir=tmp_path / "work",
        mutants=mutants,
        equivalence=FakeEquivalence(equivalent={"m3"}),
        verify_for=verify,
        render=FakeWriter(),
    )
    payload = json.loads(json.dumps(got.to_dict()))

    assert payload["outcome"] == "graded"
    assert payload["counts"] == {
        "graded": 1, "undecided": 0, "not_held": 1, "not_candidate": 0,
    }  # fmt: skip
    assert payload["mutants"] == {"generated": 3, "dropped": 4, "graded_against": 2}
    assert payload["equivalence"]["skipped"] == 1
    assert payload["equivalence"]["skipped_equivalent"] == ["relational-flip@13:5#0"]
    assert payload["unwind_ladder"] == [8, 16]
    assert payload["limitations"] == [BOUNDED_LIMITATION, EQUIVALENCE_BIAS]
    graded = next(p for p in payload["properties"] if p["property_id"] == "p1")
    assert graded["counts"] == {
        "killed": 1, "survived": 1, "unknown": 0, "error": 0, "skipped": 1,
    }  # fmt: skip
    assert graded["kill_rate"] == 0.5
    assert graded["stored"] is True
    assert graded["grading"] == {
        "verdict": "held",
        "kill_rate": 0.5,
        "reason": graded["reason"],
    }
    assert {m["outcome"] for m in graded["mutants"]} == {
        "killed",
        "survived",
        "skipped",
    }
    for entry in graded["mutants"]:
        assert set(entry) == {
            "mutant_id", "operator", "description", "outcome", "k",
            "esbmc_time_s", "detail",
        }  # fmt: skip


def test_the_transcript_states_the_bounds_and_the_bias(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1")])

    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=FakeVerify({})
    )
    text = grade_transcript(got)

    assert BOUNDED_LIMITATION in text
    assert EQUIVALENCE_BIAS in text
    assert "kill rate" in text
    assert "survived: m1 [relational-flip@11:5#0]" in text


def test_trace_rows_cover_every_property_and_mutant(
    source: Path, tmp_path: Path
) -> None:
    store = store_with(source, [make_prop("p1"), make_prop("bad")])
    verify = FakeVerify({("orig", "bad"): violated(), ("m1", "p1"): violated()})

    got = run_grade(
        source,
        store,
        tmp_path,
        mutants=[make_mutant(1, "m1"), make_mutant(2, "m2")],
        verify=verify,
    )
    rows = grade_trace_rows(got)

    assert [t for t, _ in rows].count(RUN_EVENT) == 1
    assert [t for t, _ in rows].count(PROPERTY_EVENT) == 2
    mutant_rows = [f for t, f in rows if t == MUTANT_EVENT]
    assert [(f["property_id"], f["mutant_id"], f["outcome"]) for f in mutant_rows] == [
        ("p1", "relational-flip@11:5#0", "killed"),
        ("p1", "relational-flip@12:5#0", "survived"),
    ]
    run_row = rows[0][1]
    assert run_row["outcome"] == "graded"
    assert run_row["properties_not_held"] == 1
    assert run_row["mutants_equivalent"] == 0
    json.dumps(rows)


def test_record_trace_appends_json_lines(source: Path, tmp_path: Path) -> None:
    store = store_with(source, [make_prop("p1")])
    got = run_grade(
        source, store, tmp_path, mutants=[make_mutant(1, "m1")], verify=FakeVerify({})
    )
    root = tmp_path / "root"

    record_trace(root, got)

    lines = [
        json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
    ]
    assert [line["type"] for line in lines] == [RUN_EVENT, PROPERTY_EVENT, MUTANT_EVENT]
    assert lines[2]["esbmc_time_s"] == 2 * STEP


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_len": -1}, "max_len"),
        ({"max_mutants": 0}, "max_mutants"),
        ({"max_mutants": -3}, "max_mutants"),
        ({"unwind": 8, "unwind_ladder": (4,)}, "ladder"),
    ],
)
def test_bad_bounds_fail_before_any_work(
    source: Path, tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    store = store_with(source, [make_prop("p1")])
    verify = FakeVerify({})

    with pytest.raises(ValueError, match=message):
        run_grade(source, store, tmp_path, mutants=[], verify=verify, **kwargs)

    assert verify.seen == []


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (PropertyOutcome.VIOLATED, MutantOutcome.KILLED),
        (PropertyOutcome.HELD, MutantOutcome.SURVIVED),
        (PropertyOutcome.UNKNOWN, MutantOutcome.UNKNOWN),
        (PropertyOutcome.VACUOUS, MutantOutcome.UNKNOWN),
        (PropertyOutcome.ERROR, MutantOutcome.ERROR),
        (PropertyOutcome.SKIPPED, MutantOutcome.ERROR),
    ],
)
def test_mutant_reading_table(
    outcome: PropertyOutcome, expected: MutantOutcome, source: Path
) -> None:
    from forseti.grading.grade import _subject

    mutant = make_mutant(1, "m1")
    subject = _subject(mutant, Unit.from_path(source, "f"), "f")
    verdict = PropertyVerdict("p", "u", "semantic", outcome, 4, None, "h")

    reading = mutant_reading(subject, verdict, 1.5)

    assert reading.outcome is expected
    assert reading.esbmc_time_s == 1.5
    assert reading.mutant_id == "relational-flip@11:5#0"
    assert reading.k == 4


def test_an_unresolved_mutant_verdict_records_why(source: Path) -> None:
    from forseti.grading.grade import _subject

    subject = _subject(make_mutant(1, "m1"), Unit.from_path(source, "f"), "f")
    timeout = PropertyVerdict(
        "p",
        "u",
        "semantic",
        PropertyOutcome.UNKNOWN,
        4,
        Unknown(meta(), UnknownReason.TIMEOUT),
        "h",
    )
    vacuous = PropertyVerdict(
        "p", "u", "semantic", PropertyOutcome.VACUOUS, 4, None, "h"
    )

    assert mutant_reading(subject, timeout, 0.0).detail == "unknown (timeout)"
    assert mutant_reading(subject, vacuous, 0.0).detail == "vacuous"


def test_a_mutant_of_main_keeps_the_requested_unit_id(tmp_path: Path) -> None:
    from forseti.grading.grade import _subject

    path = tmp_path / "m.c"
    path.write_text("int main(void) { return 0; }\n")
    unit = Unit.from_path(path, "main")

    subject = _subject(make_mutant(1, "m1"), unit, "main")

    assert subject.unit.unit_id == unit.unit_id
    assert subject.unit.symbol == unit.symbol


def test_the_mutant_source_forwards_the_timeout(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forseti.grading import grade as grade_module

    seen: list[dict[str, Any]] = []

    def fake_mutate(path: Path, symbol: str, **kwargs: Any) -> MutationResult:
        seen.append(kwargs)
        return MutationResult((), 0)

    monkeypatch.setattr(grade_module, "mutate_function", fake_mutate)

    grade_module.default_mutant_source(timeout_s=300.0)(source, "f")
    grade_module.default_mutant_source()(source, "f")

    assert seen[0]["timeout_s"] == 300.0
    assert "timeout_s" not in seen[1]
