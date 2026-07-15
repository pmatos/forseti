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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forseti_gate as gate

_CEX_CLIP = 1500


def _project_dir(data: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}

    file_path = data.get("tool_input", {}).get("file_path")
    if not file_path or not gate.is_c_source(file_path):
        return 0

    project_dir = _project_dir(data)
    if not os.path.isabs(file_path):
        file_path = os.path.join(project_dir, file_path)
    if not os.path.exists(file_path):
        return 0

    # Verify + persist each function incrementally under the gate lock (kill-safe,
    # and serialized against concurrent PostToolUse hooks).
    verdicts = gate.verify_and_record(file_path, project_dir=project_dir)
    if not verdicts:
        return 0  # no functions in the file; any stale units were just reconciled

    failures = [v for v in verdicts if not v.passed]
    if not failures:
        oks = ", ".join(f"{v.unit_id} (k={v.k})" for v in verdicts)
        print(f"Forseti: VERIFIED up to k — {oks}")
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
            lines.append(v.counterexample.strip()[:_CEX_CLIP])
        elif v.detail:
            lines.append(f"  {v.detail}")
        lines.append("")
    lines.append(
        "Fix the unit(s) to eliminate the counterexample; they will be "
        "re-verified automatically on the next edit. Do not report the task "
        "done until every unit is VERIFIED up to k. An UNKNOWN is not a pass — "
        "raise k (FORSETI_UNWIND) or simplify the unit."
    )
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
