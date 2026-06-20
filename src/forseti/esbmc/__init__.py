"""Thin typed wrapper around the ESBMC bounded model checker.

`verify` runs ESBMC on a source and returns one of the sealed `EsbmcResult`
variants: `Verified | Violated | Unknown | Error`. A `Violated` carries both the
raw counterexample text and a typed `Counterexample` parsed by a frontend-aware
parser (C first).
"""

from .counterexample import (
    Assignment,
    Counterexample,
    SourceLoc,
    Step,
    ViolatedProperty,
)
from .cex_parser import Frontend, parse_counterexample
from .result import (
    EsbmcResult,
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verdict,
    Verified,
    Violated,
)
from .runner import classify, verify

__all__ = [
    "verify",
    "classify",
    "EsbmcResult",
    "Verdict",
    "UnknownReason",
    "RunMeta",
    "Verified",
    "Violated",
    "Unknown",
    "Error",
    "Counterexample",
    "Step",
    "Assignment",
    "SourceLoc",
    "ViolatedProperty",
    "Frontend",
    "parse_counterexample",
]
