#!/usr/bin/env python3
"""Forseti — Codex `notify` reference hook (a *partial* gate).

Codex has no tool-use hook that can block a turn, but it does invoke a `notify`
program at the end of each agent turn with a single JSON argument. This script is
the most a Codex adapter can do toward a Stop-gate: it fires *after* the turn, so
it cannot prevent the agent handing back unverified code — it can only surface a
reminder so a human notices. Real enforcement stays with the prompt+tools
fallback in ./AGENTS.md; a hard gate needs the Claude Code adapter's `Stop` hook
(#45).

Wire it in ~/.codex/config.toml:

    notify = ["python3", "/absolute/path/to/adapters/codex/notify.py"]

Codex passes one JSON arg with `type` (currently only "agent-turn-complete"),
`thread-id`, `turn-id`, `cwd`, `input-messages`, and `last-assistant-message`.
The script never fails the caller: any error still exits 0 so a broken notify
config cannot wedge Codex.
"""

from __future__ import annotations

import json
import sys

# Words that suggest the agent is declaring completion — the moment the emulated
# Stop-gate most needs a human to confirm every changed unit is VERIFIED up to k.
_DONE_HINTS = ("done", "complete", "finished", "ready", "all set", "lgtm")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        event = json.loads(argv[1])
    except (ValueError, TypeError):
        return 0
    if not isinstance(event, dict) or event.get("type") != "agent-turn-complete":
        return 0

    last = str(event.get("last-assistant-message") or "").lower()
    claims_done = any(hint in last for hint in _DONE_HINTS)

    print(
        "[forseti] Codex turn complete — the verification gate here is advisory "
        "(prompt+tools fallback, no blocking hook).",
        file=sys.stderr,
    )
    if claims_done:
        print(
            "[forseti] The turn reads as 'done'. Confirm every changed unit is "
            "VERIFIED up to k (no VIOLATED, no UNKNOWN) before trusting it.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
