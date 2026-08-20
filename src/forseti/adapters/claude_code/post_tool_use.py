#!/usr/bin/env python3
"""PostToolUse verify hook: after a C edit, verify the edited functions.

Fires on Write/Edit/MultiEdit. For a C source file it verifies every top-level
function at the function level (ESBMC safety properties, no harness) and records
each verdict in the gate state. On any non-VERIFIED verdict it writes an
actionable message to stderr and exits 2, which feeds the counterexample back to
Claude to fix. A clean file exits 0. UNKNOWN is never treated as a pass.
"""

from __future__ import annotations

import json
import os
import sys

from . import event_log
from . import forseti_gate as gate


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}

    file_path = data.get("tool_input", {}).get("file_path")
    if not file_path or not gate.is_c_source(file_path):
        return 0

    project_dir = gate.project_dir(data)

    if errors := gate.env_config_errors():
        # Same fail-closed check `stop_gate.main()` opens with, and for the
        # same reason: `verify_and_record` below reads `gate.DEFAULT_K`/
        # `VERIFY_TIMEOUT_S` (env_int/env_float-backed), and those never raise
        # -- a malformed value silently falls back to its default instead. Left
        # unchecked here, this hook would verify and durably record a verdict
        # under that silently-wrong value before the Stop hook ever gets a
        # chance to block on the misconfiguration (issue #95 review).
        print(gate.env_config_error_message(errors), file=sys.stderr)
        return 2

    if not os.path.isabs(file_path):
        file_path = os.path.join(project_dir, file_path)
    if not os.path.exists(file_path):
        return 0

    # Verify + persist each function incrementally under the gate lock (kill-safe,
    # and serialized against concurrent PostToolUse hooks).
    verdicts = gate.verify_and_record(file_path, project_dir=project_dir)

    # Trace the loop (best-effort): the edit Claude made, each ESBMC call, then
    # the gate decision. `duration_s` is per-call accurate even though the events
    # are logged together after the (serialized) verify batch returns.
    rel = gate.unit_id(project_dir, file_path)
    event_log.log_event(
        project_dir,
        event_log.EDIT,
        tool=data.get("tool_name", "?"),
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

    if not verdicts:
        # Either the file defines no functions (any stale units were just
        # reconciled), or the bytes `verify_and_record` gated are no longer the
        # ones on disk, so it has nothing it can honestly report about the file as
        # it now stands. Passing here is safe in both cases, and in the second it
        # is not this list that makes it so: a concurrent run that stamped the new
        # content records — and blocks on — its own units, and if nothing stamped
        # it, the file reads stale and the scan re-offers it. What must not happen
        # is reporting the superseded verdicts, which would exit 2 with a
        # counterexample for code no longer on disk. The `edit` event above logs an
        # empty `functions` list either way.
        return 0

    # NEEDS_CONTRACT (pointer/array units the gate can't check without a harness)
    # is honestly-unverified but NOT a fixable counterexample — never feed it back
    # or block on it; report it loudly instead (issue #122).
    needs = [v for v in verdicts if v.verdict == gate.NEEDS_CONTRACT]
    failures = [
        v for v in verdicts if not v.passed and v.verdict != gate.NEEDS_CONTRACT
    ]

    if not failures:
        verified = [v for v in verdicts if v.passed]
        event_log.log_event(
            project_dir,
            event_log.GATE,
            file=rel,
            decision="pass",
            n_failures=0,
            n_needs_contract=len(needs),
            exit_code=0,
        )
        out = []
        if verified:
            oks = ", ".join(f"{v.unit_id} (k={v.k})" for v in verified)
            out.append(f"Forseti: VERIFIED up to k — {oks}")
        if needs:
            out.append(gate.needs_note(needs))
        if out:
            print("\n".join(out))
        return 0

    lines = [
        f"Forseti: {len(failures)} unit(s) did not verify "
        f"(function-level ESBMC, safety properties).",
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
        "re-verified automatically on the next edit. Do not report the task "
        "done until every unit is VERIFIED up to k. An UNKNOWN is not a pass — "
        "raise k (FORSETI_UNWIND) or simplify the unit."
    )
    if needs:
        lines += ["", gate.needs_note(needs)]
    event_log.log_event(
        project_dir,
        event_log.GATE,
        file=rel,
        decision="block",
        n_failures=len(failures),
        n_needs_contract=len(needs),
        exit_code=2,
    )
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
