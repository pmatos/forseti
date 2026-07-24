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


def _oob_note(project_dir: str, oob: list[str]) -> str:
    """Loud note for C files changed out-of-band (via Bash) that are unverified.

    The backstop for the ``post_bash`` PostToolUse scan (issue #99): normally that
    hook has already verified every Bash-written C file by turn end, so this is
    empty. If one slipped through it blocks the turn — never a silent pass — and
    tells Claude how to get it re-checked.
    """
    rels = ", ".join(gate.unit_id(project_dir, f) for f in oob)
    return (
        f"⚠ Forseti: {len(oob)} C file(s) changed out-of-band (written via Bash, "
        f"bypassing the edit gate) and are UNVERIFIED: {rels}. Re-verify them — edit "
        "the file, or run any Bash command so the scan re-checks — before ending."
    )


def _blob_note(blob: list[dict]) -> str:
    """Loud note for C staged/committed out-of-band whose blob was never verified.

    The worktree copy can hash clean while the *index* or *HEAD* holds a divergent,
    unverified blob (a `git add`/commit of C the gate never saw — issue #99 review).
    Remediation is reason-specific: a *staged* divergence lives in the index (clear it
    with `git add`/`git restore --staged`), a *committed* one lives in `HEAD` (those
    index commands can NOT clear it — the verified content has to reach `HEAD`).
    Spelling the right move out per reason is what turns each block into a convergent
    one instead of a loop; editing the worktree alone reconciles neither.
    """
    staged = [b["rel"] for b in blob if b.get("reason") == "staged"]
    committed = [b["rel"] for b in blob if b.get("reason") == "committed"]
    lines = [
        f"⚠ Forseti: {len(blob)} C blob(s) were staged/committed out-of-band and are "
        "UNVERIFIED — the index/HEAD holds C the gate never saw while the worktree may "
        "hash clean, so committing/shipping it would ship unverified code. Editing the "
        "worktree alone will NOT clear this:"
    ]
    if staged:
        lines.append(
            "• Staged in the index: "
            + ", ".join(staged)
            + " — reconcile the index: re-stage the verified file (`git add <file>`), "
            "unstage it (`git restore --staged <file>`), or recreate the intended "
            "content in the worktree and let the gate re-verify."
        )
    if committed:
        lines.append(
            "• Committed since session start: "
            + ", ".join(committed)
            + " — this lives in HEAD, so `git add`/`git restore --staged` cannot clear "
            "it: bring the committed content into the worktree and let the gate "
            "re-verify it, or commit the verified content over it."
        )
    lines.append("Reconcile before ending.")
    return "\n".join(lines)


def _emit(obj: dict) -> int:
    print(json.dumps(obj))
    return 0


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    project_dir = _project_dir(data)

    # Discover C files changed out-of-band (Bash) that the gate has not verified.
    # This is an ESBMC-free, git-fast backstop — the heavy verify runs in the
    # `post_bash` PostToolUse hook (300 s budget), never here (120 s, kill = silent
    # allow). `None` means no git repo → out-of-band detection is inactive. The
    # baseline HEAD (read outside the lock — it is set once at session start and
    # never mutated mid-session) also surfaces C committed in the same Bash command.
    pre = gate.load_state(project_dir)
    baseline_head = pre.get("baseline_head")
    discovered = gate.discover_changed_c_sources(
        project_dir, baseline_head=baseline_head
    )
    # C whose STAGED or COMMITTED blob diverges from the last verified content while
    # the worktree hashes clean (issue #99 review). Git-only, so it runs OUTSIDE the
    # lock like discovery; `pre` is a good-enough snapshot of `scanned` for it (a
    # concurrent verify would only relax an over-gate on the next turn, never mask a
    # divergence). Empty (skipped) when this is not a git work tree.
    blob = (
        gate.divergent_blob_sources(project_dir, pre, baseline_head=baseline_head)
        if discovered is not None
        else []
    )

    with gate.gate_lock(project_dir):  # serialize with concurrent PostToolUse hooks
        state = gate.load_state(project_dir)
        # Reconcile away units whose C source was deleted out-of-band (a Bash `rm`)
        # BEFORE reading `blocking_units`, or the turn would block forever on a unit
        # whose file no longer exists (issue #99 review).
        pruned = gate.prune_deleted_units(state, project_dir)
        blocking = gate.blocking_units(state)
        needs = gate.needs_contract_units(state)
        oob = gate.stale_sources(project_dir, state, discovered) if discovered else []
        outstanding = bool(blocking) or bool(oob) or bool(blob)
        attempts = int(state.get("stop_attempts", 0)) + 1
        if outstanding:
            state["stop_attempts"] = attempts
        if outstanding or pruned:
            gate.save_state(project_dir, state)

    if pruned:
        # Never a silent reconcile: the trace records which units were dropped
        # because their backing file was deleted.
        event_log.log_event(
            project_dir, event_log.STOP, decision="pruned_deleted", pruned=pruned
        )

    if discovered is None:
        # Never a silent no-op: the trace records that out-of-band writes could
        # not be checked in this (non-git) project.
        event_log.log_event(
            project_dir,
            event_log.STOP,
            decision="oob_scan_skipped",
            reason="not a git repository",
        )

    if not outstanding:
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

    # Fold the recorded-blocking residual, any out-of-band files, any divergent
    # staged/committed blobs, and any NEEDS_CONTRACT note into one message. Only
    # blocking + oob + blob drive the block.
    sections = []
    if blocking:
        sections.append(_residual(blocking))
    if oob:
        sections.append(_oob_note(project_dir, oob))
    if blob:
        sections.append(_blob_note(blob))
    if needs:
        sections.append(_needs_message(needs))
    detail = "\n\n".join(sections)
    n_out = len(blocking) + len(oob) + len(blob)

    if attempts > gate.MAX_STOP_ATTEMPTS:
        # Allow the turn to end by OMITTING `decision` — the Stop schema only
        # recognizes "block", so emit just the loud residual as a systemMessage
        # (a top-level field) rather than risk an unrecognized "approve" dropping it.
        event_log.log_event(
            project_dir,
            event_log.STOP,
            decision="residual",
            n_unverified=len(blocking),
            n_oob=len(oob),
            n_blob=len(blob),
            attempt=attempts,
        )
        return _emit(
            {
                "systemMessage": (
                    "⚠ Forseti: ending the turn with UNVERIFIED item(s) after "
                    f"{gate.MAX_STOP_ATTEMPTS} attempts. This is NOT a pass — "
                    "report the residual to the human:\n" + detail
                ),
            }
        )

    event_log.log_event(
        project_dir,
        event_log.STOP,
        decision="block",
        n_unverified=len(blocking),
        n_oob=len(oob),
        n_blob=len(blob),
        attempt=attempts,
    )
    return _emit(
        {
            "decision": "block",
            "reason": (
                f"Forseti verify-gate: {n_out} item(s) are not VERIFIED up to k. "
                "Do not end the turn — fix/verify them and let the gate re-check, "
                "or explicitly report to the human which unit / property / k could "
                "not be verified and why.\n\n" + detail
            ),
        }
    )


if __name__ == "__main__":
    sys.exit(main())
