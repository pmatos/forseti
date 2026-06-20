"""Typed result of one ESBMC invocation.

ESBMC emits no proof object. For a unit + property it returns a *verdict*:
VERIFIED (no violation found up to bound k), VIOLATED (+ a concrete
counterexample), or UNKNOWN (timeout / bound too small). We add a fourth,
ERROR, for tooling/invocation failures (a bad binary, a parse error) so a
broken invocation can never masquerade as an inconclusive verdict.

`EsbmcResult` is the sealed union of those four outcomes; every variant carries
the `RunMeta` provenance of the run that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .counterexample import Counterexample


class Verdict(Enum):
    """The four outcomes of an ESBMC run."""

    VERIFIED = "verified"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    ERROR = "error"


class UnknownReason(Enum):
    """Why a run was inconclusive (a real verdict, just not VERIFIED/VIOLATED)."""

    TIMEOUT = "timeout"
    MEMOUT = "memout"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class RunMeta:
    """Provenance of one ESBMC invocation.

    `argv` records the exact command (including the bound `--unwind k`), and
    `esbmc_version` the engine build, so a VERIFIED stays honestly qualified as
    "verified up to k under this version" and can later key a result cache.
    """

    esbmc_version: str
    argv: tuple[str, ...]
    exit_code: int
    duration_s: float
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Verified:
    """No violation found up to the bound recorded in `meta.argv`."""

    meta: RunMeta

    @property
    def verdict(self) -> Verdict:
        return Verdict.VERIFIED


@dataclass(frozen=True)
class Violated:
    """A property was violated.

    `raw_counterexample` is ESBMC's trace text, kept as the lossless fallback.
    `counterexample` is the typed model parsed from it (`None` when parsing
    failed — a parse failure never downgrades the VIOLATED verdict).
    """

    meta: RunMeta
    raw_counterexample: str
    counterexample: Counterexample | None = None

    @property
    def verdict(self) -> Verdict:
        return Verdict.VIOLATED


@dataclass(frozen=True)
class Unknown:
    """Inconclusive within the bound (timeout, memory limit, solver limit)."""

    meta: RunMeta
    reason: UnknownReason

    @property
    def verdict(self) -> Verdict:
        return Verdict.UNKNOWN


@dataclass(frozen=True)
class Error:
    """A tooling/invocation failure — not a verdict about the code."""

    meta: RunMeta
    message: str

    @property
    def verdict(self) -> Verdict:
        return Verdict.ERROR


EsbmcResult = Verified | Violated | Unknown | Error
