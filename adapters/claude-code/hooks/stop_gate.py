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

import event_log
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


def _needs_message(needs: list[dict]) -> str:
    """Loud, non-blocking note for NEEDS_CONTRACT units at turn end (never silent)."""
    ids = ", ".join(str(u.get("unit_id")) for u in needs)
    return (
        f"⚠ Forseti: {len(needs)} unit(s) NOT gated (pointer/array parameters need a "
        f"memory precondition/harness — issue #122): {ids}. These are UNVERIFIED, not "
        "passed; note them to the human, but they need no source fix."
    )


def _emit(obj: dict) -> int:
    print(json.dumps(obj))
    return 0


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    project_dir = _project_dir(data)

    with gate.gate_lock(project_dir):  # serialize with concurrent PostToolUse hooks
        state = gate.load_state(project_dir)
        blocking = gate.blocking_units(state)
        needs = gate.needs_contract_units(state)
        if blocking:
            attempts = int(state.get("stop_attempts", 0)) + 1
            state["stop_attempts"] = attempts
            gate.save_state(project_dir, state)

    if not blocking:
        # Nothing blocks. NEEDS_CONTRACT units (pointer/array, no harness yet) are
        # honestly-unverified but a source fix can't resolve them, so let the turn
        # end — loudly if any are outstanding, never silently.
        if needs:
            event_log.log_event(
                project_dir,
                event_log.STOP,
                decision="allow_needs_contract",
                n_needs_contract=len(needs),
                attempt=0,
            )
            return _emit({"systemMessage": _needs_message(needs)})
        event_log.log_event(
            project_dir, event_log.STOP, decision="allow", n_unverified=0, attempt=0
        )
        return 0  # nothing outstanding — allow the turn to end

    # Fold any NEEDS_CONTRACT units into the block/residual message so the human
    # sees the full picture, but they never drive the block themselves.
    extra = ("\n\n" + _needs_message(needs)) if needs else ""

    if attempts > gate.MAX_STOP_ATTEMPTS:
        # Allow the turn to end by OMITTING `decision` — the Stop schema only
        # recognizes "block", so emit just the loud residual as a systemMessage
        # (a top-level field) rather than risk an unrecognized "approve" dropping it.
        event_log.log_event(
            project_dir,
            event_log.STOP,
            decision="residual",
            n_unverified=len(blocking),
            attempt=attempts,
        )
        return _emit(
            {
                "systemMessage": (
                    "⚠ Forseti: ending the turn with UNVERIFIED unit(s) after "
                    f"{gate.MAX_STOP_ATTEMPTS} attempts. This is NOT a pass — "
                    "report the residual to the human:\n" + _residual(blocking) + extra
                ),
            }
        )

    event_log.log_event(
        project_dir,
        event_log.STOP,
        decision="block",
        n_unverified=len(blocking),
        attempt=attempts,
    )
    return _emit(
        {
            "decision": "block",
            "reason": (
                f"Forseti verify-gate: {len(blocking)} unit(s) are not VERIFIED "
                "up to k. Do not end the turn — fix them and let the gate "
                "re-verify, or explicitly report to the human which unit / "
                "property / k could not be verified and why.\n\n"
                + _residual(blocking)
                + extra
            ),
        }
    )


if __name__ == "__main__":
    sys.exit(main())
