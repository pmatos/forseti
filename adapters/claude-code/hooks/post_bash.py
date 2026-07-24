#!/usr/bin/env python3
"""PostToolUse verify hook for out-of-band writes via the ``Bash`` tool (issue #99).

The edit-triggered ``post_tool_use.py`` hook only fires on ``Write``/``Edit``/
``MultiEdit`` and keys off ``tool_input.file_path``. A C file created or modified
through the ``Bash`` tool — ``cat > f.c``, a generator script, ``sed -i``,
``tee``, a heredoc — carries a ``command`` string, not a ``file_path``, so it
never triggers that hook and the Stop-gate can let the turn end with unverified C.

This hook runs after every ``Bash`` tool call. Rather than parse the (arbitrary)
shell command for filenames — unreliable — it asks ``git`` which C sources
changed, then verifies each one whose content differs from what the gate last saw
(``forseti_gate.stale_sources``). The heavy ESBMC work runs here, inside the
turn's 300 s PostToolUse budget, not in the kill-sensitive Stop hook. A
non-``VERIFIED`` verdict is fed back to Claude on stderr with exit 2, exactly like
the edit-triggered path; a ``NEEDS_CONTRACT`` pointer/array unit is reported
loudly but never blocks (issue #122).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import event_log
import forseti_gate as gate

_CEX_CLIP = 1500


def _project_dir(data: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def _needs_note(needs: list[gate.UnitVerdict]) -> str:
    """A loud, non-fixable note for NEEDS_CONTRACT units (never a silent skip)."""
    ids = ", ".join(v.unit_id for v in needs)
    return (
        f"Forseti: {len(needs)} unit(s) NOT gated — {ids}. They take pointer/array "
        "parameter(s); function-level safety is unreliable without a memory "
        "precondition/harness, so they were NOT verified (issue #122). This is not "
        "a pass and not a source bug to 'fix' — leave them as is."
    )


def _verify_file(project_dir: str, file_path: str) -> list[gate.UnitVerdict]:
    """Verify one changed C file; trace the edit + each verify like the edit path."""
    verdicts = gate.verify_and_record(file_path, project_dir=project_dir)
    rel = gate.unit_id(project_dir, file_path)
    event_log.log_event(
        project_dir,
        event_log.EDIT,
        tool="Bash",
        file=rel,
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
    failures = [
        v for v in verdicts if not v.passed and v.verdict != gate.NEEDS_CONTRACT
    ]
    event_log.log_event(
        project_dir,
        event_log.GATE,
        file=rel,
        decision="block" if failures else "pass",
        n_failures=len(failures),
        n_needs_contract=sum(1 for v in verdicts if v.verdict == gate.NEEDS_CONTRACT),
        exit_code=2 if failures else 0,
    )
    return verdicts


def _report(verdicts: list[gate.UnitVerdict]) -> int:
    """Aggregate verdicts across all scanned files into one message + exit code."""
    needs = [v for v in verdicts if v.verdict == gate.NEEDS_CONTRACT]
    failures = [
        v for v in verdicts if not v.passed and v.verdict != gate.NEEDS_CONTRACT
    ]
    verified = [v for v in verdicts if v.passed]

    if not failures:
        out = []
        if verified:
            oks = ", ".join(f"{v.unit_id} (k={v.k})" for v in verified)
            out.append(f"Forseti (out-of-band): VERIFIED up to k — {oks}")
        if needs:
            out.append(_needs_note(needs))
        if out:
            print("\n".join(out))
        return 0

    lines = [
        f"Forseti: {len(failures)} unit(s) written out-of-band (Bash) did not "
        "verify (function-level ESBMC, safety properties).",
        "",
    ]
    for v in failures:
        lines.append(f"✗ {v.unit_id} — {v.verdict.upper()} (k={v.k})")
        if v.counterexample:
            lines.append("Counterexample:")
            lines.append(v.counterexample.strip()[:_CEX_CLIP])
        elif v.detail:
            lines.append(f"  {v.detail}")
        lines.append("")
    lines.append(
        "Fix the unit(s) to eliminate the counterexample; they will be "
        "re-verified automatically on the next edit or Bash write. Do not report "
        "the task done until every unit is VERIFIED up to k. An UNKNOWN is not a "
        "pass — raise k (FORSETI_UNWIND) or simplify the unit."
    )
    if needs:
        lines += ["", _needs_note(needs)]
    print("\n".join(lines), file=sys.stderr)
    return 2


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    project_dir = _project_dir(data)

    discovered = gate.discover_changed_c_sources(project_dir)
    if discovered is None:
        # Not a git work tree — out-of-band discovery is inactive. Never a silent
        # no-op: record the degraded scope so the gap is visible in the trace.
        event_log.log_event(
            project_dir,
            event_log.GATE,
            decision="oob_scan_skipped",
            reason="not a git repository; out-of-band Bash writes are not gated",
        )
        return 0

    # Read state once to pick the files that actually changed since their last
    # verify; verify_and_record re-locks per file, so we hold no lock here.
    state = gate.load_state(project_dir)
    stale = gate.stale_sources(project_dir, state, discovered)
    if not stale:
        return 0

    verdicts: list[gate.UnitVerdict] = []
    for file_path in stale:
        verdicts.extend(_verify_file(project_dir, file_path))
    return _report(verdicts)


if __name__ == "__main__":
    sys.exit(main())
