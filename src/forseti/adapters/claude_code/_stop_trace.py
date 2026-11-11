"""Final Stop trace projection, separate from gate decisions and messages.

The hook owns when a turn blocks, reaches its attempt cap, or can end. These
operations own the local trace schemas for those outcomes. Housekeeping and
canonical ``gate.decision`` events stay with the hook.
"""

from __future__ import annotations

from typing import Literal

from . import event_log
from .property_gate import SemanticCheckSummary


def _semantic_counts(summary: SemanticCheckSummary) -> dict[str, int]:
    return {
        "n_semantic_violations": len(summary.violations),
        "n_semantic_unresolved": len(summary.unresolved),
        "n_semantic_failed": summary.failed,
        "n_semantic_skipped": len(summary.skipped),
        "n_semantic_vacuous": len(summary.vacuous),
    }


def record_allow(
    project_dir: str,
    semantic: SemanticCheckSummary,
    *,
    n_needs_contract: int,
) -> None:
    """Record quiet allow or note-bearing allow, with its existing sparse fields.

    A semantic-only note must not be labeled as NEEDS_CONTRACT. Checked units
    alone have nothing to report; deferred units still cause a note, although
    neither count is part of the existing trace schema. The caller has already
    established that the turn can end without a blocking residual.
    """
    if not n_needs_contract and semantic.empty:
        event_log.log_event(
            project_dir, event_log.STOP, decision="allow", n_unverified=0, attempt=0
        )
        return
    event_log.log_event(
        project_dir,
        event_log.STOP,
        decision="allow_needs_contract" if n_needs_contract else "allow_semantic_check",
        n_needs_contract=n_needs_contract,
        **_semantic_counts(semantic),
        attempt=0,
    )


def record_outstanding(
    project_dir: str,
    semantic: SemanticCheckSummary,
    *,
    decision: Literal["block", "residual"],
    attempt: int,
    n_unverified: int,
    n_oob: int,
    n_blob: int,
    n_needs_contract: int,
) -> None:
    """Record a block or capped residual with the same full count schema.

    Counts and the actual attempt come from the hook's existing bookkeeping;
    this projection neither reclassifies findings nor applies the attempt cap.
    Both operations retain the event writer's best-effort I/O behavior.
    """
    event_log.log_event(
        project_dir,
        event_log.STOP,
        decision=decision,
        n_unverified=n_unverified,
        n_oob=n_oob,
        n_blob=n_blob,
        attempt=attempt,
        n_needs_contract=n_needs_contract,
        **_semantic_counts(semantic),
    )
