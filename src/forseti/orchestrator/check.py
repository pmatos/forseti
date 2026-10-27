"""The `check_properties` driver: which generated properties hold on real code (#66).

The W2.5 *check* phase of the spine. Given a verification unit and the #62 store,
it loads that unit's candidate properties, renders each to a self-contained ESBMC
harness via #64, verifies it (escalating along the shared UNKNOWN k-ladder), and
records a per-property verdict (ADR-0009 D4: `held / violated / unknown`, plus
`error`/`skipped` so a bad harness or a deferred kind is never silently dropped —
CLAUDE.md "never silently pass").

This is a *sibling* of `run_loop`, not a change to it: `run_loop` maps one source
to one terminal state and *fixes* on a violation; `check_properties` maps one unit
+ N properties to N verdicts and does **no** fixing (a `VIOLATED` property is a
result to record — the code violates that generated property). Mutation/kill-rate
*scoring* stays in the grading epic (#4); proposing stays in #65; the CLI/MCP
`propose` face stays in #44. This module owns only the check phase and its typed
result, `PropertyCheckRun`, which #4 consumes (its `held()` subset is the
mutation-kill candidate set) and #44 serializes.

The driver is deterministic and effect-free in itself — verification, storage, and
harness rendering live behind the `VerifyPort` / `PropertyStorePort` /
`HarnessWriterPort` seams; the only I/O it performs is materializing each harness
to `work_dir` (the file esbmc reads) and emitting telemetry through the injected
sink (default `NullSink` = no-op).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Literal, assert_never

from forseti.esbmc import (
    Error,
    EsbmcResult,
    Unknown,
    Verified,
    Violated,
    result_to_dict,
)
from forseti.precond.reachability import ProbeReachability, classify_site_probe
from forseti.properties import (
    CHECKABLE_STATUSES,
    NON_VACUITY_LABEL,
    HarnessError,
    LengthBounds,
    Property,
    PropertyKind,
    build_property_harness,
)

from .ladder import LadderAttempt, climb_to_terminal, validated_ladder
from .ports import (
    HarnessWriterPort,
    PropertyStorePort,
    RenderedHarness,
    Unit,
    VerifyPort,
)
from .telemetry import EventEmitter, EventSink


class PropertyOutcome(Enum):
    """Per-property verdict (ADR-0009 D4).

    `HELD`/`VIOLATED`/`UNKNOWN` mirror the ESBMC verdict on the real unit;
    `ERROR` (a tooling/invocation failure) and `SKIPPED` (a reachability kind,
    deferred per ADR-0009 D2) are added so neither is silently coerced into a
    code verdict. `VACUOUS` (W3.4) is added the same way: ESBMC said VERIFIED but
    the post-call vacuity probe could not reach the assertion at that k, so the
    postcondition was never evaluated and the result is not a HELD.
    """

    HELD = "held"  # Verified up to k
    VIOLATED = "violated"  # counterexample found
    UNKNOWN = "unknown"  # inconclusive after the ladder is exhausted
    ERROR = "error"  # esbmc failed (e.g. a harness that didn't compile)
    SKIPPED = "skipped"  # reachability kind, deferred (ADR-0009 D2)
    VACUOUS = "vacuous"  # Verified, but the post-call point is unreachable at k


@dataclass(frozen=True)
class PropertyVerdict:
    """One property's outcome on the real unit — the atom grading (#4) consumes.

    `k` is the bound the verdict settled at (`None` when `SKIPPED`); `result` is
    the raw typed ESBMC result (`None` when `SKIPPED`); `harness_source` is the
    exact harness checked, kept as provenance so #4 can re-render it against
    mutants. `skip_reason` explains a `SKIPPED` outcome, a render-failure `ERROR`,
    or a vacuity-probe `VACUOUS`/`UNKNOWN`. `probe` is the post-call probe's
    reading (`None` when no probe ran: anything other than a Verified verdict);
    on a `VACUOUS`/probe-`UNKNOWN` outcome `result` is the Verified verdict
    / the probe's own inconclusive result respectively, both at `k`.
    `length_bounds` are the ``(param, max_len)`` caps the harness applied to
    buffer lengths the property's domain left unconstrained, so the verdict reads
    as scoped to ``len <= N`` rather than to every length (empty when none).
    """

    property_id: str
    unit_id: str
    kind: str  # "semantic" | "reachability"
    outcome: PropertyOutcome
    k: int | None
    result: EsbmcResult | None
    harness_source: str | None
    skip_reason: str | None = None
    length_bounds: LengthBounds = ()
    probe: ProbeReachability | None = None

    @classmethod
    def skipped(
        cls, property_id: str, unit_id: str, kind: str, *, reason: str
    ) -> PropertyVerdict:
        """A deferred property (a reachability kind, ADR-0009 D2): `SKIPPED`, no
        `k`/`result`/`harness_source`, carrying only the `reason` it was deferred.
        """
        return cls(
            property_id,
            unit_id,
            kind,
            PropertyOutcome.SKIPPED,
            k=None,
            result=None,
            harness_source=None,
            skip_reason=reason,
        )

    @classmethod
    def render_failed(
        cls, property_id: str, unit_id: str, kind: str, *, reason: str
    ) -> PropertyVerdict:
        """A property whose harness could not be rendered (#64 fail-loud
        `HarnessError`): `ERROR` with no `k`/`result`/`harness_source` — esbmc was
        never invoked — carrying the render-failure `reason`. Distinct from an
        esbmc-error verdict (`settled` with an `Error` result), which does have a
        harness and a result.
        """
        return cls(
            property_id,
            unit_id,
            kind,
            PropertyOutcome.ERROR,
            k=None,
            result=None,
            harness_source=None,
            skip_reason=reason,
        )

    @classmethod
    def settled(
        cls,
        property_id: str,
        unit_id: str,
        kind: str,
        outcome: PropertyOutcome,
        *,
        k: int | None,
        result: EsbmcResult,
        harness_source: str,
        length_bounds: LengthBounds = (),
        probe: ProbeReachability | None = None,
        reason: str | None = None,
    ) -> PropertyVerdict:
        """A property verified to a terminal `outcome` along the k-ladder: the
        settled bound `k`, the raw `result` (kept for #4 to re-render against
        mutants), and the exact `harness_source` checked. No `reason` — the
        outcome speaks for itself — except when the vacuity probe qualified it.
        """
        return cls(
            property_id,
            unit_id,
            kind,
            outcome,
            k=k,
            result=result,
            harness_source=harness_source,
            skip_reason=reason,
            length_bounds=length_bounds,
            probe=probe,
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict — the shape the grading harness (#4) reads."""
        return {
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "kind": self.kind,
            "outcome": self.outcome.value,
            "k": self.k,
            "harness_source": self.harness_source,
            "skip_reason": self.skip_reason,
            "probe": self.probe.value if self.probe is not None else None,
            "length_bounds": dict(self.length_bounds),
            "result": (
                _result_payload(self.result, self.k)
                if self.result is not None
                else None
            ),
        }


RunOutcome = Literal["held", "violated", "unknown", "error", "empty"]

# What a `held` (and a `vacuous`) verdict does and does not show. The probe is a
# reachability witness, not a coverage claim, so it is stated wherever a probed
# verdict is reported (transcript footer, the vacuous reason, CLI/MCP docs).
VACUITY_LIMITATION = (
    "the vacuity probe shows only that some path reaches the post-call point at "
    "this k; it does not show that k covers every path (paths cut by "
    "--no-unwinding-assertions can still hide a violation)"
)


@dataclass(frozen=True)
class PropertyCheckRun:
    """The result of checking every stored property for one unit."""

    unit_id: str
    verdicts: tuple[PropertyVerdict, ...]

    def counts(self) -> dict[str, int]:
        """Per-outcome tally; every `PropertyOutcome` key present (0 if none)."""
        counts = {outcome.value: 0 for outcome in PropertyOutcome}
        for verdict in self.verdicts:
            counts[verdict.outcome.value] += 1
        return counts

    def held(self) -> tuple[PropertyVerdict, ...]:
        """The HELD subset — the mutation-kill candidate set the #4 seam grades."""
        return tuple(v for v in self.verdicts if v.outcome is PropertyOutcome.HELD)

    @property
    def outcome(self) -> RunOutcome:
        """Core-owned, worst-outcome-wins summary across every checked property.

        `violated` > `unknown` > `error` > `empty` (nothing semantically
        checked: no property stored, or every stored one was `skipped`) >
        `held`. A `vacuous` verdict (W3.4) is folded into `unknown`: the
        property's postcondition was never evaluated, so it is as inconclusive
        as a timeout and must never read as `held`; `counts()`/`verdicts[]`
        still tell the two apart. The one field a caller — the CLI's exit
        code, the MCP `check` payload, and the composed loop (`core/loop.py`) —
        reads instead of re-deriving verdict severity itself (issue #213): an
        adapter that recomputed this from `counts()` on its own could drift from
        the CLI's own policy, or (CLAUDE.md "never silently pass") miss that
        `empty` must not read as a clean pass just because it shares `held`'s
        exit code.
        """
        return self._outcome_for_counts(self.counts())

    def _outcome_for_counts(self, counts: dict[str, int]) -> RunOutcome:
        if counts["violated"]:
            return "violated"
        if counts["unknown"] or counts["vacuous"]:
            return "unknown"
        if counts["error"]:
            return "error"
        checked = len(self.verdicts) - counts["skipped"]
        if checked == 0:
            return "empty"
        return "held"

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict (unit id, counts, outcome, and every verdict)."""
        counts = self.counts()
        return {
            "unit_id": self.unit_id,
            "counts": counts,
            "outcome": self._outcome_for_counts(counts),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
        }


class SemanticHarnessWriter:
    """The production `HarnessWriterPort`: a thin adapter over #64's writer.

    Adapts the driver's `Unit`/`RenderedHarness` carriers to
    `render_property_harness`, the properties-domain recipe that owns the harness
    knowledge (parse the signature from the slice -> project the property onto a
    `SemanticSpec` -> render). Keeping the recipe in #64 (not here) is what lets
    `check_properties` stay a pure driver that need not know how a stored
    property becomes a harness. Fail-loud: a non-renderable unit/property raises
    `HarnessError` rather than emit a silent mis-harness. Non-semantic properties
    are skipped by the driver *before* this is reached (ADR-0009 D2), so `render`
    only ever sees semantic ones.

    `max_len` (default `None`: no cap) is forwarded to the harness so a
    `(ptr, len)` buffer the property's domain leaves unbounded is capped at
    ``len <= max_len`` (#299); the caps actually applied ride back on the
    `RenderedHarness`. A negative `max_len` is a per-property render `ERROR`
    (the harness's own `HarnessError`); the Core faces reject it up front.
    """

    def __init__(self, max_len: int | None = None) -> None:
        self._max_len = max_len

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        harness = build_property_harness(
            unit_source=unit.source_text,
            symbol=unit.symbol,
            prop=prop,
            max_len=self._max_len,
        )
        return RenderedHarness(
            source_text=harness.source_text,
            length_bounds=harness.length_bounds,
            probe_source_text=harness.probe_source_text,
        )


def check_properties(
    unit: Unit,
    *,
    store: PropertyStorePort,
    render: HarnessWriterPort,
    verify: VerifyPort,
    work_dir: Path,
    unwind: int,
    unwind_ladder: tuple[int, ...] = (),
    sink: EventSink | None = None,
) -> PropertyCheckRun:
    """Check the checkable stored properties for `unit`, one verdict each.

    Only non-terminal properties are checked (`CHECKABLE_STATUSES`): a terminal
    `ACCEPTED`/`REJECTED` row is settled, so it is never rendered, verified, or
    counted into the `counts()`/`held()` set grading (#4) consumes (#84).

    For each remaining property: emit a start event, then — a reachability property is
    `SKIPPED` (ADR-0009 D2, never verified); a semantic property is rendered to a
    harness (written under `work_dir`, the file esbmc reads) and verified along
    `(unwind, *unwind_ladder)`, escalating on each `Unknown` and settling on the
    terminal verdict once the ladder resolves or is exhausted (never a silent
    pass). No fixing and no scoring happen here — a `VIOLATED` property is
    recorded as-is. Determinism: iteration follows `store.list_for_unit` order
    and `Event.seq` is monotonic.
    """
    ladder = validated_ladder(unwind, unwind_ladder)
    emit = EventEmitter(sink).emit

    work_dir.mkdir(parents=True, exist_ok=True)

    props = store.list_for_unit(unit.unit_id, CHECKABLE_STATUSES)
    emit("properties.loaded", detail={"unit": unit.unit_id, "count": len(props)})

    def record_escalation(
        attempt: LadderAttempt, *, index: int, property_id: str
    ) -> None:
        if attempt.escalate_to is not None:
            emit(
                "unknown.policy.decision",
                index=index,
                detail={
                    "decision": "escalate",
                    "property_id": property_id,
                    "from_k": attempt.k,
                    "to_k": attempt.escalate_to,
                },
            )

    verdicts: list[PropertyVerdict] = []
    for index, prop in enumerate(props):
        kind = prop.kind.value
        emit(
            "property.check.start",
            index=index,
            detail={"property_id": prop.property_id, "kind": kind},
        )
        if prop.kind is not PropertyKind.SEMANTIC:
            reason = "reachability harnessing deferred (ADR-0009 D2)"
            verdicts.append(
                PropertyVerdict.skipped(
                    prop.property_id, unit.unit_id, kind, reason=reason
                )
            )
            emit(
                "property.skipped",
                index=index,
                detail={"property_id": prop.property_id, "reason": reason},
            )
            continue

        try:
            rendered = render.render(unit, prop)
        except HarnessError as exc:
            # #64 renders fail-loud on an un-renderable property (e.g. a
            # postcondition that dereferences a scalar-backed output). Record it as
            # a per-property ERROR -- CLAUDE.md "never silently pass" and this
            # module's own contract that a bad harness is never dropped -- rather
            # than crash the whole unit's run or hand esbmc un-compilable C.
            reason = f"harness render failed: {exc}"
            verdicts.append(
                PropertyVerdict.render_failed(
                    prop.property_id, unit.unit_id, kind, reason=reason
                )
            )
            emit(
                "property.verdict",
                index=index,
                verdict=PropertyOutcome.ERROR.value,
                detail={"property_id": prop.property_id, "reason": reason},
            )
            continue
        harness_path = work_dir / _harness_filename(unit.unit_id, prop.property_id)
        harness_path.write_text(rendered.source_text)

        # Observation flushes each escalation before the next verification starts.
        final = climb_to_terminal(
            harness_path,
            verify=verify,
            ladder=ladder,
            on_attempt=partial(
                record_escalation, index=index, property_id=prop.property_id
            ),
        )
        outcome = _outcome_for(final.result)
        probed = _Probed(outcome, final.result, None, None)
        if outcome is PropertyOutcome.HELD:
            probed = _probe_post_call(
                rendered,
                harness_path=harness_path,
                verify=verify,
                k=final.k,
                verified=final.result,
            )
        verdicts.append(
            PropertyVerdict.settled(
                prop.property_id,
                unit.unit_id,
                kind,
                probed.outcome,
                k=final.k,
                result=probed.result,
                harness_source=rendered.source_text,
                length_bounds=rendered.length_bounds,
                probe=probed.probe,
                reason=probed.reason,
            )
        )
        detail: dict[str, Any] = {"property_id": prop.property_id, "kind": kind}
        if probed.probe is not None:
            detail["probe"] = probed.probe.value
        emit(
            "property.verdict",
            index=index,
            k=final.k,
            verdict=probed.outcome.value,
            detail=detail,
        )

    run = PropertyCheckRun(unit.unit_id, tuple(verdicts))
    emit("properties.checked", detail={"unit": unit.unit_id, **run.counts()})
    return run


PROBE_SUFFIX = ".probe.c"


@dataclass(frozen=True)
class _Probed:
    outcome: PropertyOutcome
    result: EsbmcResult
    probe: ProbeReachability | None
    reason: str | None


def _probe_post_call(
    rendered: RenderedHarness,
    *,
    harness_path: Path,
    verify: VerifyPort,
    k: int,
    verified: EsbmcResult,
) -> _Probed:
    """Qualify a Verified verdict: is the post-call point reachable at the same k?

    Runs the harness's probe twin once, at the bound the verdict settled at --
    never escalated, so it reads the very paths the HELD was decided on.
    REACHED keeps `HELD`; UNREACHABLE is `VACUOUS` (the postcondition was never
    evaluated); INCONCLUSIVE (the probe itself was UNKNOWN/ERROR, or fired
    without its label) is `UNKNOWN`; an Unknown/Error probe result is kept as
    the cause, but a stray probe `Violated` never replaces the property's own
    `Verified` -- the probe harness's trace is not its counterexample.
    A writer that supplies no probe also yields `UNKNOWN`: an unprobed Verified
    is never reported as HELD.
    """
    if rendered.probe_source_text is None:
        return _Probed(
            PropertyOutcome.UNKNOWN,
            verified,
            None,
            "no vacuity probe available for this harness; a Verified verdict "
            "is not reported as held without one",
        )
    probe_path = harness_path.with_name(harness_path.stem + PROBE_SUFFIX)
    probe_path.write_text(rendered.probe_source_text)
    probe_result = verify(probe_path, unwind=k)
    reading = classify_site_probe(probe_result, label=NON_VACUITY_LABEL)
    match reading:
        case ProbeReachability.REACHED:
            return _Probed(PropertyOutcome.HELD, verified, reading, None)
        case ProbeReachability.UNREACHABLE:
            return _Probed(
                PropertyOutcome.VACUOUS,
                verified,
                reading,
                f"vacuous at k={k}: ESBMC verified the harness but the post-call "
                "point is unreachable (unsatisfiable domain, or the call never "
                f"returns within k); {VACUITY_LIMITATION}",
            )
        case ProbeReachability.INCONCLUSIVE:
            return _Probed(
                PropertyOutcome.UNKNOWN,
                verified if isinstance(probe_result, Violated) else probe_result,
                reading,
                f"vacuity probe inconclusive at k={k}; held is withheld",
            )
        case _:
            assert_never(reading)


def _outcome_for(result: EsbmcResult) -> PropertyOutcome:
    """Map an ESBMC verdict to a property outcome (exhaustive over the union).

    The final `assert_never` mirrors `state.py`'s `next_state`: adding a new
    `EsbmcResult` variant becomes a type error here, so no verdict is dropped.
    """
    match result:
        case Verified():
            return PropertyOutcome.HELD
        case Violated():
            return PropertyOutcome.VIOLATED
        case Unknown():
            return PropertyOutcome.UNKNOWN
        case Error():
            return PropertyOutcome.ERROR
        case _:
            assert_never(result)


def _result_payload(result: EsbmcResult, k: int | None) -> dict[str, Any]:
    """The raw-result slice of a verdict's JSON shape — the settled `k` plus the
    shared `forseti.esbmc.result_to_dict` projection.

    The check phase keeps the typed counterexample (`structured_cex=True`, the
    default): grading (#4) re-renders and diffs it against mutants, so it needs
    the structured trace, not just the raw text. `k` is this front-end's framing
    over the intrinsic verdict fields the projection owns.
    """
    return {"k": k, **result_to_dict(result)}


def _harness_filename(unit_id: str, property_id: str) -> str:
    """A filesystem-safe harness name per (unit, property), distinct + stable.

    One file per property (mirrors `ProviderFixPort` writing one file per fix),
    so each property's harness is inspectable and never clobbers another's. The
    property id is already a content hash; the unit id is sanitized for the path.
    """
    unit = re.sub(r"[^A-Za-z0-9_.-]", "_", unit_id)
    prop = re.sub(r"[^A-Za-z0-9_.-]", "_", property_id)
    return f"{unit}__{prop}.c"
