"""Bug-pair discrimination scorer (RFC-0007 M2 and M4, #394).

Given two sources for one ``symbol`` -- a buggy version and its fixed twin -- and a
set of properties, report which properties *discriminate*: non-vacuously ``HELD``
on the fixed version and ``VIOLATED`` on the buggy one. A pair is *discriminated*
when at least one property does.

Usable-pair filter first (RFC-0007 Tier 0). A bug pair measures a property set only
when the built-in safety checks (S0) cannot already see the bug, so S0 runs before
any property is rendered. A pair is *excluded*, with the reason, instead of scored
when S0 fails the clean version, S0 catches the bug, or S0 is inconclusive on
either. An excluded pair never reaches property checking.

Verdict vocabulary: ESBMC emits no proof object. ``HELD`` means no violation found
up to the bound ``k`` (and ``length_bounds``) recorded on each verdict, never "holds
for all inputs". ``UNKNOWN``, ``VACUOUS``, ``ERROR`` or ``SKIPPED`` on either version
leaves that property's discrimination ``inconclusive``; it is never read as a pass
or as "does not discriminate".

M4, domain absorption. When the pair carries a known triggering input (scalar
``name -> int`` assignments), a property that is ``HELD`` on the buggy version while
that input lies outside its domain holds there only because its domain excludes the
bug. Whether the input satisfies a domain clause is decided by ESBMC on a tiny
concrete C program (exact C semantics), not by re-implementing C in Python. Not
detected: a domain exclusion that is legitimate (undefined behaviour). The
usable-pair filter narrows this, because S0 passes the buggy version.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from forseti._events import record_event
from forseti.esbmc import Verified, Violated
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
from forseti.orchestrator.semantic_check import (
    esbmc_verify_for as esbmc_verify_for,
)
from forseti.precond import DEFAULT_MAX_LEN, Assessment, DischargeResult
from forseti.precond import discharge_precondition as _discharge_precondition
from forseti.precond.verify import sidecar_verify_port
from forseti.properties import (
    DEFAULT_INCLUDES,
    HarnessError,
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    ScalarParam,
    UnitSignature,
    extract_signature,
    make_property_id,
)

SAFETY_FLAGS: tuple[str, ...] = ("--overflow-check",)
SAFETY_TIMEOUT_S = 60.0

PAIR_EVENT = "discrimination.pair"
PROPERTY_EVENT = "discrimination.property"

_DOMAIN_LABEL = "forseti:domain"
_INT_MIN = -(2**63)
_INT_MAX = 2**64 - 1


class SafetyOutcome(Enum):
    PASSED = "passed"
    VIOLATED = "violated"
    INCONCLUSIVE = "inconclusive"  # unknown, vacuous, needs a contract, or an error


@dataclass(frozen=True)
class SafetyReading:
    """One version's S0 reading. `assumed` marks a pass that rests on an
    undischarged caller-pointer precondition (RFC-0003 S2)."""

    outcome: SafetyOutcome
    detail: str
    assessment: str | None = None
    assumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "assessment": self.assessment,
            "assumed": self.assumed,
            "detail": self.detail,
        }


class SafetyPort(Protocol):
    """S0 on one version: the built-in safety checks for `symbol` in `source`."""

    def __call__(self, source: Path, symbol: str, /) -> SafetyReading: ...


class ExclusionReason(Enum):
    S0_CATCHES_BUG = "s0_catches_bug"  # clean passes, the buggy twin fails S0
    S0_FAILS_CLEAN = "s0_fails_clean"  # S0 is noise for this unit
    S0_UNKNOWN = "s0_unknown"  # S0 is inconclusive on a version


@dataclass(frozen=True)
class Exclusion:
    reason: ExclusionReason
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


class Discrimination(Enum):
    DISCRIMINATES = "discriminates"
    NOT_DISCRIMINATING = "not_discriminating"
    INCONCLUSIVE = "inconclusive"


class Absorption(Enum):
    """M4 reading for one property against the pair's known triggering input."""

    NOT_APPLICABLE = "not_applicable"  # no trigger, or not HELD on the buggy version
    ABSORBED = "absorbed"  # the trigger lies outside the property's domain
    IN_DOMAIN = "in_domain"  # the trigger satisfies every domain clause
    UNDETERMINED = "undetermined"  # a clause could not be evaluated on the trigger


class PairOutcome(Enum):
    EXCLUDED = "excluded"
    DISCRIMINATED = "discriminated"
    NOT_DISCRIMINATED = "not_discriminated"
    INCONCLUSIVE = "inconclusive"
    EMPTY = "empty"  # usable pair, but no property to score


Trigger = tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class PropertyScore:
    prop: Property
    fixed: PropertyVerdict
    buggy: PropertyVerdict
    discrimination: Discrimination
    reason: str
    absorption: Absorption = Absorption.NOT_APPLICABLE
    absorption_detail: str | None = None

    @property
    def discriminates(self) -> bool:
        return self.discrimination is Discrimination.DISCRIMINATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.prop.property_id,
            "kind": self.prop.kind.value,
            "expression": self.prop.expression,
            "domain": list(self.prop.domain),
            "provenance": self.prop.provenance.to_dict(),
            "fixed": _side_dict(self.fixed),
            "buggy": _side_dict(self.buggy),
            "discrimination": self.discrimination.value,
            "discriminates": self.discriminates,
            "reason": self.reason,
            "absorption": self.absorption.value,
            "absorption_detail": self.absorption_detail,
        }


@dataclass(frozen=True)
class PairScore:
    """The scored (or excluded) pair; `to_dict` is the JSON wire shape."""

    symbol: str
    buggy_path: Path
    fixed_path: Path
    buggy_unit_id: str
    fixed_unit_id: str
    s0_fixed: SafetyReading
    s0_buggy: SafetyReading | None  # None: not run, the clean version already failed S0
    exclusion: Exclusion | None
    scores: tuple[PropertyScore, ...] = ()
    trigger: Trigger | None = None

    @property
    def usable(self) -> bool:
        return self.exclusion is None

    @property
    def outcome(self) -> PairOutcome:
        if self.exclusion is not None:
            return PairOutcome.EXCLUDED
        if not self.scores:
            return PairOutcome.EMPTY
        if any(s.discriminates for s in self.scores):
            return PairOutcome.DISCRIMINATED
        if any(s.discrimination is Discrimination.INCONCLUSIVE for s in self.scores):
            return PairOutcome.INCONCLUSIVE
        return PairOutcome.NOT_DISCRIMINATED

    @property
    def discriminating(self) -> tuple[PropertyScore, ...]:
        return tuple(s for s in self.scores if s.discriminates)

    def counts(self) -> dict[str, dict[str, int]]:
        """Per-discrimination and per-absorption tallies, every key present."""
        discrimination = {d.value: 0 for d in Discrimination}
        absorption = {a.value: 0 for a in Absorption}
        for score in self.scores:
            discrimination[score.discrimination.value] += 1
            absorption[score.absorption.value] += 1
        return {"discrimination": discrimination, "absorption": absorption}

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "buggy": {"source": str(self.buggy_path), "unit_id": self.buggy_unit_id},
            "fixed": {"source": str(self.fixed_path), "unit_id": self.fixed_unit_id},
            "s0": {
                "fixed": self.s0_fixed.to_dict(),
                "buggy": self.s0_buggy.to_dict() if self.s0_buggy else None,
            },
            "usable": self.usable,
            "exclusion": self.exclusion.to_dict() if self.exclusion else None,
            "outcome": self.outcome.value,
            "trigger": dict(self.trigger) if self.trigger is not None else None,
            "counts": self.counts(),
            "properties": [s.to_dict() for s in self.scores],
        }


def reading_from_discharge(result: DischargeResult) -> SafetyReading:
    """Map an S2/S3 assessment to an S0 reading; only a pass or a real violation
    is decisive, everything else is inconclusive."""
    assessment = result.assessment
    match assessment:
        case Assessment.DISCHARGED_VERIFIED | Assessment.ASSUMED_VERIFIED:
            outcome = SafetyOutcome.PASSED
        case Assessment.VIOLATED:
            outcome = SafetyOutcome.VIOLATED
        case _:
            outcome = SafetyOutcome.INCONCLUSIVE
    return SafetyReading(
        outcome,
        result.label,
        assessment=assessment.value,
        assumed=assessment is Assessment.ASSUMED_VERIFIED,
    )


def esbmc_safety_port(
    *,
    max_len: int = DEFAULT_MAX_LEN,
    timeout_s: float = SAFETY_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
) -> SafetyPort:
    """S0 over the memory-precondition discharge (RFC-0003) plus signed overflow.

    The sidecar harness materialises pointer arguments, so a pointer unit gets a
    meaningful memory verdict; a scalar unit gets the overflow check.
    """

    def port(source: Path, symbol: str, /) -> SafetyReading:
        raw = sidecar_verify_port(
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            extra_flags=(*SAFETY_FLAGS, f"-I{source.resolve().parent}"),
        )
        result = _discharge_precondition(
            source,
            function=symbol,
            max_len=max_len,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            raw_verify=raw,
        )
        return reading_from_discharge(result)

    return port


def exclusion_for(
    fixed: SafetyReading, buggy: SafetyReading | None
) -> Exclusion | None:
    """The Tier 0 usable-pair decision, in a fixed precedence: a definite failure
    of the clean version, then any inconclusive reading, then the bug being caught."""
    if fixed.outcome is SafetyOutcome.VIOLATED:
        return Exclusion(
            ExclusionReason.S0_FAILS_CLEAN,
            f"S0 fails the clean version: {fixed.detail}",
        )
    if fixed.outcome is SafetyOutcome.INCONCLUSIVE:
        return Exclusion(
            ExclusionReason.S0_UNKNOWN,
            f"S0 is inconclusive on the clean version: {fixed.detail}",
        )
    assert buggy is not None  # S0 runs on the buggy version once the clean passes
    if buggy.outcome is SafetyOutcome.VIOLATED:
        return Exclusion(
            ExclusionReason.S0_CATCHES_BUG,
            f"S0 already catches the bug: {buggy.detail}",
        )
    if buggy.outcome is SafetyOutcome.INCONCLUSIVE:
        return Exclusion(
            ExclusionReason.S0_UNKNOWN,
            f"S0 is inconclusive on the buggy version: {buggy.detail}",
        )
    return None


def classify(
    fixed: PropertyVerdict, buggy: PropertyVerdict
) -> tuple[Discrimination, str]:
    """Discrimination of one property from its two verdicts (see module docstring)."""
    if fixed.outcome is PropertyOutcome.VIOLATED:
        return (
            Discrimination.NOT_DISCRIMINATING,
            "violated on the fixed version (a false alarm, or a bug there too)",
        )
    if fixed.outcome is PropertyOutcome.HELD:
        if buggy.outcome is PropertyOutcome.VIOLATED:
            return (
                Discrimination.DISCRIMINATES,
                "held on the fixed version and violated on the buggy one",
            )
        if buggy.outcome is PropertyOutcome.HELD:
            return Discrimination.NOT_DISCRIMINATING, "holds on both versions"
    return Discrimination.INCONCLUSIVE, _inconclusive_reason(fixed, buggy)


def _inconclusive_reason(fixed: PropertyVerdict, buggy: PropertyVerdict) -> str:
    parts = [f"fixed: {fixed.outcome.value}", f"buggy: {buggy.outcome.value}"]
    notes = [v.skip_reason for v in (fixed, buggy) if v.skip_reason]
    return "; ".join(parts + notes)


class _FixedPropertySet:
    """A `PropertyStorePort` that serves one property set for any unit id, so the
    same properties are rendered against both versions (their unit ids differ)."""

    def __init__(self, props: Sequence[Property]) -> None:
        self._props = tuple(props)

    def list_for_unit(
        self, unit_id: str, statuses: Collection[PropertyStatus] | None = None
    ) -> Sequence[Property]:
        return self._props


def score_pair(
    *,
    buggy: Path,
    fixed: Path,
    symbol: str,
    properties: Sequence[Property],
    safety: SafetyPort | None = None,
    verify_for: Callable[[Path], VerifyPort] | None = None,
    render: HarnessWriterPort | None = None,
    work_dir: Path,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    max_len: int | None = DEFAULT_MAX_LEN,
    trigger: Mapping[str, int] | None = None,
    esbmc_bin: str = "esbmc",
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
) -> PairScore:
    """Score `properties` on the (`buggy`, `fixed`) pair of `symbol`.

    S0 runs on the clean version first and on the buggy one only if the clean
    passes; an excluded pair returns before any property is rendered or verified.
    Otherwise every property is rendered against each version (`render`, default
    `SemanticHarnessWriter`) and checked with `check_properties` unchanged, so the
    ladder, vacuity probe and HELD/VACUOUS rules are the check phase's own.
    `trigger` (scalar `name -> int`) enables the M4 absorption reading and is
    validated before any ESBMC call, as are `max_len` and the unwind ladder (derived
    as `check_source` does when `unwind_ladder` is None). `safety` and `verify_for`
    default to the real ESBMC bindings (`esbmc_safety_port`, `esbmc_verify_for`);
    the default S0 uses the same `max_len` as the property checks.
    """
    fixed_unit = Unit.from_path(fixed, symbol)
    buggy_unit = Unit.from_path(buggy, symbol)
    plan = SemanticCheckPlan(
        unwind=unwind,
        unwind_ladder=unwind_ladder,
        max_len=max_len,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        render=render,
        verify_for=verify_for,
    )
    plan.validate_ladder()
    pair_trigger, signature = _validated_trigger(trigger, buggy_unit)

    safety = safety or esbmc_safety_port(
        max_len=DEFAULT_MAX_LEN if max_len is None else max_len,
        esbmc_bin=esbmc_bin,
    )
    s0_fixed = safety(fixed, symbol)
    s0_buggy = (
        safety(buggy, symbol) if s0_fixed.outcome is SafetyOutcome.PASSED else None
    )
    exclusion = exclusion_for(s0_fixed, s0_buggy)

    def pair(
        excluded: Exclusion | None, scores: tuple[PropertyScore, ...] = ()
    ) -> PairScore:
        return PairScore(
            symbol=symbol,
            buggy_path=buggy,
            fixed_path=fixed,
            buggy_unit_id=buggy_unit.unit_id,
            fixed_unit_id=fixed_unit.unit_id,
            s0_fixed=s0_fixed,
            s0_buggy=s0_buggy,
            exclusion=excluded,
            scores=scores,
            trigger=pair_trigger,
        )

    if exclusion is not None:
        return pair(exclusion)

    verify_for = plan.verify_for
    writer = plan.writer
    store = _FixedPropertySet(properties)

    def run_on(unit: Unit, source: Path, subdir: str) -> PropertyCheckRun:
        return check_properties(
            unit,
            store=store,
            render=writer,
            verify=verify_for(source),
            work_dir=work_dir / subdir,
            unwind=unwind,
            unwind_ladder=plan.ladder,
        )

    fixed_run = run_on(fixed_unit, fixed, "fixed")
    buggy_run = run_on(buggy_unit, buggy, "buggy")

    domain_verify = verify_for(buggy) if pair_trigger is not None else None
    scores = []
    for index, (prop, fixed_verdict, buggy_verdict) in enumerate(
        zip(properties, fixed_run.verdicts, buggy_run.verdicts, strict=True)
    ):
        discrimination, reason = classify(fixed_verdict, buggy_verdict)
        absorption, detail = _absorption(
            prop,
            buggy_verdict,
            pair_trigger,
            verify=domain_verify,
            signature=signature,
            path=work_dir / "domain" / f"p{index}",
        )
        scores.append(
            PropertyScore(
                prop,
                fixed_verdict,
                buggy_verdict,
                discrimination,
                reason,
                absorption,
                detail,
            )
        )
    return pair(None, tuple(scores))


def _validated_trigger(
    trigger: Mapping[str, int] | None, unit: Unit
) -> tuple[Trigger | None, UnitSignature | None]:
    if trigger is None:
        return None, None
    try:
        signature = extract_signature(unit.source_text, unit.symbol)
    except HarnessError as exc:
        raise ValueError(
            f"cannot read the signature to place the trigger: {exc}"
        ) from exc
    scalars = {p.name for p in signature.params if isinstance(p, ScalarParam)}
    missing = scalars - trigger.keys()
    if missing:
        raise ValueError(
            f"trigger must give a value for every scalar parameter of "
            f"{unit.symbol}; missing {sorted(missing)}"
        )
    for name, value in trigger.items():
        if name not in scalars:
            raise ValueError(
                f"trigger names {name!r}, which is not a scalar parameter of "
                f"{unit.symbol} (scalars: {sorted(scalars)})"
            )
        if not _INT_MIN <= value <= _INT_MAX:
            raise ValueError(f"trigger value for {name!r} is out of range: {value}")
    return tuple(sorted(trigger.items())), signature


def _c_int(value: int) -> str:
    return f"{value}ULL" if value > 2**63 - 1 else f"({value}LL)"


def _domain_program(signature: UnitSignature, trigger: Trigger, clause: str) -> str:
    ctypes = {p.name: p.ctype for p in signature.params if isinstance(p, ScalarParam)}
    lines = [f"#include <{header}>" for header in DEFAULT_INCLUDES]
    lines.append("int main(void) {")
    lines += [
        f"  {ctypes[name]} {name} = ({ctypes[name]}){_c_int(value)};"
        for name, value in trigger
    ]
    lines += [
        f'  __ESBMC_assert(({clause}), "{_DOMAIN_LABEL}");',
        "  return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _is_domain_violation(result: Violated) -> bool:
    """Only the clause's own assertion counts: undefined behaviour while
    evaluating it (division by zero, overflow) is a different violated property."""
    cex = result.counterexample
    return cex is not None and cex.violated_property.description == _DOMAIN_LABEL


def _absorption(
    prop: Property,
    buggy: PropertyVerdict,
    trigger: Trigger | None,
    *,
    verify: VerifyPort | None,
    signature: UnitSignature | None,
    path: Path,
) -> tuple[Absorption, str | None]:
    if (
        trigger is None
        or signature is None
        or verify is None
        or buggy.outcome is not PropertyOutcome.HELD
    ):
        return Absorption.NOT_APPLICABLE, None
    inputs = ", ".join(f"{name}={value}" for name, value in trigger)
    path.mkdir(parents=True, exist_ok=True)
    unresolved: list[str] = []
    for index, clause in enumerate(prop.domain):
        program = path / f"clause{index}.c"
        program.write_text(_domain_program(signature, trigger, clause))
        match verify(program, unwind=1):
            case Violated() as violated if _is_domain_violation(violated):
                return (
                    Absorption.ABSORBED,
                    f"the triggering input ({inputs}) lies outside the domain "
                    f"clause {clause!r}: the property holds on the buggy version "
                    "only because its domain excludes the bug",
                )
            case Verified():
                pass
            case _:
                unresolved.append(clause)
    if unresolved:
        return (
            Absorption.UNDETERMINED,
            f"could not evaluate {unresolved!r} on the triggering input ({inputs})",
        )
    return (
        Absorption.IN_DOMAIN,
        f"the triggering input ({inputs}) satisfies the domain"
        if prop.domain
        else "the property has no domain",
    )


def _side_dict(verdict: PropertyVerdict) -> dict[str, Any]:
    result = verdict.result
    violated = None
    if isinstance(result, Violated) and result.counterexample is not None:
        violated = result.counterexample.violated_property.description
    return {
        "outcome": verdict.outcome.value,
        "k": verdict.k,
        "probe": verdict.probe.value if verdict.probe is not None else None,
        "length_bounds": dict(verdict.length_bounds),
        "reason": verdict.skip_reason,
        "violated_property": violated,
    }


def trace_rows(score: PairScore) -> list[tuple[str, dict[str, Any]]]:
    """Event-trace rows `(type, fields)`: one pair row, then one per property.

    Fields are flat and JSON-native, written through `record_trace`. Pure, so the
    rows are testable without a store.
    """
    pair = {
        "symbol": score.symbol,
        "buggy_unit_id": score.buggy_unit_id,
        "fixed_unit_id": score.fixed_unit_id,
    }
    rows: list[tuple[str, dict[str, Any]]] = [
        (
            PAIR_EVENT,
            {
                **pair,
                "outcome": score.outcome.value,
                "exclusion": score.exclusion.reason.value if score.exclusion else None,
                "s0_fixed": score.s0_fixed.outcome.value,
                "s0_buggy": score.s0_buggy.outcome.value if score.s0_buggy else None,
                "properties": len(score.scores),
                "discriminating": len(score.discriminating),
            },
        )
    ]
    rows += [
        (
            PROPERTY_EVENT,
            {
                **pair,
                "property_id": s.prop.property_id,
                "fixed": s.fixed.outcome.value,
                "buggy": s.buggy.outcome.value,
                "fixed_k": s.fixed.k,
                "buggy_k": s.buggy.k,
                "discrimination": s.discrimination.value,
                "absorption": s.absorption.value,
            },
        )
        for s in score.scores
    ]
    return rows


def record_trace(store_root: Path, score: PairScore) -> None:
    """Append `trace_rows(score)` to `<store_root>/events.jsonl` (best-effort)."""
    for event_type, fields in trace_rows(score):
        record_event(store_root, event_type, **fields)


class PropertySetError(ValueError):
    """A property-set file is not a readable list of properties."""


def properties_from_data(data: object, *, unit_id: str) -> tuple[Property, ...]:
    """Properties from decoded JSON: a list of property objects, or the object
    `propose --json` / `submit-property --json` print (its ``accepted`` list), or
    one with a ``properties`` list. Each needs an ``expression``; ``kind`` defaults
    to semantic, ``domain`` to empty, and a missing ``property_id`` is derived from
    the content under `unit_id`."""
    items = data
    if isinstance(data, Mapping):
        items = data.get("accepted", data.get("properties"))
    if not isinstance(items, list):
        raise PropertySetError(
            "expected a list of properties, or an object with an 'accepted' "
            "or 'properties' list"
        )
    return tuple(_property_from_obj(obj, unit_id, i) for i, obj in enumerate(items))


def load_properties_file(path: Path, *, unit_id: str) -> tuple[Property, ...]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PropertySetError(f"cannot read property set {path}: {exc}") from exc
    return properties_from_data(data, unit_id=unit_id)


def _property_from_obj(obj: object, unit_id: str, index: int) -> Property:
    where = f"property #{index}"
    if not isinstance(obj, Mapping):
        raise PropertySetError(f"{where} is not an object")
    expression = obj.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise PropertySetError(f"{where} has no 'expression'")
    domain = obj.get("domain", [])
    if not isinstance(domain, list) or not all(isinstance(c, str) for c in domain):
        raise PropertySetError(f"{where} has a 'domain' that is not a list of strings")
    try:
        kind = PropertyKind(obj.get("kind", PropertyKind.SEMANTIC.value))
    except ValueError as exc:
        raise PropertySetError(f"{where} has an unknown kind: {exc}") from exc
    own_unit = obj.get("unit_id")
    unit = own_unit if isinstance(own_unit, str) else unit_id
    given_id = obj.get("property_id")
    property_id = (
        given_id
        if isinstance(given_id, str) and given_id
        else make_property_id(unit, kind, expression, tuple(domain))
    )
    return Property(
        property_id=property_id,
        unit_id=unit,
        kind=kind,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=_provenance_from(obj.get("provenance")),
        domain=tuple(domain),
    )


def _provenance_from(raw: object) -> Provenance:
    """The proposer attribution a `propose --json` run file carries, if any; kept
    so a downstream report can aggregate per recipe and model."""
    if not isinstance(raw, Mapping):
        return Provenance("", "")

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    return Provenance(
        text("prompt_id"), text("prompt_version"), text("provider"), text("model")
    )
