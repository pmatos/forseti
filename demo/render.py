#!/usr/bin/env python3
"""Render `.forseti/events.jsonl` as a live sequence diagram for the demo pane.

This is demo/e2e-test tooling, not part of the `forseti` package: a stdlib-only
(no pip deps) script that turns the interleaved trace a Forseti-gated Claude Code
session writes to `<project_dir>/.forseti/events.jsonl` into a readable,
incrementally-appended timeline, meant to run in one half of a split
tmux/terminal layout next to a real `claude` session.

Two modes:

- Live (default): `render.py <project_dir>` tails
  `<project_dir>/.forseti/events.jsonl` as it grows -- waiting/polling if the
  file does not exist yet, and recovering if it is truncated or rotated (a
  fresh `.forseti/` from a new demo run).
- Replay: `render.py --replay <trace-file>` plays back a previously captured
  `events.jsonl` -- instantly by default, or paced with `--speed N` (delay
  between events scaled by their real recorded timestamp deltas, capped so a
  long gap in the original session doesn't stall the replay). This is the
  rehearsal/fallback mode for when a live demo run doesn't cooperate.

It reads three interleaved event families, all JSON-per-line with a `ts`
epoch-seconds float and a `type` field:

1. The Claude Code adapter's own hook trace (`session`/`edit`/`verify`/`gate`/
   `stop`) -- schema: `src/forseti/adapters/claude_code/event_log.py`.
2. This demo's `cli` events from the `demo/bin/forseti` PATH shim, covering
   `forseti synth`/`discharge`/etc invocations the package doesn't instrument
   on its own.
3. Forseti Core's canonical cross-harness events (`property.proposed`,
   `property.check.start`, `property.verdict`, `gate.decision`) -- schema:
   `src/forseti/core/events.py`.

Vocabulary discipline (see this repo's CLAUDE.md): ESBMC never "proves" or
"corrects" anything -- it returns a verdict, bounded up to k. This renderer
never prints "proven"/"correct"; it always prints VERIFIED/VIOLATED/UNKNOWN
(or NEEDS_CONTRACT/ERROR), and UNKNOWN/NEEDS_CONTRACT/ERROR are always styled
as a distinct, unresolved state -- never colored as if they passed. The
adapter's `needs_contract` (a safety-gate verdict: no harness for a
pointer-taking function, non-blocking, *not* evidence of safety) and a `cli`
event's own `assessment` field from `forseti synth` (parsed best-effort out of
its `output_tail`, since synth has no dedicated event type) are two distinct
claims from two different tools and are labeled distinctly here -- never
conflated as the same "needs_contract".

A malformed or unrecognized-`type` line is skipped, never a crash -- mirrors
`event_log.read_events_file`'s own tolerance of a torn trailing write.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from _shared import TailState, compute_replay_delays

# --------------------------------------------------------------------------
# ANSI styling -- plain codes only, no external libraries.
# --------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"

_COLOR_ENABLED = True


def _sty(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED or not codes:
        return text
    return "".join(codes) + text + RESET


def c(text: str, color: str) -> str:
    """Wrap `text` in one color, if coloring is enabled."""
    return _sty(text, color)


def tag(text: str, color: str) -> str:
    """A bold, colored event-kind label (AGENT, ESBMC, GATE, ...)."""
    return _sty(text, BOLD, color)


# Verdict discipline: VERIFIED is the only green outcome. UNKNOWN,
# NEEDS_CONTRACT and ERROR are all yellow -- distinct-but-unresolved, never
# styled as a pass. VIOLATED is red.
VERDICT_COLOR = {
    "verified": GREEN,
    "violated": RED,
    "unknown": YELLOW,
    "error": YELLOW,
    "needs_contract": YELLOW,
}

# `forseti synth`/`discharge` assessment vocabulary (precond/verify.py's
# Assessment enum) -- a different claim from the gate's own `needs_contract`,
# so it gets its own color map (same color *policy*, different source).
ASSESSMENT_COLOR = {
    "assumed_verified": GREEN,
    "discharged_verified": GREEN,
    "violated": RED,
    "vacuous": YELLOW,
    "unknown": YELLOW,
    "needs_contract": YELLOW,
    "error": YELLOW,
}

# `property.verdict`'s `outcome` (held/violated/unknown/error/skipped).
OUTCOME_COLOR = {
    "held": GREEN,
    "violated": RED,
    "unknown": YELLOW,
    "error": YELLOW,
    "skipped": YELLOW,
}

# Subcommands whose exit code follows forseti's own VERIFIED=0/VIOLATED=1/
# UNKNOWN=2/ERROR=3 contract (src/forseti/esbmc/render.py's EXIT_CODES,
# src/forseti/precond/verify.py's ASSESSMENT_EXIT_CODES agrees on 0-3) --
# used by render_cli's fallback when a `cli` event's output_tail has no
# regex-matchable `assessment` (i.e. not a `--json` invocation).
_VERDICT_EXIT_SUBCOMMANDS = {"verify", "check", "semantic-loop", "synth", "discharge"}

GATE_DECISION_COLOR = {"pass": GREEN, "block": RED}

STOP_DECISION_COLOR = {
    "allow": GREEN,
    "allow_needs_contract": YELLOW,
    "allow_semantic_check": YELLOW,
    "block": RED,
    "residual": RED,
    "pruned_deleted": YELLOW,
    "oob_scan_skipped": YELLOW,
}

TOOL_VERB = {"Write": "wrote", "Edit": "edited", "MultiEdit": "edited (multi)"}

_KNOWN_ASSESSMENTS = set(ASSESSMENT_COLOR)
_ASSESSMENT_RE = re.compile(r'"assessment"\s*:\s*"([a-zA-Z_]+)"')


def _parse_assessment(output_tail: str) -> str | None:
    """Best-effort pull of `forseti synth`/`discharge`'s `assessment` out of a
    `cli` event's `output_tail`.

    `output_tail` is plain captured stdout+stderr, not a guaranteed JSON
    payload (only `--json` runs emit one, and even then it may be the *tail*
    of a truncated line if a huge counterexample pushed the front of the line
    out of the kept window). A regex search is more robust to that than
    requiring `output_tail` to itself be valid JSON.
    """
    if not output_tail:
        return None
    match = _ASSESSMENT_RE.search(output_tail)
    if match and match.group(1) in _KNOWN_ASSESSMENTS:
        return match.group(1)
    return None


def _ts(ev: dict[str, Any]) -> str:
    raw = ev.get("ts")
    if not isinstance(raw, (int, float)):
        return "??:??:??"
    try:
        return time.strftime("%H:%M:%S", time.localtime(raw))
    except (ValueError, OSError, OverflowError):
        return "??:??:??"


# --------------------------------------------------------------------------
# Per-event-type rendering. Each returns a plain message (no timestamp
# prefix) or None to render nothing for this line. `state` carries small
# cross-event bookkeeping (currently: the last adapter-local `gate` decision,
# for collapsing the duplicate `gate.decision` report of the same edit).
# --------------------------------------------------------------------------


def render_session(ev: dict[str, Any]) -> str:
    decision = str(ev.get("decision", "baseline"))
    n = ev.get("n_baselined", "?")
    source = ev.get("source", "?")
    return f"{tag('SESSION', CYAN)} {decision} (n_baselined={n}, source={source})"


def render_edit(ev: dict[str, Any]) -> str:
    tool = str(ev.get("tool", "?"))
    verb = TOOL_VERB.get(tool, tool)
    file = ev.get("file", "?")
    fns = ev.get("functions") or []
    fn_part = (
        f" (functions: {', '.join(str(f) for f in fns)})" if fns else " (no functions)"
    )
    return f"{tag('AGENT', BLUE)} {verb} {file}{fn_part}"


def render_verify(ev: dict[str, Any]) -> str:
    unit = ev.get("unit", "?")
    verdict = str(ev.get("verdict", "?")).lower()
    color = VERDICT_COLOR.get(verdict, YELLOW)
    note = ""
    if verdict == "needs_contract":
        note = " (gate: no harness for pointer/array param -- not evidence of safety)"
    k = ev.get("k")
    k_s = k if k is not None else "?"
    dur = ev.get("duration_s")
    dur_s = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "?"
    verdict_s = c(verdict.upper(), color)
    return (
        f"{tag('ESBMC', MAGENTA)} verify {unit} -> {verdict_s} (k={k_s}, {dur_s}){note}"
    )


def render_gate(ev: dict[str, Any], state: dict[str, Any]) -> str:
    file = ev.get("file", "?")
    decision = str(ev.get("decision", "?"))
    color = GATE_DECISION_COLOR.get(decision, YELLOW)
    parts = []
    n_fail = ev.get("n_failures")
    n_needs = ev.get("n_needs_contract")
    if n_fail:
        parts.append(f"failures={n_fail}")
    if n_needs:
        parts.append(f"needs_contract={n_needs}")
    suffix = f" ({', '.join(parts)})" if parts else ""
    # Remember this (file, decision) so a same-edit `gate.decision` (Core's
    # canonical re-report of the same PostToolUse decision) can be collapsed
    # into this line instead of shown separately.
    state["last_gate"] = (file, decision, ev.get("ts"))
    return f"{tag('GATE', CYAN)} {c(decision, color)} {file}{suffix}"


def render_gate_decision(ev: dict[str, Any], state: dict[str, Any]) -> str | None:
    file = ev.get("file")
    decision = str(ev.get("decision", "?"))
    last = state.get("last_gate")
    if file is not None and last is not None:
        last_file, last_decision, last_ts = last
        close_in_time = True
        with contextlib.suppress(TypeError, ValueError):
            close_in_time = abs(float(ev.get("ts", 0)) - float(last_ts)) <= 5.0
        if last_file == file and last_decision == decision and close_in_time:
            return None  # same edit's decision, already shown by the `gate` line
    color = GATE_DECISION_COLOR.get(decision, YELLOW)
    adapter = ev.get("adapter", "?")
    unit_ids = ev.get("unit_ids") or []
    scope = file if file else f"{len(unit_ids)} unit(s)"
    return f"{tag('GATE', CYAN)} [{adapter}] {c(decision, color)} {scope}"


def render_stop(ev: dict[str, Any]) -> str:
    decision = str(ev.get("decision", "?"))
    color = STOP_DECISION_COLOR.get(decision, YELLOW)
    bits = []
    for key, label in (
        ("n_needs_contract", "needs_contract"),
        ("n_semantic_violations", "semantic_violated"),
        ("n_semantic_unresolved", "semantic_unresolved"),
        ("n_semantic_failed", "semantic_failed"),
        ("n_semantic_skipped", "semantic_skipped"),
        ("n_unverified", "unverified"),
        ("n_oob", "oob"),
        ("attempt", "attempt"),
    ):
        v = ev.get(key)
        # `attempt` is a real 0-based counter (0 == the first attempt), so it
        # must show even when falsy; every other field here is a count where
        # 0 genuinely means "nothing to report" and should stay hidden.
        show = v is not None if key == "attempt" else bool(v)
        if show:
            bits.append(f"{label}={v}")
    suffix = f" ({', '.join(bits)})" if bits else ""
    return f"{tag('STOP', CYAN)} {c(decision, color)}{suffix}"


def render_cli(ev: dict[str, Any]) -> str:
    argv = ev.get("argv") or []
    cmd = (
        " ".join(str(a) for a in argv)
        if argv
        else str(ev.get("subcommand") or "forseti")
    )
    exit_code = ev.get("exit_code")
    dur = ev.get("duration_s")
    dur_s = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "?"
    assessment = _parse_assessment(str(ev.get("output_tail") or ""))
    if assessment is not None:
        color = ASSESSMENT_COLOR.get(assessment, YELLOW)
        result = f"assessment: {c(assessment, color)}"
    elif exit_code == 0:
        result = c("ok", GREEN)
    elif str(ev.get("subcommand") or "") in _VERDICT_EXIT_SUBCOMMANDS and exit_code in (
        1,
        2,
        3,
    ):
        # These subcommands share forseti's own VIOLATED=1/UNKNOWN=2/ERROR=3
        # exit-code contract (src/forseti/esbmc/render.py's EXIT_CODES); a
        # bare exit-code check would otherwise collapse VIOLATED and UNKNOWN
        # into the same "failed" label -- exactly the distinction the rest
        # of this file's verdict/assessment color maps exist to preserve.
        label, color = {
            1: ("VIOLATED", RED),
            2: ("UNKNOWN", YELLOW),
            3: ("ERROR", YELLOW),
        }[exit_code]
        result = c(label, color)
    else:
        result = c("failed", YELLOW)
    return f"{tag('CLI', BLUE)} {cmd} -> {result} (exit {exit_code}, {dur_s})"


def render_property_proposed(ev: dict[str, Any]) -> str:
    unit_id = ev.get("unit_id", "?")
    property_id = ev.get("property_id", "?")
    expr = str(ev.get("expression", ""))
    if len(expr) > 40:
        expr = expr[:37] + "..."
    provider = ev.get("provider", "?")
    model = ev.get("model", "?")
    channel = ev.get("channel", "?")
    head = tag("PROPOSE", MAGENTA)
    return f'{head} {unit_id}::{property_id} "{expr}" ({channel}, {provider}/{model})'


def render_property_check_start(ev: dict[str, Any]) -> str:
    return f"{tag('CHECK', MAGENTA)} start {ev.get('unit_id', '?')}"


def render_property_verdict(ev: dict[str, Any]) -> str:
    unit_id = ev.get("unit_id", "?")
    property_id = ev.get("property_id", "?")
    outcome = str(ev.get("outcome", "?")).lower()
    color = OUTCOME_COLOR.get(outcome, YELLOW)
    k = ev.get("k")
    k_s = k if k is not None else "?"
    outcome_s = c(outcome.upper(), color)
    head = tag("CHECK", MAGENTA)
    return f"{head} {unit_id}::{property_id} -> {outcome_s} (k={k_s})"


_RENDERERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str | None]] = {
    "session": lambda ev, st: render_session(ev),
    "edit": lambda ev, st: render_edit(ev),
    "verify": lambda ev, st: render_verify(ev),
    "gate": render_gate,
    "gate.decision": render_gate_decision,
    "stop": lambda ev, st: render_stop(ev),
    "cli": lambda ev, st: render_cli(ev),
    "property.proposed": lambda ev, st: render_property_proposed(ev),
    "property.check.start": lambda ev, st: render_property_check_start(ev),
    "property.verdict": lambda ev, st: render_property_verdict(ev),
}


def render_event(ev: dict[str, Any], state: dict[str, Any]) -> str | None:
    """One full display line for `ev`, or None to render nothing.

    Never raises: an unrecognized `type` or a handler that trips on a
    surprising payload shape both degrade to "skip this line" rather than
    crashing the renderer mid-demo.
    """
    fn = _RENDERERS.get(str(ev.get("type", "")))
    if fn is None:
        return None
    try:
        msg = fn(ev, state)
    except Exception:
        return None
    if msg is None:
        return None
    return f"{c(f'[{_ts(ev)}]', DIM)} {msg}"


# --------------------------------------------------------------------------
# JSONL parsing -- tolerant of malformed/partial lines, like
# event_log.read_events_file.
# --------------------------------------------------------------------------


def _parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def read_events_file(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"render.py: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    events = []
    for line in text.splitlines():
        ev = _parse_line(line)
        if ev is not None:
            events.append(ev)
    return events


def tail_events(path: Path, poll_interval: float = 0.3) -> Iterator[dict[str, Any]]:
    """Yield parsed events appended to `path`, live, forever.

    Handles the file not existing yet (polls until it appears), in-place
    truncation (size shrinks -- reset to the start), rotation (a new inode at
    the same path -- reopen from the start), and a truncate-then-rewrite that
    leaves the file no smaller than before (`TailState`, shared with
    `canvas/server.py`'s own live tail -- see its docstring). A torn trailing
    line from a write still in flight is buffered until its newline arrives
    rather than parsed early.
    """
    tail_state = TailState()
    pos = 0
    buf = ""
    waiting = False
    while True:
        try:
            reset, st = tail_state.observe(path)
        except OSError:
            if not waiting:
                print(c(f"-- waiting for {path} --", DIM))
                waiting = True
            time.sleep(poll_interval)
            continue
        if waiting:
            print(c(f"-- {path} appeared, tailing --", DIM))
            waiting = False

        if reset:
            print(c("-- trace file changed, resuming from start --", DIM))
            pos = 0
            buf = ""

        if st.st_size == pos:
            time.sleep(poll_interval)
            continue

        try:
            with open(path, encoding="utf-8") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except OSError:
            time.sleep(poll_interval)
            continue

        buf += chunk
        lines = buf.split("\n")
        buf = lines.pop()  # last, possibly-partial line carries over
        for line in lines:
            ev = _parse_line(line)
            if ev is not None:
                yield ev


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

_MAX_REPLAY_DELAY_S = 3.0


def run_live(project_dir: Path) -> int:
    events_path = project_dir / ".forseti" / "events.jsonl"
    print(c(f"forseti demo renderer -- tailing {events_path}", DIM))
    state: dict[str, Any] = {}
    try:
        for ev in tail_events(events_path):
            line = render_event(ev, state)
            if line is not None:
                print(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return 0


def run_replay(trace_file: Path, speed: float | None) -> int:
    events = read_events_file(trace_file)
    pace = f"speed={speed}" if speed else "instant"
    banner = f"forseti demo renderer -- replaying {trace_file} "
    banner += f"({len(events)} events, {pace})"
    print(c(banner, DIM))
    state: dict[str, Any] = {}
    delays = compute_replay_delays(events, speed, _MAX_REPLAY_DELAY_S)
    try:
        for delay, ev in zip(delays, events, strict=True):
            if delay > 0:
                time.sleep(delay)
            line = render_event(ev, state)
            if line is not None:
                print(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="render.py",
        description=(
            "Render a Forseti demo project's .forseti/events.jsonl as a "
            "live sequence diagram."
        ),
    )
    p.add_argument(
        "project_dir",
        nargs="?",
        help="project directory containing .forseti/events.jsonl (live mode)",
    )
    p.add_argument(
        "--replay",
        metavar="TRACE_FILE",
        help="replay a saved events.jsonl file instead of tailing a live project",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=None,
        help=(
            "pace --replay using recorded timestamp deltas divided by SPEED "
            "(e.g. 1 = real time, 4 = 4x fast-forward, capped per-gap); "
            "omit for instant replay"
        ),
    )
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    global _COLOR_ENABLED
    _COLOR_ENABLED = (
        sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    )

    if args.replay:
        if args.project_dir:
            print(
                "render.py: pass either a project_dir or --replay, not both",
                file=sys.stderr,
            )
            return 2
        return run_replay(Path(args.replay), speed=args.speed)

    if args.speed is not None:
        print("render.py: --speed only applies to --replay", file=sys.stderr)
        return 2

    if not args.project_dir:
        print(
            "render.py: a project_dir is required in live mode (or use --replay)",
            file=sys.stderr,
        )
        return 2

    return run_live(Path(args.project_dir))


if __name__ == "__main__":
    sys.exit(main())
