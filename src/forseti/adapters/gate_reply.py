"""Shared violated/inconclusive reply grammar for harness gate adapters.

Codex and Oh My Pi classify their own verification results and own their JSON
transport, canonical events, and output. This module concentrates only the
ordered reply grammar those adapters otherwise repeat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GateReply:
    """One pure gate decision and the message its adapter should transport."""

    decision: Literal["block", "unresolved", "pass"]
    message: str


def _render_inconclusive(findings: Sequence[tuple[str, str]]) -> str:
    return ", ".join(f"{subject} [{outcome}]" for subject, outcome in findings)


def render_gate_reply(
    violations: Sequence[tuple[str, str]],
    inconclusive: Sequence[tuple[str, str]],
    *,
    violation_header: str,
    verb: Literal["verify", "check"],
) -> GateReply:
    """Render ordered findings without performing transport side effects.

    Violations take precedence over inconclusive findings. Mixed results retain
    the inconclusive residual after every violation. Empty findings are a silent
    pass; callers still own any adapter-specific no-source outcome.
    """
    if violations:
        lines = [violation_header]
        lines.extend(
            f"\n### VIOLATED: {subject}\n{evidence}" for subject, evidence in violations
        )
        if inconclusive:
            lines.append(
                "\nAlso inconclusive (do not ignore): "
                f"{_render_inconclusive(inconclusive)}"
            )
        return GateReply(decision="block", message="\n".join(lines))

    if inconclusive:
        return GateReply(
            decision="unresolved",
            message=(
                f"Forseti could not conclusively {verb}: "
                f"{_render_inconclusive(inconclusive)}. "
                "Not a pass — raise k, add an entry/harness, or report."
            ),
        )

    return GateReply(decision="pass", message="")
