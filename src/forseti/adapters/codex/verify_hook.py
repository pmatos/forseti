#!/usr/bin/env python3
"""Forseti — Codex `PostToolUse` verify hook (the enforcing gate).

Codex *does* have tool-use hooks. This one fires after an `apply_patch` edit,
runs the Core `forseti verify` CLI on each edited source unit, and — on a
VIOLATED verdict — returns ``{"decision": "block", "reason": ...}`` so Codex
feeds the counterexample back to the model instead of letting it move on. That
is the verify → counterexample → fix loop, enforced by the harness rather than
by prompt goodwill (the AGENTS.md instructions remain as a fallback).

`forseti enable-project --harness codex` wires this into a project's
`.codex/config.toml` (#212) as:

    [[hooks.PostToolUse]]
    matcher = "apply_patch"

    [[hooks.PostToolUse.hooks]]
    type = "command"
    command = "forseti codex-hook verify"
    timeout = 120

`main()` here is dispatched in-process by `forseti codex-hook verify`
(`core/cli.py`), not invoked as a standalone script — the written command
needs only `forseti` on `PATH`, never an absolute path to this file.

Codex sends the hook one JSON object on **stdin** with `tool_name` and, for
`apply_patch`, a `tool_input.command` holding the patch envelope (whose
`*** Add File:` / `*** Update File:` lines name the edited paths).

Verdict policy — deliberately asymmetric, because the hook fires on *any* edited
file, not a registered verification unit:
  - **VIOLATED** → block. A concrete counterexample is unambiguous: the code is
    wrong, fix it.
  - **UNKNOWN / ERROR** → surface via `systemMessage`, do **not** hard-block. On
    an arbitrary edited file these usually mean "not independently verifiable
    here" (no entry point, k too small) rather than "defective"; blocking on them
    would wedge routine edits. They are reported, never silently passed — precise
    per-unit strictness (with the raise-k ladder) arrives with the unit registry.
  - **VERIFIED** (up to k) → allow.

Any internal error still exits 0 so a broken hook cannot wedge Codex.

Each block/unresolved/pass decision also emits Core's canonical `gate.decision`
event (`core/events.py`, #213) to the hook's own cwd's `.forseti/events.jsonl`
-- the same file `forseti check`/`propose`/`submit` write their own canonical
events into from a project root -- so this harness's per-edit gate reads the
same in a trace as the Claude Code adapter's own `post_tool_use` decision.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from forseti.adapters._cli_json import run_json
from forseti.adapters.gate_reply import render_gate_reply
from forseti.core.events import GATE_DECISION
from forseti.core.events import record_event as record_core_event

# Source kinds Forseti (ESBMC) targets: C -> C++ -> Python.
_SRC_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".py"}

# apply_patch names each touched file on its own header line. `Move to:` is the
# rename destination — capture it too, or a renamed+edited file would only be
# recorded under its old (now-deleted) path and never verified at its new one.
_FILE_RE = re.compile(r"^\*\*\* (?:(?:Add|Update) File|Move to): (.+)$", re.MULTILINE)

_VERIFY_TIMEOUT_S = 120


def _edited_sources(command: str) -> list[str]:
    seen: dict[str, None] = {}
    for raw in _FILE_RE.findall(command or ""):
        path = raw.strip()
        if Path(path).suffix in _SRC_SUFFIXES:
            seen.setdefault(path, None)
    return list(seen)


def _verify(path: str) -> tuple[str, str]:
    """Run `forseti verify --json`; return (verdict, evidence)."""
    result = run_json(
        ["forseti", "verify", path, "--json"],
        timeout=_VERIFY_TIMEOUT_S,
    )
    if result.failure == "launch":
        return (
            "skipped",
            f"could not run forseti verify: {result.diagnostic}",
        )
    if result.failure == "decode":
        return ("skipped", result.diagnostic)

    payload = result.payload
    if not isinstance(payload, dict):
        return ("skipped", result.diagnostic)
    verdict = str(payload.get("verdict", "error"))
    evidence = str(
        payload.get("counterexample")
        or payload.get("reason")
        or payload.get("message")
        or ""
    )
    return (verdict, evidence)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(event, dict) or event.get("tool_name") != "apply_patch":
        return 0
    tool_input = event.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    edited = _edited_sources(command)
    if not edited:
        return 0

    violated: list[tuple[str, str]] = []
    inconclusive: list[tuple[str, str]] = []
    checked: list[str] = []
    for path in edited:
        if not Path(path).exists():
            continue
        checked.append(path)
        verdict, evidence = _verify(path)
        if verdict == "violated":
            violated.append((path, evidence))
        elif verdict in ("unknown", "error", "skipped"):
            # "skipped" = verify could not run (launch/timeout/parse failure).
            # Surface it, never let it fall through as an implicit pass.
            inconclusive.append((path, verdict))

    reply = render_gate_reply(
        violated,
        inconclusive,
        violation_header="Forseti found a counterexample — fix it before continuing:",
        verb="verify",
    )
    if reply.decision == "block":
        _record_gate_decision(checked, "block")
        print(json.dumps({"decision": "block", "reason": reply.message}))
        return 0

    if reply.decision == "unresolved":
        _record_gate_decision(checked, "unresolved")
        print(json.dumps({"systemMessage": reply.message}))
        return 0

    if not checked:
        # Every edited path was gone by the time this hook ran (e.g. a rename's
        # old name, or a file deleted later in the same turn) -- nothing was
        # actually verified, so this must not read as a pass in the canonical
        # trace (issue #252 review; AGENTS.md: an unverified unit is never a
        # silent pass).
        _record_gate_decision(edited, "unresolved")
        print(
            json.dumps(
                {
                    "systemMessage": (
                        "Forseti: no edited source still exists to verify "
                        f"({', '.join(edited)}). Not a pass."
                    )
                }
            )
        )
        return 0

    _record_gate_decision(checked, "pass")
    return 0


def _record_gate_decision(files: list[str], decision: str) -> None:
    """Core's canonical `gate.decision` event (`core/events.py`, #213).

    `files` are the raw edited source paths, not canonical `path::symbol` unit
    IDs: `_verify` above checks a whole file at a time (module docstring), so
    there is no per-function verdict to key one by. Recorded under `files`
    rather than `unit_ids` so this event is honest about its own granularity
    instead of implying it joins with `property.proposed`/`property.verdict`,
    which are keyed by real units (issue #252 review).

    Written to the hook process's own cwd's `.forseti/events.jsonl` -- the
    same directory `forseti check`/`propose`/`submit` write their own
    canonical events into when run from a project root, which is how Codex
    invokes this hook (module docstring: `_verify` shells out to `forseti
    verify` without an explicit `cwd`, i.e. inherits the hook's own).
    """
    record_core_event(
        Path.cwd() / ".forseti",
        GATE_DECISION,
        harness="codex",
        adapter="codex-post-tool-use",
        files=list(files),
        decision=decision,
    )


if __name__ == "__main__":
    raise SystemExit(main())
