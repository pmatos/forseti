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
import sys

from . import event_log
from . import forseti_gate as gate


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
            out.append(gate.needs_note(needs))
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
            lines.append(v.counterexample.strip()[: gate.CEX_CLIP])
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
        lines += ["", gate.needs_note(needs)]
    print("\n".join(lines), file=sys.stderr)
    return 2


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    project_dir = gate.project_dir(data)

    if errors := gate.env_config_errors():
        # Same fail-closed check `stop_gate.main()` and `post_tool_use.main()`
        # open with, and for the same reason: `_verify_file` below reads
        # `gate.DEFAULT_K`/`VERIFY_TIMEOUT_S` via `verify_and_record`, and a
        # malformed value silently falls back to its default instead of
        # raising (issue #95 review) -- this hook would otherwise verify and
        # durably record a verdict under that wrong value before the Stop hook
        # ever gets a chance to block on the misconfiguration.
        print(gate.env_config_error_message(errors), file=sys.stderr)
        return 2

    # Read state once for the baseline HEAD (so the scan also catches C committed
    # in the same Bash command) and to pick the files that actually changed since
    # their last verify; verify_and_record re-locks per file, so we hold no lock.
    state = gate.load_state(project_dir)
    discovered = gate.discover_changed_c_sources(
        project_dir, baseline_head=state.get("baseline_head")
    )
    if discovered is None:
        # Not a git work tree — out-of-band discovery is inactive. Never a silent
        # no-op: record the degraded scope so the gap is visible in the trace. The
        # scan still runs: a verify interrupted before its verdicts landed is named
        # by the gate's own `pending` marker, not by git, so it is retried even here
        # (PR #148 review).
        event_log.log_event(
            project_dir,
            event_log.GATE,
            decision="oob_scan_skipped",
            reason=(
                "not a git repository; C written out-of-band via Bash is not gated "
                "(an interrupted verify is still retried from its pending marker)"
            ),
        )

    stale = gate.sources_needing_verify(project_dir, state, discovered)
    if not stale:
        return 0

    verdicts: list[gate.UnitVerdict] = []
    for file_path in stale:
        verdicts.extend(_verify_file(project_dir, file_path))
    return _report(verdicts)


if __name__ == "__main__":
    sys.exit(main())
