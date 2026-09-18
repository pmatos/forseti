#!/usr/bin/env python3
"""e2e assertion test for demo/fixtures/recorded_run.jsonl (stdlib only).

Checks that a recorded Forseti demo trace shows the expected shape of the
write -> verify -> (synth) -> propose -> check loop, end to end. This is
demo-only tooling -- not part of the `forseti` package's own pytest suite --
mirroring demo/render.py's and demo/canvas/server.py's stdlib-only ethos so
it can run with zero extra installs.

Usage: python3 demo/fixtures/assert_recorded_run.py [trace-file]
       (defaults to demo/fixtures/recorded_run.jsonl next to this script)

This trace is a REAL recorded run (task #9): Sonnet 5 writes a UTF-8 decoder,
`forseti synth` verifies it memory-safe, `forseti semantic-loop --mode
propose` asks an LLM for a semantic invariant and checks it -- which reports
UNKNOWN, an honest tooling limitation (issue #299: semantic-loop has no
`--max-len` equivalent to bound an unconstrained length, so its harness
can never fully unwind the fill loop). Nothing here was staged; the
assertions check the *shape* of an honest trace, not a forced pass.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Mirrors `forseti.core.EXIT_CODES` (VERIFIED=0, VIOLATED=1, UNKNOWN=2,
# ERROR=3) -- only the `unknown` outcome exits 2; a `violated` or `error`
# semantic-loop outcome would exit 1 or 3 respectively.
_OUTCOME_EXIT_CODE = {"violated": 1, "unknown": 2, "error": 3}
_OUTCOME_RE = re.compile(r'"outcome":\s*"(\w+)"')


class TraceAssertionError(AssertionError):
    pass


def load_events(path: Path) -> list[dict]:
    events = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise TraceAssertionError(
                f"{path}:{lineno}: malformed JSON: {exc}"
            ) from exc
    return events


def index_of(events: list[dict], **fields: object) -> int:
    for i, e in enumerate(events):
        if all(e.get(k) == v for k, v in fields.items()):
            return i
    raise TraceAssertionError(f"no event matching {fields!r}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise TraceAssertionError(message)


def run(events: list[dict]) -> None:
    check(len(events) > 0, "trace is empty")
    check(
        events[0]["type"] == "session", "first event must be the SessionStart baseline"
    )

    unit = "utf8.c::utf8_decode"

    # write -> the safety-gate hook sees a pointer-taking function it cannot
    # check without a harness (needs_contract), and does not block on it.
    i_edit = index_of(events, type="edit", file="utf8.c")
    check(
        "utf8_decode" in (events[i_edit].get("functions") or []),
        "edit event does not mention utf8_decode",
    )
    i_verify = index_of(events, type="verify", unit=unit, verdict="needs_contract")
    check(i_verify > i_edit, "needs_contract verify must follow the edit")
    i_gate = index_of(events, type="gate", file="utf8.c")
    check(
        events[i_gate]["decision"] == "pass", "needs_contract must not block the gate"
    )
    check(i_gate > i_verify, "gate decision must follow the verify")

    # forseti synth: the deterministic memory-precondition path, run because
    # needs_contract is informational only, never a substitute for it.
    i_synth = index_of(events, type="cli", subcommand="synth")
    synth_ev = events[i_synth]
    check(synth_ev["exit_code"] == 0, "synth call must succeed (exit 0)")
    check(
        "VERIFIED" in synth_ev.get("output_tail", ""),
        "synth output must report VERIFIED",
    )
    check(i_synth > i_gate, "synth call must follow the gate's needs_contract")

    # forseti semantic-loop --mode propose: the LLM-invariant path.
    i_proposed = index_of(events, type="property.proposed", unit_id=unit)
    i_check_start = index_of(events, type="property.check.start", unit_id=unit)
    check(
        i_check_start > i_proposed,
        "property.check.start must follow property.proposed",
    )
    i_verdict = index_of(events, type="property.verdict")
    check(
        i_verdict > i_check_start, "property.verdict must follow property.check.start"
    )

    semantic_loop_calls = [
        e
        for e in events
        if e.get("type") == "cli"
        and e.get("subcommand") == "semantic-loop"
        and "--mode" in (e.get("argv") or [])
    ]
    check(len(semantic_loop_calls) >= 1, "no semantic-loop --mode invocation recorded")
    for call in semantic_loop_calls:
        tail = call.get("output_tail", "")
        match = _OUTCOME_RE.search(tail)
        check(match is not None, "semantic-loop output must report an outcome")
        assert match is not None  # for type-checkers; `check` already raised otherwise
        outcome = match.group(1)
        expected_exit = _OUTCOME_EXIT_CODE.get(outcome)
        check(
            expected_exit is not None,
            f"semantic-loop outcome {outcome!r} has no known non-held exit code",
        )
        check(
            call["exit_code"] == expected_exit,
            f"semantic-loop outcome {outcome!r} should exit {expected_exit}, "
            f"got {call['exit_code']}",
        )
        check(
            outcome == "unknown",
            f"recorded run's semantic-loop outcome must be unknown, got {outcome!r}",
        )

    # Vocabulary/verdict discipline: this trace is honest. Neither the safety
    # gate nor the semantic check ever produced a fabricated pass, and nothing
    # anywhere reports a real defect (see issue #299 -- the UNKNOWN here is a
    # harness ceiling, not evidence about utf8_decode).
    check(
        not any(
            e.get("verdict") == "violated" for e in events if e.get("type") == "verify"
        ),
        "no safety-gate verify should have reported violated in this trace",
    )
    check(
        not any(
            e.get("outcome") == "violated"
            for e in events
            if e.get("type") == "property.verdict"
        ),
        "no semantic property should have reported violated in this trace",
    )
    check(
        all(
            str(e.get("decision", "")).startswith("allow")
            for e in events
            if e.get("type") == "stop"
        ),
        "every Stop-hook decision in this trace must be an allow* variant",
    )


def main(argv: list[str]) -> int:
    default_path = Path(__file__).parent / "recorded_run.jsonl"
    trace_path = Path(argv[1]) if len(argv) > 1 else default_path
    events = load_events(trace_path)
    try:
        run(events)
    except TraceAssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"PASS: {trace_path} ({len(events)} events) match the expected demo-loop shape"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
