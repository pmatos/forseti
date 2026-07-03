"""Thin typed wrapper around the ESBMC bounded model checker.

`verify` runs ESBMC on a source and returns one of the sealed `EsbmcResult`
variants: `Verified | Violated | Unknown | Error`. A `Violated` carries both the
raw counterexample text and a typed `Counterexample` parsed by a frontend-aware
parser (C first).
"""

from .cex_parser import Frontend, parse_counterexample
from .counterexample import (
    Assignment,
    Counterexample,
    SourceLoc,
    Step,
    ViolatedProperty,
)
from .result import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verdict,
    Verified,
    Violated,
)
from .runner import classify, verify

__all__ = [
    "Assignment",
    "Counterexample",
    "Error",
    "EsbmcResult",
    "Frontend",
    "RunMeta",
    "SourceLoc",
    "Step",
    "Unknown",
    "UnknownReason",
    "Verdict",
    "Verified",
    "Violated",
    "ViolatedProperty",
    "classify",
    "parse_counterexample",
    "verify",
]
