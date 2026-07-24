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
from .units import ListUnitsError, Param, Unit, list_units, parse_units
from .verify_cli import add_verify_arguments, verify_kwargs

__all__ = [
    "EXIT_CODES",
    "Assignment",
    "Counterexample",
    "Error",
    "EsbmcResult",
    "Frontend",
    "ListUnitsError",
    "Param",
    "RunMeta",
    "SourceLoc",
    "Step",
    "Unit",
    "Unknown",
    "UnknownReason",
    "Verdict",
    "Verified",
    "Violated",
    "ViolatedProperty",
    "add_verify_arguments",
    "build_argv",
    "classify",
    "list_units",
    "parse_counterexample",
    "parse_units",
    "render_result",
    "result_to_dict",
    "verify",
    "verify_kwargs",
]
