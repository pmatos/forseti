"""Typed property model for the write->verify->fix loop's property store.

A Property is a checkable predicate proposed for one verification unit
(`path::symbol`). ESBMC returns a VERDICT, never a proof -- a property's
lifecycle status and reserved grading slot record how a proposed property fared,
not any soundness claim. Pure data: no LLM, no ESBMC, stdlib only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PropertyKind(Enum):
    """What a property asserts about its unit."""

    SEMANTIC = "semantic"  # a predicate over the unit's behaviour (W2 target)
    REACHABILITY = "reachability"  # representable now, inert (#63 parked, ADR-0009 D2)


class PropertyStatus(Enum):
    """A property's lifecycle position; transitions gated by `_ALLOWED`."""

    CANDIDATE = "candidate"  # freshly proposed
    GRADED = "graded"  # mutation-kill score attached (#4)
    ACCEPTED = "accepted"  # kept for the unit (terminal)
    REJECTED = "rejected"  # discarded (terminal)


class GradingVerdict(Enum):
    """Reserved per-property grading outcome (ADR-0009 D4); populated by #4."""

    HELD = "held"  # property held under grading (analogue of VERIFIED)
    VIOLATED = "violated"
    UNKNOWN = "unknown"


class InvalidStatusTransition(ValueError):
    """A status move not permitted by `_ALLOWED` (e.g. a terminal, or a skip)."""

    def __init__(self, current: PropertyStatus, requested: PropertyStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"{current.value} -> {requested.value} is not an allowed status transition"
        )


@dataclass(frozen=True)
class Provenance:
    """Proposer origin hooks -- the GEPA (#5) prompt-evolution key.

    Deliberately excluded from `make_property_id`: the predicate is the identity,
    the prompt that proposed it is metadata.
    """

    prompt_id: str
    prompt_version: str

    def to_dict(self) -> dict[str, Any]:
        return {"prompt_id": self.prompt_id, "prompt_version": self.prompt_version}


@dataclass(frozen=True)
class Grading:
    """Reserved grading slot, set atomically by the mutation-kill harness (#4).

    `None` on a Property means "not yet graded"; present means all three are set
    (`reason` may be None). `kill_rate` in [0.0, 1.0].
    """

    verdict: GradingVerdict
    kill_rate: float
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "kill_rate": self.kill_rate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Property:
    """A checkable predicate proposed for one unit (`path::symbol`).

    `expression` is the predicate/assertion text (a label, when reachability);
    `domain` holds the precondition expressions the harness writer (#64) emits as
    `__ESBMC_assume(...)` before the call, constraining the nondet inputs so the
    property is not vacuously violated. Both feed `make_property_id`, so two
    predicates that differ only in their preconditions are distinct properties.
    """

    property_id: str  # content id (see make_property_id); PK in the store
    unit_id: str  # canonical "path::symbol"
    kind: PropertyKind
    expression: str
    status: PropertyStatus
    provenance: Provenance
    domain: tuple[str, ...] = ()  # __ESBMC_assume preconditions over the params (#64)
    grading: Grading | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict (enums -> `.value`, nested carriers expanded)."""
        return {
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "kind": self.kind.value,
            "expression": self.expression,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "domain": list(self.domain),
            "grading": self.grading.to_dict() if self.grading is not None else None,
            "description": self.description,
        }


_ALLOWED: dict[PropertyStatus, frozenset[PropertyStatus]] = {
    PropertyStatus.CANDIDATE: frozenset(
        {PropertyStatus.GRADED, PropertyStatus.REJECTED}
    ),
    PropertyStatus.GRADED: frozenset(
        {PropertyStatus.ACCEPTED, PropertyStatus.REJECTED}
    ),
    PropertyStatus.ACCEPTED: frozenset(),  # terminal
    PropertyStatus.REJECTED: frozenset(),  # terminal
}


def is_valid_transition(current: PropertyStatus, requested: PropertyStatus) -> bool:
    """True iff `requested` is reachable from `current`.

    Policy, centralized here so #4/#44 can tighten it without touching the store:
    `CANDIDATE->REJECTED` is allowed (discard a bad candidate pre-grading);
    self-transitions are disallowed (the caller handles no-ops); `ACCEPTED` and
    `REJECTED` are terminal.
    """
    return requested in _ALLOWED[current]


def is_terminal(status: PropertyStatus) -> bool:
    """True iff `status` admits no onward transition (`ACCEPTED`/`REJECTED`).

    Derived from `_ALLOWED` (not a hardcoded pair) so it stays correct if the
    lifecycle grows a new terminal state. A terminal property's fate is settled,
    which is what lets `check_properties` (#84) treat it as an invalid check input.
    """
    return not _ALLOWED[status]


def make_property_id(
    unit_id: str,
    kind: PropertyKind,
    expression: str,
    domain: tuple[str, ...] = (),
) -> str:
    """Stable content id over (unit, kind, expression, domain).

    Deterministic: the same logical property dedups across proposer runs
    regardless of provenance. Provenance is excluded by design; `domain` is
    included so a predicate checked under different preconditions is a distinct
    property rather than a spurious duplicate.
    """
    parts = (unit_id, kind.value, expression, *domain)
    payload = "\x00".join(parts).encode()
    return hashlib.sha256(payload).hexdigest()[:16]
