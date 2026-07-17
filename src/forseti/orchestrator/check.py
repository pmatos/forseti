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
from pathlib import Path
from typing import Any, assert_never

from forseti.esbmc import (
    Error,
    EsbmcResult,
    Unknown,
    Verified,
    Violated,
    result_to_dict,
)
from forseti.properties import (
    HarnessError,
    Property,
    PropertyKind,
    extract_signature,
    render_semantic_harness,
    spec_from_property,
)

from .ladder import LadderAttempt, validated_ladder, verify_ladder
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
    code verdict.
    """

    HELD = "held"  # Verified up to k
    VIOLATED = "violated"  # counterexample found
    UNKNOWN = "unknown"  # inconclusive after the ladder is exhausted
    ERROR = "error"  # esbmc failed (e.g. a harness that didn't compile)
    SKIPPED = "skipped"  # reachability kind, deferred (ADR-0009 D2)


@dataclass(frozen=True)
class PropertyVerdict:
    """One property's outcome on the real unit — the atom grading (#4) consumes.

    `k` is the bound the verdict settled at (`None` when `SKIPPED`); `result` is
    the raw typed ESBMC result (`None` when `SKIPPED`); `harness_source` is the
    exact harness checked, kept as provenance so #4 can re-render it against
    mutants. `skip_reason` explains a `SKIPPED` outcome or a render-failure `ERROR`.
    """

    property_id: str
    unit_id: str
    kind: str  # "semantic" | "reachability"
    outcome: PropertyOutcome
    k: int | None
    result: EsbmcResult | None
    harness_source: str | None
    skip_reason: str | None = None

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
            "result": (
                _result_payload(self.result, self.k)
                if self.result is not None
                else None
            ),
        }


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

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict (unit id, counts, and every verdict)."""
        return {
            "unit_id": self.unit_id,
            "counts": self.counts(),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
        }


class SemanticHarnessWriter:
    """The production `HarnessWriterPort`: render a semantic property via #64.

    Parses the unit signature from the slice (`extract_signature`), projects the
    property onto a `SemanticSpec` (`spec_from_property`), and renders a
    self-contained ESBMC harness (`render_semantic_harness`). Fail-loud: a
    non-renderable unit/property raises `HarnessError` rather than emit a silent
    mis-harness. Non-semantic properties are skipped by the driver *before* this
    is reached (ADR-0009 D2), so `render` only ever sees semantic ones.
    """

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        signature = extract_signature(unit.source_text, unit.symbol)
        spec = spec_from_property(prop)
        text = render_semantic_harness(
            unit_source=unit.source_text, signature=signature, spec=spec
        )
        return RenderedHarness(source_text=text)


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
    """Check every stored property for `unit`, returning a verdict per property.

    For each property: emit a start event, then — a reachability property is
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

    props = store.list_for_unit(unit.unit_id)
    emit("properties.loaded", detail={"unit": unit.unit_id, "count": len(props)})

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
                PropertyVerdict(
                    prop.property_id,
                    unit.unit_id,
                    kind,
                    PropertyOutcome.SKIPPED,
                    None,
                    None,
                    None,
                    reason,
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
                PropertyVerdict(
                    prop.property_id,
                    unit.unit_id,
                    kind,
                    PropertyOutcome.ERROR,
                    None,
                    None,
                    None,
                    reason,
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

        # verify_ladder *yields* each rung as it is computed, already carrying its
        # `escalate_to` decision; consume it lazily so a non-terminal Unknown's
        # escalation is flushed before the next (possibly slow) rung runs (issue
        # #100) — never pull the next rung ahead of emitting (that was the eager
        # bug).
        attempts: list[LadderAttempt] = []
        for attempt in verify_ladder(harness_path, verify=verify, ladder=ladder):
            attempts.append(attempt)
            if attempt.escalate_to is not None:
                emit(
                    "unknown.policy.decision",
                    index=index,
                    detail={
                        "decision": "escalate",
                        "property_id": prop.property_id,
                        "from_k": attempt.k,
                        "to_k": attempt.escalate_to,
                    },
                )
        final = attempts[-1]
        outcome = _outcome_for(final.result)
        verdicts.append(
            PropertyVerdict(
                prop.property_id,
                unit.unit_id,
                kind,
                outcome,
                final.k,
                final.result,
                rendered.source_text,
            )
        )
        emit(
            "property.verdict",
            index=index,
            k=final.k,
            verdict=outcome.value,
            detail={"property_id": prop.property_id, "kind": kind},
        )

    run = PropertyCheckRun(unit.unit_id, tuple(verdicts))
    emit("properties.checked", detail={"unit": unit.unit_id, **run.counts()})
    return run


def _outcome_for(result: EsbmcResult) -> PropertyOutcome:
    """Map an ESBMC verdict to a property outcome (exhaustive over the union).

    The final `assert_never` mirrors `state.py`'s `next_state`: adding a new
    `EsbmcResult` variant becomes a mypy error here, so no verdict is dropped.
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
