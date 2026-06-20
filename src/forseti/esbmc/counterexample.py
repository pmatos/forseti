"""Typed model of an ESBMC counterexample.

ESBMC emits no proof object. When it returns the VIOLATED verdict it prints a
concrete *counterexample*: an ordered trace of program states (the path) ending
in the property that was violated. This module is the typed, faithful model of
that trace — the frontend-aware parsing of ESBMC's text into it lives in
`cex_parser`.

Fields are restricted to `str | int | None | tuple` so the whole structure
serializes cleanly (see `Counterexample.to_dict`) for the observability JSONL
trace and the result cache.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

_BASE_SYMBOL_RE = re.compile(r"^[^$\[.]+")


@dataclass(frozen=True)
class SourceLoc:
    """A source location as ESBMC prints it; `function` is absent on some states."""

    file: str
    line: int
    column: int
    function: str | None


@dataclass(frozen=True)
class Assignment:
    """One `lhs = value` binding in a trace step.

    `value` is ESBMC's raw right-hand side (e.g. `-2147483648`, `(signed int *)0`,
    `ARRAY_OF(0)`); `binary` is the trailing bit-string ESBMC shows for scalars,
    or `None` when none was printed.
    """

    lhs: str
    value: str
    binary: str | None


@dataclass(frozen=True)
class Step:
    """One ESBMC trace state: its source location and the assignments made there.

    `number` is the state number exactly as printed — ESBMC's slicer drops
    states, so these are not contiguous and must not be re-indexed.
    """

    number: int
    loc: SourceLoc
    assignments: tuple[Assignment, ...]


@dataclass(frozen=True)
class ViolatedProperty:
    """The property ESBMC found violated at the end of the path."""

    loc: SourceLoc
    description: str
    expression: str | None
    cwe: tuple[str, ...]


@dataclass(frozen=True)
class Counterexample:
    """A parsed ESBMC counterexample: the ordered path plus the violated property."""

    steps: tuple[Step, ...]
    violated_property: ViolatedProperty

    @property
    def inputs(self) -> tuple[Assignment, ...]:
        """The first assignment seen for each distinct base variable.

        A *heuristic* "concrete input seeds" view: ESBMC's trace doesn't mark
        which assignments are nondet inputs versus computed values, so this
        keeps the earliest write per base source symbol (SSA suffix `$...` and
        any `[index]`/`.field` accessor stripped). The faithful, complete path
        lives in `steps`.
        """
        seen: set[str] = set()
        out: list[Assignment] = []
        for step in self.steps:
            for assignment in step.assignments:
                match = _BASE_SYMBOL_RE.match(assignment.lhs)
                base = match.group(0) if match else assignment.lhs
                if base not in seen:
                    seen.add(base)
                    out.append(assignment)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict (for the observability trace / result cache).

        Every field is `str | int | None | tuple`, so `asdict` yields a structure
        `json.dumps` accepts directly. The `inputs` view is derived, not stored.
        """
        return asdict(self)
