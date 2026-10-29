"""Local trace projection shared by the two PostToolUse verification hooks.

The hooks choose when these two phases run, including whether an empty batch
gets a local gate event. Canonical decisions and reviewer messages stay with
the hooks; verdict classification stays with ``verdict_report``.
"""

from __future__ import annotations

from typing import Literal

from . import event_log
from .forseti_gate import UnitVerdict
from .verdict_report import Partition


def record_verification(
    project_dir: str, file: str, tool: str, verdicts: list[UnitVerdict]
) -> None:
    """Record the edit, then its verdicts in batch order, even for an empty batch.

    Timing belongs to each verification call, although these events are appended
    together after the serialized verification batch returns. Writer failures
    retain ``event_log.log_event``'s existing best-effort I/O behavior.
    """
    event_log.log_event(
        project_dir,
        event_log.EDIT,
        tool=tool,
        file=file,
        functions=[v.function for v in verdicts],
    )
    for v in verdicts:
        event_log.log_event(
            project_dir,
            event_log.VERIFY,
            unit=v.unit_id,
            verdict=v.verdict,
            k=v.k,
            duration_s=v.duration_s,
            argv=list(v.argv) if v.argv else None,
        )


def record_gate(
    project_dir: str, file: str, partition: Partition
) -> Literal["pass", "block"]:
    """Record gate counts from the report partition and return the decision."""
    decision = "block" if partition.failures else "pass"
    event_log.log_event(
        project_dir,
        event_log.GATE,
        file=file,
        decision=decision,
        n_failures=len(partition.failures),
        n_needs_contract=len(partition.needs),
        exit_code=2 if partition.failures else 0,
    )
    return decision
