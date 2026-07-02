#!/usr/bin/env python3
"""Forseti — Codex `PostToolUse` verify hook (the enforcing gate).

Codex *does* have tool-use hooks. This one fires after an `apply_patch` edit,
runs the Core `forseti verify` CLI on each edited source unit, and — on a
VIOLATED verdict — returns ``{"decision": "block", "reason": ...}`` so Codex
feeds the counterexample back to the model instead of letting it move on. That
is the verify → counterexample → fix loop, enforced by the harness rather than
by prompt goodwill (the AGENTS.md instructions remain as a fallback).

Wire it in ~/.codex/config.toml (see ./config.toml.example):

    [[hooks.PostToolUse]]
    matcher = "apply_patch"

    [[hooks.PostToolUse.hooks]]
    type = "command"
    command = 'python3 "/absolute/path/to/adapters/codex/verify_hook.py"'
    timeout = 120

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
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

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
    try:
        proc = subprocess.run(
            ["forseti", "verify", path, "--json"],
            capture_output=True,
            text=True,
            timeout=_VERIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ("skipped", f"could not run forseti verify: {exc}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return ("skipped", (proc.stderr or proc.stdout).strip()[:400])
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

    violated: list[tuple[str, str]] = []
    inconclusive: list[tuple[str, str]] = []
    for path in _edited_sources(command):
        if not Path(path).exists():
            continue
        verdict, evidence = _verify(path)
        if verdict == "violated":
            violated.append((path, evidence))
        elif verdict in ("unknown", "error", "skipped"):
            # "skipped" = verify could not run (launch/timeout/parse failure).
            # Surface it, never let it fall through as an implicit pass.
            inconclusive.append((path, verdict))

    if violated:
        lines = ["Forseti found a counterexample — fix it before continuing:"]
        for path, evidence in violated:
            lines.append(f"\n### VIOLATED: {path}\n{evidence}")
        if inconclusive:
            residual = ", ".join(f"{p} [{v}]" for p, v in inconclusive)
            lines.append(f"\nAlso inconclusive (do not ignore): {residual}")
        print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
        return 0

    if inconclusive:
        residual = ", ".join(f"{p} [{v}]" for p, v in inconclusive)
        print(
            json.dumps(
                {
                    "systemMessage": (
                        f"Forseti could not conclusively verify: {residual}. "
                        "Not a pass — raise k, add an entry/harness, or report."
                    )
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
