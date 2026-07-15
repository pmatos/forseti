#!/usr/bin/env python3
"""Stop-gate hook: block the turn from ending until every unit is VERIFIED.

Reads the gate state written by the PostToolUse hook. While any tracked unit is
not VERIFIED up to k it emits a `block` decision, so Claude cannot hand the code
back — the emulated Stop-gate. To avoid an unbounded loop when a unit genuinely
cannot be verified, it blocks at most MAX_STOP_ATTEMPTS consecutive times (the
counter is reset by any fresh edit), then lets the turn end with a LOUD
unverified residual — never a silent pass.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forseti_gate as gate

_CEX_CLIP = 1200


def _project_dir(data: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def _residual(failures: list[dict]) -> str:
    lines = []
    for u in failures:
        lines.append(
            f"✗ {u.get('unit_id')} — {str(u.get('verdict')).upper()} (k={u.get('k')})"
        )
        cex, detail = u.get("counterexample"), u.get("detail")
        if cex:
            lines.append(cex.strip()[:_CEX_CLIP])
        elif detail:
            lines.append(f"  {detail}")
    return "\n".join(lines)


def _emit(obj: dict) -> int:
    print(json.dumps(obj))
    return 0


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    project_dir = _project_dir(data)

    with gate.gate_lock(project_dir):  # serialize with concurrent PostToolUse hooks
        state = gate.load_state(project_dir)
        failures = gate.unverified_units(state)
        if failures:
            attempts = int(state.get("stop_attempts", 0)) + 1
            state["stop_attempts"] = attempts
            gate.save_state(project_dir, state)

    if not failures:
        return 0  # nothing outstanding — allow the turn to end

    if attempts > gate.MAX_STOP_ATTEMPTS:
        # Allow the turn to end by OMITTING `decision` — the Stop schema only
        # recognizes "block", so emit just the loud residual as a systemMessage
        # (a top-level field) rather than risk an unrecognized "approve" dropping it.
        return _emit(
            {
                "systemMessage": (
                    "⚠ Forseti: ending the turn with UNVERIFIED unit(s) after "
                    f"{gate.MAX_STOP_ATTEMPTS} attempts. This is NOT a pass — "
                    "report the residual to the human:\n" + _residual(failures)
                ),
            }
        )

    return _emit(
        {
            "decision": "block",
            "reason": (
                f"Forseti verify-gate: {len(failures)} unit(s) are not VERIFIED "
                "up to k. Do not end the turn — fix them and let the gate "
                "re-verify, or explicitly report to the human which unit / "
                "property / k could not be verified and why.\n\n" + _residual(failures)
            ),
        }
    )


if __name__ == "__main__":
    sys.exit(main())
