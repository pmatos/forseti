"""The human-readable transcripts — the at-a-glance demo summaries.

`transcript_for` is a pure projection over `report_for`: the unit, the final
outcome (+ give-up reason), and one line per iteration (verdict, the bound `k` it
ran at, the source verified, and the UNKNOWN reason when relevant). It reuses the
report's already-extracted rows, so the bound `k` the loop escalated to is
carried straight through. The full counterexample is `report.py`'s job; this
stays a summary.

`property_check_transcript` is the sibling projection for the #66 property-check
driver: the unit, one line per property (its outcome, the bound it settled at,
the property id and kind), and a per-outcome counts footer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .loop import LoopRun
from .report import report_for

if TYPE_CHECKING:  # only for typing — avoids a runtime cycle with check.py
    from .check import PropertyCheckRun


def transcript_for(run: LoopRun, *, unit_id: str) -> str:
    """Render a `LoopRun` as a readable transcript (pure, total over every outcome)."""
    report = report_for(run)
    lines = [
        f"Forseti loop transcript — {unit_id}",
        f"Outcome: {report.final_state.name}",
    ]
    if report.give_up_reason is not None:
        lines.append(f"  reason: {report.give_up_reason.value}")
    lines.append("Iterations:")
    for it in report.iterations:
        row = f"  [{it.index}] {it.verdict.upper():9} k={it.k} {it.source}"
        if it.unknown_reason is not None:
            row += f"  (unknown: {it.unknown_reason})"
        lines.append(row)
    return "\n".join(lines)


def property_check_transcript(run: PropertyCheckRun) -> str:
    """Render a `PropertyCheckRun` as a readable transcript (pure, total).

    One line per property in check order — the outcome, the bound `k` it settled
    at (`-` when SKIPPED), the property id and kind, and a skip reason when
    relevant — followed by a per-outcome counts footer.
    """
    lines = [f"Forseti property check — {run.unit_id}", "Properties:"]
    for verdict in run.verdicts:
        k = f"k={verdict.k}" if verdict.k is not None else "k=-"
        row = (
            f"  {verdict.outcome.value.upper():9} {k:6} "
            f"{verdict.property_id} {verdict.kind}"
        )
        if verdict.skip_reason is not None:
            row += f"  ({verdict.skip_reason})"
        lines.append(row)
    counts = run.counts()
    summary = ", ".join(
        f"{name}={counts[name]}"
        for name in ("held", "violated", "unknown", "error", "skipped")
    )
    lines.append(f"Counts: {summary}")
    return "\n".join(lines)
