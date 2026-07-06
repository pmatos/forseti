"""The human-readable loop transcript — the at-a-glance demo summary.

A pure projection over `report_for`: the unit, the final outcome (+ give-up
reason), and one line per iteration (verdict, the bound `k` it ran at, the
source verified, and the UNKNOWN reason when relevant). It reuses the report's
already-extracted rows, so the bound `k` the loop escalated to is carried
straight through. The full counterexample is `report.py`'s job; this stays a
summary.
"""

from __future__ import annotations

from .loop import LoopRun
from .report import report_for


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
