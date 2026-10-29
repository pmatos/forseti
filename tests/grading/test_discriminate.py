"""The bug-pair discrimination scorer with every port faked: no esbmc, no network.

S0, the harness writer and the verifier are scripted, so these tests pin the
scorer's own rules -- the usable-pair filter and its precedence, the
discrimination table, M4 absorption, the JSON and trace shapes, and property-set
loading.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
from forseti.grading import discriminate
from forseti.grading.discriminate import (
    PAIR_EVENT,
    PROPERTY_EVENT,
    Absorption,
    Discrimination,
    ExclusionReason,
    PairOutcome,
    PropertyScore,
    PropertySetError,
    SafetyOutcome,
    SafetyReading,
    classify,
    esbmc_safety_port,
    esbmc_verify_for,
    exclusion_for,
    load_properties_file,
    properties_from_data,
    reading_from_discharge,
    record_trace,
    score_pair,
    trace_rows,
)
from forseti.orchestrator import PropertyOutcome, PropertyVerdict, RenderedHarness, Unit
from forseti.precond import (
    DEFAULT_MAX_LEN,
    Assessment,
    DischargeResult,
    PreconditionResult,
)
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
)

SOURCE = "int f(int x, int lo) { return x < lo ? lo : x; }\n"
PASSED = SafetyReading(SafetyOutcome.PASSED, "ok", assessment="discharged_verified")
FAILED = SafetyReading(SafetyOutcome.VIOLATED, "memory violation")
INCONCLUSIVE = SafetyReading(SafetyOutcome.INCONCLUSIVE, "timeout")


def meta() -> RunMeta:
    return RunMeta("8.5.0", ("esbmc",), 0, 0.0, "", "")


def violated(description: str = "assertion") -> Violated:
    loc = SourceLoc("h.c", 1, 1, "main")
    cex = Counterexample((), ViolatedProperty(loc, description, None, ()))
    return Violated(meta(), "[Counterexample]", cex)


def outside_domain() -> Violated:
    return violated("forseti:domain")


def reached_probe() -> Violated:
    return Violated(meta(), "Violated property:\n  forseti:non-vacuity", None)


def prop(
    property_id: str,
    expression: str = "result >= 0",
    domain: tuple[str, ...] = (),
    kind: PropertyKind = PropertyKind.SEMANTIC,
) -> Property:
    return Property(
        property_id,
        "u.c::f",
        kind,
        expression,
        PropertyStatus.CANDIDATE,
        Provenance("t", "1"),
        domain=domain,
    )


def verdict(outcome: PropertyOutcome, reason: str | None = None) -> PropertyVerdict:
    result: EsbmcResult | None = {
        PropertyOutcome.HELD: Verified(meta()),
        PropertyOutcome.VIOLATED: violated(),
    }.get(outcome)
    return PropertyVerdict("p", "u", "semantic", outcome, 4, result, "h", reason)


class FakeSafety:
    def __init__(self, fixed: SafetyReading, buggy: SafetyReading) -> None:
        self.by_name = {"fixed.c": fixed, "buggy.c": buggy}
        self.calls: list[str] = []

    def __call__(self, source: Path, symbol: str, /) -> SafetyReading:
        self.calls.append(source.name)
        return self.by_name[source.name]


class FakeWriter:
    def __init__(self) -> None:
        self.renders = 0

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        self.renders += 1
        return RenderedHarness("/* harness */", probe_source_text="/* probe */")


class FakeVerify:
    """Scripted by (version, property id); domain programs by clause text."""

    def __init__(
        self,
        verdicts: Mapping[tuple[str, str], EsbmcResult],
        domain: Mapping[str, EsbmcResult] | None = None,
    ) -> None:
        self.verdicts = verdicts
        self.domain = domain or {}
        self.paths: list[Path] = []

    def for_source(self, source: Path):  # noqa: ANN201
        version = source.stem

        def port(path: Path, /, *, unwind: int) -> EsbmcResult:
            self.paths.append(path)
            if path.name.startswith("clause"):
                text = path.read_text()
                for clause, result in self.domain.items():
                    if clause in text:
                        return result
                return Error(meta(), "no scripted result")
            if path.name.endswith(".probe.c"):
                return reached_probe()
            property_id = path.stem.split("__")[-1]
            return self.verdicts[(version, property_id)]

        return port


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    buggy = tmp_path / "buggy.c"
    fixed = tmp_path / "fixed.c"
    buggy.write_text(SOURCE)
    fixed.write_text(SOURCE)
    return buggy, fixed, tmp_path / "work"


def test_exclusion_precedence_covers_every_branch() -> None:
    got = exclusion_for(FAILED, None)
    assert got is not None
    assert got.reason is ExclusionReason.S0_FAILS_CLEAN
    got = exclusion_for(INCONCLUSIVE, None)
    assert got is not None
    assert got.reason is ExclusionReason.S0_UNKNOWN
    got = exclusion_for(PASSED, FAILED)
    assert got is not None
    assert got.reason is ExclusionReason.S0_CATCHES_BUG
    got = exclusion_for(PASSED, INCONCLUSIVE)
    assert got is not None
    assert got.reason is ExclusionReason.S0_UNKNOWN
    assert exclusion_for(PASSED, PASSED) is None


@pytest.mark.parametrize(
    ("assessment", "outcome", "assumed"),
    [
        (Assessment.DISCHARGED_VERIFIED, SafetyOutcome.PASSED, False),
        (Assessment.ASSUMED_VERIFIED, SafetyOutcome.PASSED, True),
        (Assessment.VIOLATED, SafetyOutcome.VIOLATED, False),
        (Assessment.UNKNOWN, SafetyOutcome.INCONCLUSIVE, False),
        (Assessment.VACUOUS, SafetyOutcome.INCONCLUSIVE, False),
        (Assessment.NEEDS_CONTRACT, SafetyOutcome.INCONCLUSIVE, False),
        (Assessment.ERROR, SafetyOutcome.INCONCLUSIVE, False),
    ],
)
def test_discharge_assessment_maps_to_an_s0_reading(
    assessment: Assessment, outcome: SafetyOutcome, assumed: bool
) -> None:
    unit = PreconditionResult("f", assessment, "detail")
    reading = reading_from_discharge(DischargeResult("f", assessment, "d", unit))
    assert reading.outcome is outcome
    assert reading.assumed is assumed
    assert reading.assessment == assessment.value


@pytest.mark.parametrize(
    ("fixed", "buggy", "expected"),
    [
        (PropertyOutcome.HELD, PropertyOutcome.VIOLATED, Discrimination.DISCRIMINATES),
        (PropertyOutcome.HELD, PropertyOutcome.HELD, Discrimination.NOT_DISCRIMINATING),
        (
            PropertyOutcome.VIOLATED,
            PropertyOutcome.VIOLATED,
            Discrimination.NOT_DISCRIMINATING,
        ),
        (
            PropertyOutcome.VIOLATED,
            PropertyOutcome.UNKNOWN,
            Discrimination.NOT_DISCRIMINATING,
        ),
        (PropertyOutcome.HELD, PropertyOutcome.UNKNOWN, Discrimination.INCONCLUSIVE),
        (PropertyOutcome.HELD, PropertyOutcome.VACUOUS, Discrimination.INCONCLUSIVE),
        (PropertyOutcome.HELD, PropertyOutcome.ERROR, Discrimination.INCONCLUSIVE),
        (
            PropertyOutcome.UNKNOWN,
            PropertyOutcome.VIOLATED,
            Discrimination.INCONCLUSIVE,
        ),
        (
            PropertyOutcome.VACUOUS,
            PropertyOutcome.VIOLATED,
            Discrimination.INCONCLUSIVE,
        ),
        (PropertyOutcome.SKIPPED, PropertyOutcome.SKIPPED, Discrimination.INCONCLUSIVE),
    ],
)
def test_classify_table(
    fixed: PropertyOutcome, buggy: PropertyOutcome, expected: Discrimination
) -> None:
    assert classify(verdict(fixed), verdict(buggy))[0] is expected


def test_inconclusive_reason_names_both_sides_and_any_note() -> None:
    _, reason = classify(
        verdict(PropertyOutcome.HELD),
        verdict(PropertyOutcome.VACUOUS, reason="post-call point unreachable"),
    )
    assert "fixed: held" in reason
    assert "buggy: vacuous" in reason
    assert "post-call point unreachable" in reason


@pytest.mark.parametrize(
    ("fixed", "buggy", "reason", "buggy_called"),
    [
        (FAILED, PASSED, ExclusionReason.S0_FAILS_CLEAN, False),
        (INCONCLUSIVE, PASSED, ExclusionReason.S0_UNKNOWN, False),
        (PASSED, FAILED, ExclusionReason.S0_CATCHES_BUG, True),
        (PASSED, INCONCLUSIVE, ExclusionReason.S0_UNKNOWN, True),
    ],
)
def test_excluded_pair_is_reported_and_never_reaches_property_checking(
    pair: tuple[Path, Path, Path],
    fixed: SafetyReading,
    buggy: SafetyReading,
    reason: ExclusionReason,
    buggy_called: bool,
) -> None:
    buggy_path, fixed_path, work = pair
    safety = FakeSafety(fixed, buggy)
    writer = FakeWriter()
    factory_calls: list[Path] = []

    def verify_for(source: Path):  # noqa: ANN202
        factory_calls.append(source)
        raise AssertionError("an excluded pair must not verify a property")

    got = score_pair(
        buggy=buggy_path,
        fixed=fixed_path,
        symbol="f",
        properties=[prop("a")],
        safety=safety,
        verify_for=verify_for,
        render=writer,
        work_dir=work,
    )

    assert got.outcome is PairOutcome.EXCLUDED
    assert not got.usable
    assert got.exclusion is not None
    assert got.exclusion.reason is reason
    assert got.scores == ()
    assert ("buggy.c" in safety.calls) is buggy_called
    assert writer.renders == 0
    assert factory_calls == []
    assert not work.exists()
    payload = json.loads(json.dumps(got.to_dict()))
    assert payload["usable"] is False
    assert payload["exclusion"]["reason"] == reason.value
    assert payload["outcome"] == "excluded"
    assert payload["properties"] == []


def test_scored_pair_reports_each_property_and_the_pair(
    pair: tuple[Path, Path, Path],
) -> None:
    buggy, fixed, work = pair
    fake = FakeVerify(
        {
            ("fixed", "disc"): Verified(meta()),
            ("buggy", "disc"): violated(),
            ("fixed", "both"): Verified(meta()),
            ("buggy", "both"): Verified(meta()),
            ("fixed", "unk"): Verified(meta()),
            ("buggy", "unk"): Unknown(meta(), UnknownReason.TIMEOUT),
        }
    )
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("disc"), prop("both"), prop("unk")],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=fake.for_source,
        render=FakeWriter(),
        work_dir=work,
    )

    assert got.usable
    assert got.outcome is PairOutcome.DISCRIMINATED
    assert [s.discriminates for s in got.scores] == [True, False, False]
    assert [s.prop.property_id for s in got.discriminating] == ["disc"]
    assert got.counts()["discrimination"] == {
        "discriminates": 1,
        "not_discriminating": 1,
        "inconclusive": 1,
    }
    payload = json.loads(json.dumps(got.to_dict()))
    first = payload["properties"][0]
    assert first["fixed"]["outcome"] == "held"
    assert first["buggy"]["outcome"] == "violated"
    assert first["buggy"]["violated_property"] == "assertion"
    assert first["discriminates"] is True
    assert payload["s0"]["buggy"]["outcome"] == "passed"
    assert payload["trigger"] is None
    # each version is checked in its own directory
    assert {p.parent.name for p in fake.paths} == {"fixed", "buggy"}


def test_pair_outcome_without_a_discriminating_property(
    pair: tuple[Path, Path, Path],
) -> None:
    buggy, fixed, work = pair
    held_both = FakeVerify(
        {("fixed", "a"): Verified(meta()), ("buggy", "a"): Verified(meta())}
    )
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("a")],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=held_both.for_source,
        render=FakeWriter(),
        work_dir=work,
    )
    assert got.outcome is PairOutcome.NOT_DISCRIMINATED

    unknown = FakeVerify(
        {
            ("fixed", "a"): Verified(meta()),
            ("buggy", "a"): Unknown(meta(), UnknownReason.TIMEOUT),
        }
    )
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("a")],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=unknown.for_source,
        render=FakeWriter(),
        work_dir=work,
    )
    assert got.outcome is PairOutcome.INCONCLUSIVE


def test_an_empty_property_set_is_reported_as_empty(
    pair: tuple[Path, Path, Path],
) -> None:
    buggy, fixed, work = pair
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=FakeVerify({}).for_source,
        render=FakeWriter(),
        work_dir=work,
    )
    assert got.usable
    assert got.outcome is PairOutcome.EMPTY


def absorption_run(
    pair: tuple[Path, Path, Path],
    props: list[Property],
    verdicts: Mapping[tuple[str, str], EsbmcResult],
    domain: Mapping[str, EsbmcResult],
    trigger: dict[str, int] | None,
) -> list[Absorption]:
    buggy, fixed, work = pair
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=props,
        safety=FakeSafety(PASSED, PASSED),
        verify_for=FakeVerify(verdicts, domain).for_source,
        render=FakeWriter(),
        work_dir=work,
        trigger=trigger,
    )
    return [s.absorption for s in got.scores]


def test_absorption_reads_the_domain_on_the_triggering_input(
    pair: tuple[Path, Path, Path],
) -> None:
    held = {
        ("fixed", "out"): Verified(meta()),
        ("buggy", "out"): Verified(meta()),
        ("fixed", "inside"): Verified(meta()),
        ("buggy", "inside"): Verified(meta()),
        ("fixed", "nodomain"): Verified(meta()),
        ("buggy", "nodomain"): Verified(meta()),
        ("fixed", "unsure"): Verified(meta()),
        ("buggy", "unsure"): Verified(meta()),
        ("fixed", "killed"): Verified(meta()),
        ("buggy", "killed"): violated(),
    }
    got = absorption_run(
        pair,
        [
            prop("out", domain=("x <= 3",)),
            prop("inside", domain=("x >= 0",)),
            prop("nodomain"),
            prop("unsure", domain=("x >= 0", "mystery > 1")),
            prop("killed", domain=("x <= 3",)),
        ],
        held,
        {
            "x <= 3": outside_domain(),
            "x >= 0": Verified(meta()),
            "mystery > 1": Error(meta(), "undeclared identifier"),
        },
        {"x": 10, "lo": 0},
    )
    assert got == [
        Absorption.ABSORBED,
        Absorption.IN_DOMAIN,
        Absorption.IN_DOMAIN,
        Absorption.UNDETERMINED,
        Absorption.NOT_APPLICABLE,
    ]


def test_undefined_behaviour_in_a_domain_clause_is_not_absorption(
    pair: tuple[Path, Path, Path],
) -> None:
    held = {("fixed", "ub"): Verified(meta()), ("buggy", "ub"): Verified(meta())}
    got = absorption_run(
        pair,
        [prop("ub", domain=("x / lo >= 0",))],
        held,
        {"x / lo >= 0": violated("division by zero")},
        {"x": 10, "lo": 0},
    )
    assert got == [Absorption.UNDETERMINED]


def test_absorption_is_not_applicable_without_a_trigger(
    pair: tuple[Path, Path, Path],
) -> None:
    held = {("fixed", "a"): Verified(meta()), ("buggy", "a"): Verified(meta())}
    got = absorption_run(pair, [prop("a", domain=("x <= 3",))], held, {}, None)
    assert got == [Absorption.NOT_APPLICABLE]


def test_absorption_detail_and_domain_program(pair: tuple[Path, Path, Path]) -> None:
    buggy, fixed, work = pair
    fake = FakeVerify(
        {("fixed", "a"): Verified(meta()), ("buggy", "a"): Verified(meta())},
        {"x <= 3": outside_domain()},
    )
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("a", domain=("x <= 3",))],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=fake.for_source,
        render=FakeWriter(),
        work_dir=work,
        trigger={"x": -7, "lo": 2**64 - 1},
    )
    score = got.scores[0]
    assert score.absorption_detail is not None
    assert "x=-7" in score.absorption_detail
    assert "excludes the bug" in score.absorption_detail
    assert got.to_dict()["trigger"] == {"lo": 2**64 - 1, "x": -7}
    program = (work / "domain" / "p0" / "clause0.c").read_text()
    assert "int x = (int)(-7LL);" in program
    assert f"int lo = (int){2**64 - 1}ULL;" in program
    assert '__ESBMC_assert((x <= 3), "forseti:domain");' in program


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ({"nope": 1, "x": 1, "lo": 1}, "not a scalar parameter"),
        ({"x": 2**64, "lo": 1}, "out of range"),
        ({"x": -(2**63) - 1, "lo": 1}, "out of range"),
        ({"x": 10}, r"missing \['lo'\]"),
    ],
)
def test_bad_trigger_fails_before_any_check(
    pair: tuple[Path, Path, Path], trigger: dict[str, int], message: str
) -> None:
    buggy, fixed, work = pair
    safety = FakeSafety(PASSED, PASSED)
    with pytest.raises(ValueError, match=message):
        score_pair(
            buggy=buggy,
            fixed=fixed,
            symbol="f",
            properties=[],
            safety=safety,
            verify_for=FakeVerify({}).for_source,
            render=FakeWriter(),
            work_dir=work,
            trigger=trigger,
        )
    assert safety.calls == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_len": -1}, "max_len"),
        ({"unwind": 8, "unwind_ladder": (8, 16)}, "increasing"),
        ({"unwind_ladder": (2,)}, "increasing"),
    ],
)
def test_bad_bounds_fail_before_s0_runs_esbmc(
    pair: tuple[Path, Path, Path], kwargs: dict[str, Any], message: str
) -> None:
    buggy, fixed, work = pair
    safety = FakeSafety(PASSED, PASSED)
    with pytest.raises(ValueError, match=message):
        score_pair(
            buggy=buggy,
            fixed=fixed,
            symbol="f",
            properties=[],
            safety=safety,
            verify_for=FakeVerify({}).for_source,
            render=FakeWriter(),
            work_dir=work,
            **kwargs,
        )
    assert safety.calls == []


def test_a_raised_unwind_derives_its_own_ladder(
    pair: tuple[Path, Path, Path],
) -> None:
    buggy, fixed, work = pair
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=FakeVerify({}).for_source,
        render=FakeWriter(),
        work_dir=work,
        unwind=8,
    )
    assert got.outcome is PairOutcome.EMPTY


def test_default_safety_port_uses_the_property_max_len(
    pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    buggy, fixed, work = pair
    seen: list[int] = []

    def fake_port(*, max_len: int, **_: object) -> FakeSafety:
        seen.append(max_len)
        return FakeSafety(FAILED, FAILED)

    monkeypatch.setattr(discriminate, "esbmc_safety_port", fake_port)
    for max_len in (5, None):
        score_pair(
            buggy=buggy,
            fixed=fixed,
            symbol="f",
            properties=[],
            work_dir=work,
            max_len=max_len,
        )
    assert seen == [5, DEFAULT_MAX_LEN]


def test_trigger_needs_a_readable_signature(pair: tuple[Path, Path, Path]) -> None:
    buggy, fixed, work = pair
    with pytest.raises(ValueError, match="signature"):
        score_pair(
            buggy=buggy,
            fixed=fixed,
            symbol="missing",
            properties=[],
            safety=FakeSafety(PASSED, PASSED),
            verify_for=FakeVerify({}).for_source,
            render=FakeWriter(),
            work_dir=work,
            trigger={"x": 1},
        )


def test_trace_rows_and_record_trace(
    pair: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    buggy, fixed, work = pair
    fake = FakeVerify({("fixed", "a"): Verified(meta()), ("buggy", "a"): violated()})
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("a")],
        safety=FakeSafety(PASSED, PASSED),
        verify_for=fake.for_source,
        render=FakeWriter(),
        work_dir=work,
    )
    rows = trace_rows(got)
    assert [kind for kind, _ in rows] == [PAIR_EVENT, PROPERTY_EVENT]
    assert rows[0][1]["outcome"] == "discriminated"
    assert rows[0][1]["discriminating"] == 1
    assert rows[1][1]["discrimination"] == "discriminates"

    store_root = tmp_path / ".forseti"
    record_trace(store_root, got)
    lines = [
        json.loads(line)
        for line in (store_root / "events.jsonl").read_text().splitlines()
    ]
    assert [line["type"] for line in lines] == [PAIR_EVENT, PROPERTY_EVENT]
    assert all("ts" in line for line in lines)
    assert lines[1]["property_id"] == "a"


def test_trace_rows_for_an_excluded_pair(pair: tuple[Path, Path, Path]) -> None:
    buggy, fixed, work = pair
    got = score_pair(
        buggy=buggy,
        fixed=fixed,
        symbol="f",
        properties=[prop("a")],
        safety=FakeSafety(PASSED, FAILED),
        verify_for=FakeVerify({}).for_source,
        render=FakeWriter(),
        work_dir=work,
    )
    rows = trace_rows(got)
    assert len(rows) == 1
    assert rows[0][1]["exclusion"] == "s0_catches_bug"
    assert rows[0][1]["s0_buggy"] == "violated"


def test_properties_from_a_propose_payload_and_bare_lists() -> None:
    entry = {"expression": "result >= 0", "domain": ["lo <= hi"]}
    propose = properties_from_data({"accepted": [entry], "rejected": []}, unit_id="u")
    bare = properties_from_data([entry], unit_id="u")
    keyed = properties_from_data({"properties": [entry]}, unit_id="u")
    assert propose == bare == keyed
    only = propose[0]
    assert only.domain == ("lo <= hi",)
    assert only.kind is PropertyKind.SEMANTIC
    assert only.unit_id == "u"
    assert len(only.property_id) == 16


def test_property_ids_and_units_are_kept_when_given() -> None:
    (got,) = properties_from_data(
        [
            {
                "expression": "e",
                "property_id": "abc",
                "unit_id": "own",
                "kind": "reachability",
            }
        ],
        unit_id="u",
    )
    assert (got.property_id, got.unit_id, got.kind) == (
        "abc",
        "own",
        PropertyKind.REACHABILITY,
    )


def test_proposer_provenance_is_read_back_and_reported() -> None:
    entry = {
        "expression": "e",
        "provenance": {
            "prompt_id": "blind",
            "prompt_version": "3",
            "provider": "claude -p",
            "model": "sonnet",
            "recipe": "blind/v1",
            "extra": 1,
        },
    }
    (got, bare, odd) = properties_from_data(
        [entry, {"expression": "f"}, {"expression": "g", "provenance": {"model": 7}}],
        unit_id="u",
    )
    assert got.provenance == Provenance("blind", "3", "claude -p", "sonnet", "blind/v1")
    assert bare.provenance == Provenance("", "")
    assert odd.provenance == Provenance("", "")
    score = PropertyScore(
        got,
        verdict(PropertyOutcome.HELD),
        verdict(PropertyOutcome.VIOLATED),
        Discrimination.DISCRIMINATES,
        "r",
    )
    assert score.to_dict()["provenance"]["model"] == "sonnet"


@pytest.mark.parametrize(
    "data",
    [
        {"nothing": []},
        "text",
        [3],
        [{"expression": "  "}],
        [{"expression": "e", "domain": "lo <= hi"}],
        [{"expression": "e", "domain": [1]}],
        [{"expression": "e", "kind": "bogus"}],
    ],
)
def test_malformed_property_sets_are_rejected(data: object) -> None:
    with pytest.raises(PropertySetError):
        properties_from_data(data, unit_id="u")


def test_load_properties_file(tmp_path: Path) -> None:
    good = tmp_path / "props.json"
    good.write_text(json.dumps({"accepted": [{"expression": "e"}]}))
    assert load_properties_file(good, unit_id="u")[0].expression == "e"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(PropertySetError, match="cannot read"):
        load_properties_file(bad, unit_id="u")
    with pytest.raises(PropertySetError, match="cannot read"):
        load_properties_file(tmp_path / "missing.json", unit_id="u")


def test_verify_factory_binds_the_real_runner(tmp_path: Path) -> None:
    source = tmp_path / "f.c"
    source.write_text(SOURCE)
    port = esbmc_verify_for(esbmc_bin=str(tmp_path / "no-such-esbmc"))(source)
    assert isinstance(port(source, unwind=1), Error)


def test_safety_port_without_a_working_esbmc_is_inconclusive(tmp_path: Path) -> None:
    source = tmp_path / "f.c"
    source.write_text(SOURCE)
    port = esbmc_safety_port(esbmc_bin=str(tmp_path / "no-such-esbmc"))
    assert port(source, "f").outcome is SafetyOutcome.INCONCLUSIVE
