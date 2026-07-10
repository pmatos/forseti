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
from .render import EXIT_CODES, render_result, result_to_dict
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
from .runner import build_argv, classify, verify

__all__ = [
    "EXIT_CODES",
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
    "build_argv",
    "classify",
    "parse_counterexample",
    "render_result",
    "result_to_dict",
    "verify",
]
