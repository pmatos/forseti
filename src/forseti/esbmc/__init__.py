"""Thin typed wrapper around the ESBMC bounded model checker.

`verify` runs ESBMC on a C source and returns one of the sealed `EsbmcResult`
variants: `Verified | Violated | Unknown | Error`.
"""

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
]
