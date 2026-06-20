"""Frontend-aware parser: ESBMC counterexample text -> typed `Counterexample`.

Only the C frontend is implemented (ADR-0003: C first, then C++, then Python);
`Frontend` is the extension point so later frontends slot in without touching
callers. `parse_counterexample` never raises and returns `None` on any failure,
so attaching a parsed model can never downgrade or break the VIOLATED verdict.
"""

from __future__ import annotations

import re
from enum import Enum

from .counterexample import (
    Assignment,
    Counterexample,
    SourceLoc,
    Step,
    ViolatedProperty,
)


class Frontend(Enum):
    """ESBMC source frontend a counterexample came from (C only, for now)."""

    C = "c"


_STATE_RE = re.compile(
    r"^State (\d+) file (.+?) line (\d+) column (\d+)"
    r"(?: function (.+?))? thread (\d+)$"
)
_LOC_RE = re.compile(
    r"^\s*file (.+?) line (\d+) column (\d+)(?: function (.+?))?$"
)
_VIOLATED = "Violated property:"
_CEX_MARKER = "[Counterexample]"
_BINARY_RE = re.compile(r"\s+\(([01]+(?: [01]+)*)\)$")


def _loc(file: str, line: str, column: str, function: str | None) -> SourceLoc:
    return SourceLoc(
        file=file, line=int(line), column=int(column), function=function or None
    )


def _assignment(body: str) -> Assignment:
    """Parse one `lhs = value` line, peeling ESBMC's trailing bit-string if shown."""
    lhs, _, rhs = body.partition(" = ")
    rhs = rhs.strip()
    binary: str | None = None
    match = _BINARY_RE.search(rhs)
    if match:
        binary = match.group(1)
        rhs = rhs[: match.start()].rstrip()
    return Assignment(lhs=lhs.strip(), value=rhs, binary=binary)


def _opens_block(line: str) -> bool:
    """A `State` header or the `Violated property:` line ends the current block."""
    return bool(_STATE_RE.match(line.strip())) or line.startswith(_VIOLATED)


def _parse_state(lines: list[str], i: int) -> tuple[Step, int]:
    """Parse a `State` block starting at line `i`; return the Step and next index."""
    number, file, ln, col, fn, _thread = _STATE_RE.match(lines[i].strip()).groups()  # type: ignore[union-attr]
    assignments: list[Assignment] = []
    i += 1
    while i < len(lines) and not _opens_block(lines[i]):
        body = lines[i].strip()
        if body and set(body) != {"-"} and " = " in body:
            assignments.append(_assignment(body))
        i += 1
    step = Step(int(number), _loc(file, ln, col, fn), tuple(assignments))
    return step, i


def _parse_violated(lines: list[str], i: int) -> tuple[ViolatedProperty | None, int]:
    """Parse the `Violated property:` block; return the property (or None) and next index.

    Layout: a `file … line … column … function …` location line, then a human
    description, optional `CWE: <id>[, <id>…]` line(s), and an optional trailing
    violated expression (absent for e.g. a NULL-pointer dereference).
    """
    i += 1
    loc_match = _LOC_RE.match(lines[i]) if i < len(lines) else None
    if loc_match is None:
        return None, i
    file, ln, col, fn = loc_match.groups()
    i += 1
    cwe: tuple[str, ...] = ()
    detail: list[str] = []
    while i < len(lines) and lines[i][:1] in (" ", "\t"):
        text = lines[i].strip()
        if text.startswith("CWE:"):
            cwe = tuple(p.strip() for p in text[len("CWE:") :].split(",") if p.strip())
        elif text:
            detail.append(text)
        i += 1
    prop = ViolatedProperty(
        loc=_loc(file, ln, col, fn),
        description=detail[0] if detail else "",
        expression=detail[1] if len(detail) > 1 else None,
        cwe=cwe,
    )
    return prop, i


def _parse_c(raw: str) -> Counterexample | None:
    lines = raw.splitlines()
    # Some flag combinations (e.g. --assertion-coverage) emit several
    # [Counterexample] blocks, each for a different violated property, before one
    # terminal banner. Merging their states under a single property would be a
    # synthetic trace mixing assertions — decline rather than mislead (the caller
    # keeps the lossless raw_counterexample).
    if sum(1 for line in lines if line.strip() == _CEX_MARKER) > 1:
        return None
    steps: list[Step] = []
    prop: ViolatedProperty | None = None
    i = 0
    while i < len(lines):
        if _STATE_RE.match(lines[i].strip()):
            step, i = _parse_state(lines, i)
            steps.append(step)
        elif lines[i].startswith(_VIOLATED):
            prop, i = _parse_violated(lines, i)
        else:
            i += 1

    if prop is None:
        return None
    return Counterexample(steps=tuple(steps), violated_property=prop)


def parse_counterexample(
    raw: str, frontend: Frontend = Frontend.C
) -> Counterexample | None:
    """Parse ESBMC counterexample text into a typed model, or `None` on failure.

    Returns `None` (never raises) when the text can't be parsed, so the caller's
    VIOLATED verdict and `raw_counterexample` fallback are always preserved.
    """
    try:
        if frontend is Frontend.C:
            return _parse_c(raw)
    except Exception:
        return None
    return None
