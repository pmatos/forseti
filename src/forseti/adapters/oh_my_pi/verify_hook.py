#!/usr/bin/env python3
"""Forseti — Oh My Pi's `tool_result` gate (the enforcing gate, #249).

Oh My Pi extensions intercept tool calls through a pre/post pair: `tool_call`
(pre-execution, can `block`) and `tool_result` (post-execution, can override
the result the model sees). A write/edit has already landed on disk by the
time `tool_result` fires, so — like Codex's `PostToolUse` hook, and unlike
Claude Code's in-process `post_tool_use` — there is nothing left to block;
this hook instead rewrites the tool's own reported result so the model sees
the counterexample as part of what it just did, and (verified against Oh My
Pi's current `ExtensionToolWrapper` source, `packages/coding-agent/src/
extensibility/extensions/wrapper.ts`, not the legacy `HookToolWrapper` its
`docs/hooks.md` describes) can set `isError: true` on that result, which the
wrapper *does* apply. That makes this gate comparably strong to Codex's
`{"decision": "block", ...}` — the model reads the edit itself as having
failed, not merely as an FYI aside.

`forseti enable-project --harness oh-my-pi` wires the companion TypeScript
extension (`adapters/oh-my-pi/forseti-gate.ts`, packaged by `install.py`) into
a project's `.omp/extensions/forseti-gate.ts`. That extension registers a
`tool_result` handler for the `write`/`edit` tools, serializes the event to
JSON on stdin, and runs `forseti omp-hook tool-result` (dispatched here by
`main()`, `core/cli.py`) — the written extension needs only `forseti` on
`PATH`, never an absolute path to this file.

The extension sends one JSON object on **stdin**:

    {"toolName": "write" | "edit", "input": {...}, "isError": bool,
     "cwd": "<project dir the OMP session is running in>"}

`write`'s `input.path` names the edited file directly. `edit`'s `input.input`
is Oh My Pi's own hashline patch DSL — one or more `[PATH#TAG]` sections, plus
optional `MV DEST` rename operations — so paths are extracted with a regex
over that string, the same shape Codex's own `apply_patch` envelope needs
(`adapters/codex/verify_hook.py`'s `_FILE_RE`).

Verdict policy, checking only what is already *stored* (`--mode check-only` —
this gate never proposes new candidates itself, so a project that never
`propose`s/`submit`s stays at zero cost, the same store-presence opt-in
`adapters/claude_code/property_gate.py` documents):
  - **violated** → block (`isError: true`), with the counterexample.
  - **unknown** / **error** → surfaced, never silently passed, but does not
    block — an inconclusive semantic check on an arbitrary edited file usually
    means "not settled yet" rather than "defective".
  - **held** / **empty** (nothing stored to check) → allow, silently.

Any internal error still exits 0 so a broken hook cannot wedge Oh My Pi.

Each block/unresolved/pass decision also emits Core's canonical `gate.decision`
event (`core/events.py`, #213) to `<cwd>/.forseti/events.jsonl` — the same file
the Claude Code and Codex adapters' own gates write to — so this harness's
per-edit gate reads the same in a trace as the other two. Unlike Codex (which
verifies a whole edited file with no per-function enumeration), this gate
calls `forseti list-units` first, so its `unit_ids` are real `path::symbol`
keys, joinable with `property.proposed`/`property.verdict` the same way Claude
Code's own `post_tool_use` event is.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from forseti.core.events import GATE_DECISION
from forseti.core.events import record_event as record_core_event

# Source kinds Forseti (ESBMC) targets or plans to: C -> C++ -> Python (ADR-0003).
_SRC_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".py"}

# Oh My Pi's hashline edit DSL: one or more `[PATH#TAG]` section headers, where
# TAG is a four-hex-digit snapshot tag (docs/tools/edit.md). `MV DEST` renames
# the *current* section's file -- captured too, so a renamed+edited file is
# recorded under its new path, not only its old (now-superseded) one.
_HASHLINE_PATH_RE = re.compile(r"^\[(.+?)#[0-9A-Fa-f]{4}\]", re.MULTILINE)
_MV_RE = re.compile(r"^MV\s+(\S+)", re.MULTILINE)

_LIST_UNITS_TIMEOUT_S = 60
_SEMANTIC_LOOP_TIMEOUT_S = 120


def _edited_sources(tool_name: str, tool_input: dict[str, object]) -> list[str]:
    """Edited source paths for one `write`/`edit` `tool_result` event.

    `write`'s `input.path` names the file directly (docs/tools/write.md).
    `edit`'s `input.input` is the hashline DSL described above -- every
    `[PATH#TAG]` header names a touched file, and a trailing `MV DEST` inside
    that same section supersedes it with the rename target, mirroring
    `apply_patch`'s `*** Move to:` handling in the Codex adapter.
    """
    seen: dict[str, None] = {}
    if tool_name == "write":
        path = tool_input.get("path")
        if isinstance(path, str) and path:
            seen.setdefault(path, None)
        return [p for p in seen if Path(p).suffix in _SRC_SUFFIXES]

    if tool_name != "edit":
        return []
    raw = tool_input.get("input")
    if not isinstance(raw, str):
        return []
    headers = list(_HASHLINE_PATH_RE.finditer(raw))
    for i, match in enumerate(headers):
        section_end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        section = raw[match.end() : section_end]
        mv = _MV_RE.search(section)
        path = mv.group(1).strip() if mv else match.group(1).strip()
        seen.setdefault(path, None)
    return [p for p in seen if Path(p).suffix in _SRC_SUFFIXES]


def _list_functions(path: str, cwd: str) -> list[str] | None:
    """`forseti list-units --json`'s function names, or `None` on any failure."""
    try:
        proc = subprocess.run(
            ["forseti", "list-units", path, "--json"],
            capture_output=True,
            text=True,
            timeout=_LIST_UNITS_TIMEOUT_S,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    units = payload.get("units") if isinstance(payload, dict) else None
    if not isinstance(units, list):
        return None
    names = [u["function"] for u in units if isinstance(u, dict) and "function" in u]
    return names


def _semantic_check(path: str, function: str, cwd: str) -> tuple[str, str]:
    """`forseti semantic-loop --mode check-only --json`; return (outcome, evidence).

    `outcome` is Core's own worst-outcome-wins field (`held` | `violated` |
    `unknown` | `error` | `empty`, `core/loop.py`) — never re-derived from
    `verdicts[]` here. `evidence` is the first non-held verdict's raw
    counterexample/skip reason, best-effort.
    """
    try:
        proc = subprocess.run(
            [
                "forseti",
                "semantic-loop",
                path,
                "--function",
                function,
                "--mode",
                "check-only",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=_SEMANTIC_LOOP_TIMEOUT_S,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ("error", f"could not run forseti semantic-loop: {exc}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return ("error", (proc.stderr or proc.stdout).strip()[:400])
    outcome = payload.get("outcome") if isinstance(payload, dict) else None
    if not isinstance(outcome, str):
        return ("error", (proc.stderr or proc.stdout).strip()[:400])
    evidence = ""
    check = payload.get("check") if isinstance(payload, dict) else None
    verdicts = check.get("verdicts") if isinstance(check, dict) else None
    if isinstance(verdicts, list):
        for verdict in verdicts:
            if not isinstance(verdict, dict) or verdict.get("outcome") == "held":
                continue
            result = verdict.get("result")
            if isinstance(result, dict) and result.get("raw_counterexample"):
                evidence = str(result["raw_counterexample"])
                break
            if verdict.get("skip_reason"):
                evidence = str(verdict["skip_reason"])
                break
    return (outcome, evidence)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0
    tool_name = event.get("toolName")
    if tool_name not in ("write", "edit"):
        return 0
    if event.get("isError"):
        return 0
    tool_input = event.get("input")
    cwd = event.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return 0

    edited = _edited_sources(str(tool_name), tool_input)
    if not edited:
        return 0

    if not (Path(cwd) / ".forseti" / "forseti.db").exists():
        # Store-presence opt-in (module docstring): nothing was ever proposed
        # or submitted for this project, so there is nothing this check-only
        # gate could report -- zero subprocess cost, same as today's behaviour
        # for a project that never opts into semantic properties.
        return 0

    violated: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    checked_units: list[str] = []
    any_path_existed = False
    for path in edited:
        resolved = Path(path) if Path(path).is_absolute() else Path(cwd) / path
        if not resolved.exists():
            continue
        any_path_existed = True
        functions = _list_functions(path, cwd)
        if functions is None:
            # `list-units` itself failed to run/parse -- distinct from a file
            # that genuinely has no functions. Surface it like any other
            # unresolved outcome; never let an enumeration failure read as
            # "nothing to check" (module docstring: never silently passed).
            unresolved.append((path, "list-units-failed"))
            continue
        if not functions:
            continue
        for function in functions:
            outcome, evidence = _semantic_check(path, function, cwd)
            if outcome == "empty":
                continue
            unit_id = f"{path}::{function}"
            checked_units.append(unit_id)
            if outcome == "violated":
                violated.append((unit_id, evidence))
            elif outcome in ("unknown", "error"):
                unresolved.append((unit_id, outcome))

    if violated:
        lines = [
            "Forseti found a counterexample in a stored semantic property "
            "for a unit you just edited — fix it before continuing:"
        ]
        for unit_id, evidence in violated:
            lines.append(f"\n### VIOLATED: {unit_id}\n{evidence}")
        if unresolved:
            residual = ", ".join(f"{u} [{o}]" for u, o in unresolved)
            lines.append(f"\nAlso inconclusive (do not ignore): {residual}")
        _record_gate_decision(cwd, checked_units, "block")
        print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
        return 0

    if unresolved:
        residual = ", ".join(f"{u} [{o}]" for u, o in unresolved)
        _record_gate_decision(cwd, checked_units, "unresolved")
        print(
            json.dumps(
                {
                    "systemMessage": (
                        f"Forseti could not conclusively check: {residual}. "
                        "Not a pass — raise k, add an entry/harness, or report."
                    )
                }
            )
        )
        return 0

    if not any_path_existed:
        # Every edited path was gone by the time this hook ran (e.g. a
        # rename's old name, or a REM'd file) -- nothing was actually
        # checked, so this must not read as a pass in the canonical trace
        # (mirrors the Codex adapter's own "nothing existed" branch).
        _record_gate_decision(cwd, [], "unresolved")
        print(
            json.dumps(
                {
                    "systemMessage": (
                        "Forseti: no edited source still exists to check "
                        f"({', '.join(edited)}). Not a pass."
                    )
                }
            )
        )
        return 0

    if checked_units:
        _record_gate_decision(cwd, checked_units, "pass")
    return 0


def _record_gate_decision(cwd: str, unit_ids: list[str], decision: str) -> None:
    """Core's canonical `gate.decision` event (`core/events.py`, #213).

    Written to `<cwd>/.forseti/events.jsonl` using the *event's own* `cwd`
    field, not this process's own working directory -- Oh My Pi's extension
    runtime hands the session's project directory to every handler via
    `ExtensionContext.cwd`, and the packaged `forseti-gate.ts` forwards it
    verbatim, so there is no ambient-cwd assumption to document here the way
    Codex's own hook (which inherits Codex's own launch cwd) has to.
    """
    record_core_event(
        Path(cwd) / ".forseti",
        GATE_DECISION,
        harness="oh-my-pi",
        adapter="oh-my-pi-tool-result",
        unit_ids=list(unit_ids),
        decision=decision,
    )


if __name__ == "__main__":
    raise SystemExit(main())
