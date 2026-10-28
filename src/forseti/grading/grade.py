"""Kill-rate scorer (W3.3, #392): grade held properties against mutants.

A property earns a score by *holding on the real code and breaking on mutants*.
For a unit, the candidate set is the properties the check phase reports ``HELD``
(non-vacuously) on the real source. Each is re-rendered against every
non-equivalent mutant (#390 generates them, #391 drops the equivalent ones) and
checked with the same ladder and vacuity probe. Per property and mutant:

- ``KILLED``: the harness is ``VIOLATED`` on the mutant;
- ``SURVIVED``: ``HELD`` on the mutant, post-call point reached;
- ``UNKNOWN``: inconclusive at every rung, or ``VACUOUS`` (the postcondition was
  never evaluated, so it can neither kill nor survive);
- ``ERROR``: the harness could not be rendered or ESBMC failed;
- ``SKIPPED``: the mutant was found equivalent to the original.

``kill_rate = killed / (killed + survived)``. UNKNOWN, ERROR and SKIPPED are
reported beside it and never folded into it (roadmap Risk 9). When
``killed + survived == 0`` the rate is undefined: nothing is stored and the
property stays ``CANDIDATE``. A graded property stores ``Grading(HELD, rate,
reason)`` and moves to ``GRADED`` in one transaction (``PropertyStore.grade``);
``verdict`` is always ``HELD`` because only held properties are graded, so the
rate and the reason carry the quality signal. v1 grades ``CANDIDATE`` properties
only.

Vocabulary: ESBMC emits no proof object. Every verdict is "no violation found up
to k (and ``len <= max_len``)", never "for all inputs". A mutant counts as killed
whenever its harness is violated, including a mutant that would violate the
property for reasons unrelated to the mutation (not special-cased in v1).
Equivalent-mutant skipping uses a bounded check, so a mutant diverging only
beyond that bound is skipped too, which shrinks the denominator and inflates the
kill rate; the skipped count is always reported.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol

from forseti._events import record_event
from forseti.esbmc import Error, EsbmcResult, Unknown
from forseti.orchestrator import (
    HarnessWriterPort,
    PropertyCheckRun,
    PropertyOutcome,
    PropertyVerdict,
    Unit,
    VerifyPort,
    check_properties,
)
from forseti.orchestrator.semantic_check import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_UNWIND,
    SemanticCheckPlan,
)
from forseti.precond import DEFAULT_MAX_LEN
from forseti.properties import (
    Grading,
    GradingVerdict,
    InvalidStatusTransition,
    Property,
    PropertyNotFound,
    PropertyStatus,
)

from .equivalence import (
    EquivalenceChecker,
    EquivalenceRefused,
    EquivalenceResult,
    check_equivalence,
    filter_equivalent,
)
from .mutate import Mutant, MutationResult, mutate_function

RUN_EVENT = "grading.run"
PROPERTY_EVENT = "grading.property"
MUTANT_EVENT = "grading.mutant"

BOUNDED_LIMITATION = (
    "every verdict is bounded: killed means a violation was found, survived means "
    "none was found up to the unwind bound k and len<=max_len, not for all inputs"
)
EQUIVALENCE_BIAS = (
    "equivalent mutants are skipped by a bounded check: a mutant that diverges "
    "only beyond that bound is skipped too, which shrinks the denominator and "
    "inflates the kill rate (the skipped count is reported beside it)"
)
LIMITATIONS: tuple[str, ...] = (BOUNDED_LIMITATION, EQUIVALENCE_BIAS)


class MutantOutcome(Enum):
    KILLED = "killed"
    SURVIVED = "survived"
    UNKNOWN = "unknown"
    ERROR = "error"
    SKIPPED = "skipped"  # equivalent to the original up to the bound


class GradeOutcome(Enum):
    GRADED = "graded"  # a Grading was stored and the property is GRADED
    UNDECIDED = "undecided"  # killed + survived == 0: no rate, stays CANDIDATE
    NOT_HELD = "not_held"  # not HELD on the real code: nothing to grade
    NOT_CANDIDATE = "not_candidate"  # v1 grades CANDIDATE properties only


class MutantSource(Protocol):
    """The mutants of `symbol` in `source` (default: `mutate_function`)."""

    def __call__(self, source: Path, symbol: str, /) -> MutationResult: ...


class GradeStorePort(Protocol):
    """The slice of `PropertyStore` the scorer uses."""

    def list_for_unit(self, unit_id: str) -> Sequence[Property]: ...

    def grade(self, property_id: str, grading: Grading) -> object: ...


@dataclass(frozen=True)
class MutantReading:
    """One property on one mutant."""

    mutant_id: str
    operator: str
    description: str
    outcome: MutantOutcome
    k: int | None = None
    esbmc_time_s: float = 0.0
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutant_id": self.mutant_id,
            "operator": self.operator,
            "description": self.description,
            "outcome": self.outcome.value,
            "k": self.k,
            "esbmc_time_s": self.esbmc_time_s,
            "detail": self.detail,
        }


def tally(readings: Sequence[MutantReading]) -> dict[MutantOutcome, int]:
    """Per-outcome count; every `MutantOutcome` key present (0 if none)."""
    counts = {outcome: 0 for outcome in MutantOutcome}
    for reading in readings:
        counts[reading.outcome] += 1
    return counts


def kill_rate_of(readings: Sequence[MutantReading]) -> float | None:
    """``killed / (killed + survived)``, or ``None`` when neither happened."""
    counts = tally(readings)
    killed = counts[MutantOutcome.KILLED]
    decided = killed + counts[MutantOutcome.SURVIVED]
    return killed / decided if decided else None


@dataclass(frozen=True)
class PropertyGrade:
    prop: Property
    outcome: GradeOutcome
    reason: str
    check: PropertyOutcome | None = None  # the check outcome on the real code
    k: int | None = None  # the bound the real-code verdict settled at
    readings: tuple[MutantReading, ...] = ()
    grading: Grading | None = None

    def counts(self) -> dict[str, int]:
        """Per-outcome tally; every `MutantOutcome` key present (0 if none)."""
        return {o.value: n for o, n in tally(self.readings).items()}

    @property
    def kill_rate(self) -> float | None:
        return kill_rate_of(self.readings)

    @property
    def esbmc_time_s(self) -> float:
        return math.fsum(r.esbmc_time_s for r in self.readings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.prop.property_id,
            "kind": self.prop.kind.value,
            "expression": self.prop.expression,
            "domain": list(self.prop.domain),
            "status": self.prop.status.value,
            "outcome": self.outcome.value,
            "stored": self.outcome is GradeOutcome.GRADED,
            "check": self.check.value if self.check is not None else None,
            "k": self.k,
            "counts": self.counts(),
            "kill_rate": self.kill_rate,
            "grading": self.grading.to_dict() if self.grading is not None else None,
            "reason": self.reason,
            "esbmc_time_s": self.esbmc_time_s,
            "mutants": [r.to_dict() for r in self.readings],
        }


RunOutcome = Literal["graded", "partial", "ungraded"]


@dataclass(frozen=True)
class GradeRun:
    """The graded (or not) properties of one unit; `to_dict` is the JSON shape."""

    unit_id: str
    properties: tuple[PropertyGrade, ...]
    unwind: int
    unwind_ladder: tuple[int, ...]
    max_len: int | None
    generated: int = 0  # valid mutants
    dropped: int = 0  # candidates the validity check rejected
    equivalence_k: int | None = None
    equivalence_max_len: int | None = None
    equivalent: tuple[str, ...] = ()
    equivalence_undecided: tuple[str, ...] = ()
    original_refusal: str | None = None
    baseline_esbmc_time_s: float = 0.0
    equivalence_esbmc_time_s: float = 0.0
    limitations: tuple[str, ...] = field(default=LIMITATIONS)

    def counts(self) -> dict[str, int]:
        counts = {outcome.value: 0 for outcome in GradeOutcome}
        for grade in self.properties:
            counts[grade.outcome.value] += 1
        return counts

    @property
    def outcome(self) -> RunOutcome:
        """``graded`` when every held candidate got a Grading (and there was one),
        ``partial`` when only some did, ``ungraded`` when none did (including a run
        with no held candidate): nothing graded is never a clean pass. A held
        candidate that could not be stored (it changed during grading) counts as
        eligible but ungraded."""
        counts = self.counts()
        unstored = sum(
            1
            for g in self.properties
            if g.outcome is GradeOutcome.NOT_CANDIDATE and g.check is not None
        )
        eligible = counts["graded"] + counts["undecided"] + unstored
        if eligible and counts["graded"] == eligible:
            return "graded"
        return "partial" if counts["graded"] else "ungraded"

    @property
    def mutant_esbmc_time_s(self) -> float:
        return math.fsum(g.esbmc_time_s for g in self.properties)

    @property
    def esbmc_time_s(self) -> float:
        return math.fsum(
            (
                self.baseline_esbmc_time_s,
                self.equivalence_esbmc_time_s,
                self.mutant_esbmc_time_s,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "outcome": self.outcome,
            "counts": self.counts(),
            "unwind": self.unwind,
            "unwind_ladder": list(self.unwind_ladder),
            "max_len": self.max_len,
            "mutants": {
                "generated": self.generated,
                "dropped": self.dropped,
                "graded_against": self.generated - len(self.equivalent),
            },
            "equivalence": {
                "k": self.equivalence_k,
                "max_len": self.equivalence_max_len,
                "skipped": len(self.equivalent),
                "skipped_equivalent": list(self.equivalent),
                "undecided": list(self.equivalence_undecided),
                "original_refusal": self.original_refusal,
            },
            "esbmc_time_s": {
                "baseline": self.baseline_esbmc_time_s,
                "equivalence": self.equivalence_esbmc_time_s,
                "mutants": self.mutant_esbmc_time_s,
                "total": self.esbmc_time_s,
            },
            "limitations": list(self.limitations),
            "properties": [g.to_dict() for g in self.properties],
        }


class _TimedVerify:
    """A `VerifyPort` that sums the ESBMC time of the runs it forwards."""

    def __init__(self, port: VerifyPort) -> None:
        self._port = port
        self.seconds = 0.0

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        result = self._port(source, unwind=unwind)
        self.seconds += result.meta.duration_s
        return result


class _PropertyView:
    """A `PropertyStorePort` over a fixed property list, whatever the unit id."""

    def __init__(self, props: Sequence[Property]) -> None:
        self._props = tuple(props)

    def list_for_unit(
        self, unit_id: str, statuses: object = None
    ) -> Sequence[Property]:
        return self._props


@dataclass(frozen=True)
class _Subject:
    """A kept mutant as the equivalence filter sees it (`MutantLike`)."""

    id: str
    source: str
    mutant: Mutant
    unit: Unit


def _subject(mutant: Mutant, unit: Unit, symbol: str) -> _Subject:
    renamed = Unit.from_text(unit.path, symbol, mutant.source)
    return _Subject(str(mutant.id), renamed.source_text, mutant, renamed)


def mutant_reading(
    subject: _Subject, verdict: PropertyVerdict, seconds: float
) -> MutantReading:
    """Map one property verdict on a mutant to a reading (see the module docstring).

    Only a ``VIOLATED`` verdict kills and only a ``HELD`` one (post-call point
    reached) survives; ``VACUOUS`` and an unresolved verdict are ``UNKNOWN``.
    """
    outcome: MutantOutcome
    detail = verdict.skip_reason
    match verdict.outcome:
        case PropertyOutcome.VIOLATED:
            outcome = MutantOutcome.KILLED
        case PropertyOutcome.HELD:
            outcome = MutantOutcome.SURVIVED
        case PropertyOutcome.UNKNOWN | PropertyOutcome.VACUOUS:
            outcome = MutantOutcome.UNKNOWN
            if detail is None:
                detail = verdict.outcome.value
                if isinstance(verdict.result, Unknown):
                    detail += f" ({verdict.result.reason.value})"
        case _:
            outcome = MutantOutcome.ERROR
            if isinstance(verdict.result, Error):
                detail = verdict.result.message
    return MutantReading(
        subject.id,
        subject.mutant.operator,
        subject.mutant.description,
        outcome,
        k=verdict.k,
        esbmc_time_s=seconds,
        detail=detail,
    )


def kill_reason(grade_readings: Sequence[MutantReading]) -> str:
    """The plain-English reason: the kill count, each survivor by name, then what
    was left out of the rate (``killed 7/9; survived: ...; not counted: ...``)."""
    counts = tally(grade_readings)
    survivors = [
        f"{r.description} [{r.mutant_id}]"
        for r in grade_readings
        if r.outcome is MutantOutcome.SURVIVED
    ]
    killed, survived = counts[MutantOutcome.KILLED], counts[MutantOutcome.SURVIVED]
    parts = [f"killed {killed}/{killed + survived}"]
    parts.append("survived: " + "; ".join(survivors) if survivors else "no survivors")
    left_out = [
        f"{counts[outcome]} {label}"
        for outcome, label in (
            (MutantOutcome.UNKNOWN, "unknown"),
            (MutantOutcome.ERROR, "error"),
            (MutantOutcome.SKIPPED, "skipped as equivalent"),
        )
        if counts[outcome]
    ]
    if left_out:
        parts.append("not counted: " + ", ".join(left_out))
    return "; ".join(parts)


def _undecided_reason(readings: Sequence[MutantReading], generated: int) -> str:
    counts = tally(readings)
    return (
        "no mutant was killed or survived, so there is no kill rate and the "
        f"property stays a candidate ({generated} mutants generated; "
        f"{counts[MutantOutcome.UNKNOWN]} unknown, {counts[MutantOutcome.ERROR]} "
        f"error, {counts[MutantOutcome.SKIPPED]} skipped as equivalent)"
    )


def _not_held_reason(verdict: PropertyVerdict) -> str:
    note = f": {verdict.skip_reason}" if verdict.skip_reason else ""
    return (
        f"not graded: {verdict.outcome.value} on the real code, so only held "
        f"properties are graded{note}"
    )


class _TimedEquivalence:
    """An `EquivalenceChecker` that sums the ESBMC time of the checks it forwards,
    including the filter's own original-versus-original baseline."""

    def __init__(self, checker: EquivalenceChecker) -> None:
        self._checker = checker
        self.seconds = 0.0

    def __call__(self, **kwargs: Any) -> EquivalenceResult:
        result = self._checker(**kwargs)
        if not isinstance(result, EquivalenceRefused):
            self.seconds += result.meta.duration_s
        return result


def default_mutant_source(
    *,
    max_mutants: int | None = None,
    seed: int = 0,
    timeout_s: float | None = None,
    esbmc_bin: str = "esbmc",
    extra_flags: Sequence[str] = (),
) -> MutantSource:
    timeout: dict[str, Any] = {} if timeout_s is None else {"timeout_s": timeout_s}

    def source(path: Path, symbol: str, /) -> MutationResult:
        return mutate_function(
            path,
            symbol,
            cap=max_mutants,
            seed=seed,
            esbmc_bin=esbmc_bin,
            extra_flags=extra_flags,
            **timeout,
        )

    return source


def grade_unit(
    source: Path,
    symbol: str,
    *,
    store: GradeStorePort,
    work_dir: Path,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    max_len: int | None = DEFAULT_MAX_LEN,
    max_mutants: int | None = None,
    seed: int = 0,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    extra_flags: Sequence[str] = (),
    mutants: MutantSource | None = None,
    equivalence: EquivalenceChecker | None = None,
    verify_for: Callable[[Path], VerifyPort] | None = None,
    render: HarnessWriterPort | None = None,
) -> GradeRun:
    """Grade `source`::`symbol`'s CANDIDATE properties and store the Gradings.

    The check phase first runs unchanged on the real source (same ladder, same
    vacuity probe). Only properties it reports ``HELD`` are graded; every other
    stored property is reported with the reason it was not (a non-CANDIDATE
    property is skipped, v1). With no held property no mutant is generated.
    Equivalence is checked at the top rung of the ladder (the largest bound a
    property verdict can settle at) and at the property checks' `max_len`.
    `max_mutants`/`seed` cap and fix the mutant sample (roadmap Risk 6). The
    `mutants`, `equivalence`, `verify_for` and `render` ports default to the real
    ESBMC-backed bindings. `max_len`, `max_mutants` and the ladder are validated
    before any ESBMC call.
    """
    plan = SemanticCheckPlan(
        unwind=unwind,
        unwind_ladder=unwind_ladder,
        max_len=max_len,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=extra_flags,
        render=render,
        verify_for=verify_for,
    )
    max_len = plan.max_len
    if max_mutants is not None and max_mutants < 1:
        raise ValueError(f"max_mutants must be >= 1, got {max_mutants}")
    plan.validate_ladder()
    ladder = plan.ladder
    unit = Unit.from_path(source, symbol)
    writer = plan.writer
    port_for = plan.verify_for

    stored = tuple(store.list_for_unit(unit.unit_id))
    candidates = [p for p in stored if p.status is PropertyStatus.CANDIDATE]
    baseline_timer = _TimedVerify(port_for(source))
    baseline = (
        check_properties(
            unit,
            store=_PropertyView(candidates),
            render=writer,
            verify=baseline_timer,
            work_dir=work_dir / "original",
            unwind=unwind,
            unwind_ladder=ladder,
        )
        if candidates
        else PropertyCheckRun(unit.unit_id, ())
    )
    verdicts = {v.property_id: v for v in baseline.verdicts}
    held = [
        p for p in candidates if verdicts[p.property_id].outcome is PropertyOutcome.HELD
    ]
    held_ids = {p.property_id for p in held}

    def run(properties: dict[str, PropertyGrade], **fields: Any) -> GradeRun:
        return GradeRun(
            unit.unit_id,
            tuple(properties[p.property_id] for p in stored),
            unwind,
            ladder,
            max_len,
            baseline_esbmc_time_s=baseline_timer.seconds,
            **fields,
        )

    grades: dict[str, PropertyGrade] = {}
    for prop in stored:
        if prop.status is not PropertyStatus.CANDIDATE:
            grades[prop.property_id] = PropertyGrade(
                prop,
                GradeOutcome.NOT_CANDIDATE,
                f"skipped: status is {prop.status.value}; v1 grades candidate "
                "properties only",
            )
        elif prop.property_id not in held_ids:
            verdict = verdicts[prop.property_id]
            grades[prop.property_id] = PropertyGrade(
                prop,
                GradeOutcome.NOT_HELD,
                _not_held_reason(verdict),
                check=verdict.outcome,
                k=verdict.k,
            )
    if not held:
        return run(grades)

    mutant_source = mutants or default_mutant_source(
        max_mutants=max_mutants,
        seed=seed,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=extra_flags,
    )
    generated = mutant_source(source, symbol)
    subjects = [_subject(m, unit, symbol) for m in generated.mutants]
    eq_k = ladder[-1] if ladder else unwind
    eq_max_len = DEFAULT_MAX_LEN if max_len is None else max_len
    checker = _TimedEquivalence(
        equivalence
        or partial(
            check_equivalence,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            extra_flags=(*extra_flags, f"-I{source.resolve().parent}"),
        )
    )
    filtered = filter_equivalent(
        unit.source_text,
        subjects,
        eq_k,
        symbol=unit.symbol,
        max_len=eq_max_len,
        checker=checker,
    )
    equivalent_ids = {s.id for s in filtered.skipped_equivalent}

    readings: dict[str, list[MutantReading]] = {p.property_id: [] for p in held}
    for index, subject in enumerate(subjects):
        if subject.id in equivalent_ids:
            skipped = MutantReading(
                subject.id,
                subject.mutant.operator,
                subject.mutant.description,
                MutantOutcome.SKIPPED,
                detail="equivalent to the original up to the bound",
            )
            for prop in held:
                readings[prop.property_id].append(skipped)
            continue
        for prop in held:
            timer = _TimedVerify(port_for(source))
            got = check_properties(
                subject.unit,
                store=_PropertyView([prop]),
                render=writer,
                verify=timer,
                work_dir=work_dir / f"m{index}",
                unwind=unwind,
                unwind_ladder=ladder,
            )
            readings[prop.property_id].append(
                mutant_reading(subject, got.verdicts[0], timer.seconds)
            )

    for prop in held:
        grades[prop.property_id] = _finish(
            prop,
            verdicts[prop.property_id],
            tuple(readings[prop.property_id]),
            store,
            len(subjects),
        )
    return run(
        grades,
        generated=len(subjects),
        dropped=generated.dropped,
        equivalence_k=eq_k,
        equivalence_max_len=eq_max_len,
        equivalent=tuple(s.id for s in filtered.skipped_equivalent),
        equivalence_undecided=filtered.undecided,
        original_refusal=filtered.original_refusal,
        equivalence_esbmc_time_s=checker.seconds,
    )


def _finish(
    prop: Property,
    verdict: PropertyVerdict,
    readings: tuple[MutantReading, ...],
    store: GradeStorePort,
    generated: int,
) -> PropertyGrade:
    rate = kill_rate_of(readings)
    if rate is None:
        return PropertyGrade(
            prop,
            GradeOutcome.UNDECIDED,
            _undecided_reason(readings, generated),
            verdict.outcome,
            verdict.k,
            readings,
        )
    reason = kill_reason(readings)
    grading = Grading(GradingVerdict.HELD, rate, reason)
    try:
        store.grade(prop.property_id, grading)
    except (InvalidStatusTransition, PropertyNotFound) as exc:
        return PropertyGrade(
            prop,
            GradeOutcome.NOT_CANDIDATE,
            f"not stored: the property changed during grading ({exc})",
            verdict.outcome,
            verdict.k,
            readings,
        )
    return PropertyGrade(
        prop, GradeOutcome.GRADED, reason, verdict.outcome, verdict.k, readings, grading
    )


def trace_rows(run: GradeRun) -> list[tuple[str, dict[str, Any]]]:
    """Event-trace rows `(type, fields)`: the run, then each property, then each
    (property, mutant) result. Flat and JSON-native; pure, so testable without a
    store."""
    unit = {"unit_id": run.unit_id}
    rows: list[tuple[str, dict[str, Any]]] = [
        (
            RUN_EVENT,
            {
                **unit,
                "outcome": run.outcome,
                **{f"properties_{k}": v for k, v in run.counts().items()},
                "mutants": run.generated,
                "mutants_dropped": run.dropped,
                "mutants_equivalent": len(run.equivalent),
                "equivalence_k": run.equivalence_k,
                "esbmc_time_s": run.esbmc_time_s,
            },
        )
    ]
    for grade in run.properties:
        prop = {**unit, "property_id": grade.prop.property_id}
        rows.append(
            (
                PROPERTY_EVENT,
                {
                    **prop,
                    "outcome": grade.outcome.value,
                    "check": grade.check.value if grade.check else None,
                    **grade.counts(),
                    "kill_rate": grade.kill_rate,
                    "esbmc_time_s": grade.esbmc_time_s,
                },
            )
        )
        rows += [
            (
                MUTANT_EVENT,
                {
                    **prop,
                    "mutant_id": r.mutant_id,
                    "operator": r.operator,
                    "outcome": r.outcome.value,
                    "k": r.k,
                    "esbmc_time_s": r.esbmc_time_s,
                },
            )
            for r in grade.readings
        ]
    return rows


def record_trace(store_root: Path, run: GradeRun) -> None:
    """Append `trace_rows(run)` to `<store_root>/events.jsonl` (best-effort)."""
    for event_type, fields in trace_rows(run):
        record_event(store_root, event_type, **fields)


def grade_transcript(run: GradeRun) -> str:
    """Human-readable report: one block per property, then the bounds in force."""
    lines = [f"grade {run.unit_id}: {run.outcome}"]
    for grade in run.properties:
        counts = grade.counts()
        head = f"- {grade.prop.expression}  [{grade.outcome.value}]"
        if grade.kill_rate is not None:
            head += f"  kill rate {grade.kill_rate:.2f}"
        lines.append(head)
        lines.append(
            f"    killed {counts['killed']}, survived {counts['survived']}, "
            f"unknown {counts['unknown']}, error {counts['error']}, "
            f"skipped {counts['skipped']}"
        )
        lines.append(f"    {grade.reason}")
    lines.append(
        f"mutants: {run.generated} generated, {run.dropped} dropped as invalid, "
        f"{len(run.equivalent)} skipped as equivalent (k={run.equivalence_k})"
    )
    if run.original_refusal:
        lines.append(
            f"equivalence filter off: original not modelled ({run.original_refusal})"
        )
    lines.append(f"ESBMC time: {run.esbmc_time_s:.1f}s")
    lines += [f"note: {text}" for text in run.limitations]
    return "\n".join(lines)
