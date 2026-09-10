"""One home for the verdict->report transform shared by the PostToolUse hooks.

``post_tool_use`` (direct edits) and ``post_bash`` (out-of-band Bash writes) both
turn a ``list[UnitVerdict]`` into a reviewer-facing message and a process exit
code. The two differ only in three strings — a fail-header, the pass-line prefix,
and the trailer — which each hook supplies as a :class:`ReportStyle`. The grammar
(the blocking-failure predicate, the ``✗``-line + counterexample layout, the
``CEX_CLIP`` clip, and the ``needs_note`` placement) lives here, once.

Event emission stays in the callers: the hooks interleave their own
``event_log``/``gate.decision`` events, and :func:`render` hands the classified
:class:`Partition` back on the :class:`Report` so a caller reads
``report.partition.failures`` for those events instead of re-classifying.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import forseti_gate as gate
from .forseti_gate import UnitVerdict


@dataclass(frozen=True)
class ReportStyle:
    """The three strings that separate one hook's message from the other.

    ``fail_header`` is a template with one ``{n}`` field — the failure count.
    """

    fail_header: str
    pass_prefix: str
    trailer: str


@dataclass(frozen=True)
class Partition:
    """A verdict batch split on the gate's decision axes, order preserved.

    ``failures`` is the sole home of the blocking predicate ``not v.passed and
    v.verdict != NEEDS_CONTRACT``: NEEDS_CONTRACT is honestly-unverified but must
    never block or be fed back as a counterexample (issue #122), so it lives in
    ``needs``, reported loudly and separately.
    """

    verified: list[UnitVerdict]
    failures: list[UnitVerdict]
    needs: list[UnitVerdict]


@dataclass(frozen=True)
class Report:
    """The rendered message, its exit code, and the partition it was built from.

    ``message`` is ``""`` when a pass has nothing to say — :func:`emit` then
    prints nothing (never a bare newline). ``exit_code`` is 0 (pass -> stdout) or
    2 (block -> stderr).
    """

    message: str
    exit_code: int
    partition: Partition

    @property
    def is_failure(self) -> bool:
        return self.exit_code != 0


def partition(verdicts: list[UnitVerdict]) -> Partition:
    """Split ``verdicts`` into verified / blocking-failures / needs-contract."""
    return Partition(
        verified=[v for v in verdicts if v.passed],
        failures=[
            v for v in verdicts if not v.passed and v.verdict != gate.NEEDS_CONTRACT
        ],
        needs=[v for v in verdicts if v.verdict == gate.NEEDS_CONTRACT],
    )


def render(verdicts: list[UnitVerdict], style: ReportStyle) -> Report:
    """Build the reviewer message + exit code for a verdict batch (pure)."""
    part = partition(verdicts)

    if not part.failures:
        out: list[str] = []
        if part.verified:
            oks = ", ".join(f"{v.unit_id} (k={v.k})" for v in part.verified)
            out.append(f"{style.pass_prefix} VERIFIED up to k — {oks}")
        if part.needs:
            out.append(gate.needs_note(part.needs))
        return Report(message="\n".join(out), exit_code=0, partition=part)

    lines = [style.fail_header.format(n=len(part.failures)), ""]
    for v in part.failures:
        lines.append(f"✗ {v.unit_id} — {v.verdict.upper()} (k={v.k})")
        if v.counterexample:
            lines.append("Counterexample:")
            lines.append(v.counterexample.strip()[: gate.CEX_CLIP])
        elif v.detail:
            lines.append(f"  {v.detail}")
        lines.append("")
    lines.append(style.trailer)
    if part.needs:
        lines += ["", gate.needs_note(part.needs)]
    return Report(message="\n".join(lines), exit_code=2, partition=part)


def emit(report: Report) -> int:
    """Print the report to the stream its exit code selects; return the code.

    The single I/O boundary of this module: pass -> stdout, failure -> stderr,
    and an empty message prints nothing. ``sys.std*`` is resolved at call time so
    a caller's stream redirection (and pytest's capture) is honoured.
    """
    if report.message:
        stream = sys.stderr if report.is_failure else sys.stdout
        print(report.message, file=stream)
    return report.exit_code
