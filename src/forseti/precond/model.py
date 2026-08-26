"""The discharge result vocabulary (RFC-0003 S3).

Pure value objects — no ESBMC, no tempfiles, no driver logic — that record what
checking a callee's callers established: `CallerOutcome` (what one caller's run
proved), `CallerCheck` (one caller's contribution to or withholding of the
discharge), and `DischargeResult` (the S2 verdict plus what the callers did to
it, with its honest one-line `label` and `--json` `to_dict`).

Isolated from the `discharge` driver so the vocabulary the CLI and `--json`
consumers read does not pull in the tempfile/ESBMC machinery that produces it.
The dependency runs one way: the driver imports these; they import nothing from
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from forseti.esbmc import EsbmcResult, Violated

from .verify import Assessment, PreconditionResult


class CallerOutcome(Enum):
    """What one caller's obligation run established."""

    DISCHARGED = "discharged"  # reaches the call and satisfies the precondition
    OBLIGATION_VIOLATED = "obligation_violated"  # passes a pointer that does not
    CALLER_VIOLATED = "caller_violated"  # fails a memory property of its own first
    UNDERDETERMINED = "underdetermined"  # L0 under-read the *caller*'s own extent
    UNREACHABLE = "unreachable"  # never reaches the call — discharges nothing
    UNCHECKED = "unchecked"  # not materialisable / inconclusive — not a discharge
    UNRESOLVED = "unresolved"  # takes the callee's address — the caller set is open
    UNATTRIBUTED = "unattributed"  # a failure the callee's own re-entry can explain


@dataclass(frozen=True)
class CallerCheck:
    """One caller's contribution to (or withholding of) the discharge.

    `caller` names whatever can reach the callee: a function of this translation
    unit, or — for an ``UNRESOLVED`` escape written at file scope — the object
    whose initialiser holds the callee's address.
    """

    caller: str
    outcome: CallerOutcome
    detail: str
    settled_k: int | None = None
    esbmc_result: EsbmcResult | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "caller": self.caller,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "settled_k": self.settled_k,
        }
        if isinstance(self.esbmc_result, Violated):
            payload["counterexample"] = self.esbmc_result.raw_counterexample
        return payload


@dataclass(frozen=True)
class DischargeResult:
    """The S2 verdict, plus what checking the unit's callers did to it."""

    function: str
    assessment: Assessment
    detail: str
    unit_result: PreconditionResult
    callers: tuple[CallerCheck, ...] = ()

    @property
    def label(self) -> str:
        """The one-line honest verdict.

        The S2 label passes through **only** when S2's own verdict is what this
        result carries. A failure of the discharge machinery itself (an unreadable
        source, an uninjectable definition, a failed TU listing) leaves S2's
        ``ASSUMED_VERIFIED`` label attached to an ``ERROR``, which the CLI would
        print as if verification had succeeded.
        """
        if self.assessment is Assessment.DISCHARGED_VERIFIED:
            return f"VERIFIED (discharged — {self.detail})"
        if self.assessment is Assessment.ASSUMED_VERIFIED:
            return f"{self.unit_result.label} [{self.detail}]"
        if self.callers:
            return f"VIOLATED at the call site ({self.detail})"
        if self.assessment is self.unit_result.assessment:
            return self.unit_result.label
        return f"{self.assessment.value.upper()} ({self.detail})"

    def to_dict(self) -> dict[str, Any]:
        payload = self.unit_result.to_dict()
        payload["assessment"] = self.assessment.value
        payload["assumed"] = self.assessment is Assessment.ASSUMED_VERIFIED
        payload["discharged"] = self.assessment is Assessment.DISCHARGED_VERIFIED
        payload["detail"] = self.detail
        payload["callers"] = [c.to_dict() for c in self.callers]
        return payload
