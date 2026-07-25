#!/usr/bin/env python3
"""SessionStart hook: baseline the pre-session dirty C tree (issue #99).

The out-of-band scan (``post_bash`` + the Stop-gate backstop) gates every C file
`git status` reports as changed whose content differs from the recorded
``scanned`` hash. At session start that set includes C that was dirty *before* the
session and the agent never touched — gating it would resurrect the very
over-reach this issue set out to avoid (a pure conversational turn blocking on the
user's WIP, the first `Bash` call verifying code Claude never opened).

This hook records each already-dirty C file's current content as the baseline, so
the scan fires only once the agent actually changes a file — scoping the gate to
"changed **since session start**". It baselines on a fresh/cleared session only;
on resume it leaves the live ``scanned`` map untouched so a mid-session
out-of-band change is not masked.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import event_log
import forseti_gate as gate

# A new or cleared context — (re)establish the baseline. On "resume"/"compact"
# the session's `scanned` map is still live, so we must not overwrite it.
_FRESH_SOURCES = {"startup", "clear"}


def _project_dir(data: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    # Default to a baseline when the source is unknown: missing the baseline is
    # the harmful failure (pre-existing C gets gated), a redundant one is benign.
    if str(data.get("source", "startup")) not in _FRESH_SOURCES:
        return 0

    project_dir = _project_dir(data)
    n = gate.baseline_scanned(project_dir)
    if n is None:
        # Not a git repo — the out-of-band scan is inactive anyway; nothing to seed.
        return 0
    event_log.log_event(
        project_dir,
        event_log.SESSION,
        decision="baseline",
        n_baselined=n,
        source=str(data.get("source", "startup")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
