"""The injected seams the loop driver depends on.

`VerifyPort` is the subset of `forseti.esbmc.verify`'s signature the driver
calls, so the real `verify` is structurally assignable to it. `FixPort` is the
minimal fix seam: given the current source and the violation, it returns the
path of the next source to verify — performing whatever edit/write it needs
*outside* the driver, so `run_loop` stays pure. The richer
FixRequest/FixProvider contract is #28.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from forseti.esbmc import EsbmcResult, Violated


class VerifyPort(Protocol):
    """Runs ESBMC on `source` at bound `unwind` and returns the verdict."""

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult: ...


class FixPort(Protocol):
    """Turns a violation into the next source to verify (effects are its own)."""

    def __call__(self, source: Path, violated: Violated) -> Path: ...
