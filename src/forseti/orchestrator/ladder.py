"""The UNKNOWN k-escalation ladder, shared by the loop and property-check drivers.

`UNKNOWN` is a distinct, honest halt — never a silent pass (CLAUDE.md, roadmap
Risk 1). On an inconclusive verdict a driver re-verifies the *same* source at the
next-higher unwind bound along a bounded ladder, settling on a terminal `UNKNOWN`
only once the ladder is exhausted. That policy was inline in `run_loop`; it is
extracted here — pure and independently tested — so `check_properties` (#66)
reuses the exact same rule instead of a second copy.

Pure with respect to emission: `verify_ladder` returns every attempt in order so
the caller owns telemetry (each driver emits its own event vocabulary).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

from forseti.esbmc import EsbmcResult, Unknown

from .ports import VerifyPort


def validated_ladder(unwind: int, unwind_ladder: tuple[int, ...]) -> tuple[int, ...]:
    """`(unwind, *unwind_ladder)` after asserting strictly-increasing positive ints.

    The single source of truth for the ladder-shape rule (previously inline in
    `run_loop`): every rung must be >= 1 and each strictly greater than the last,
    so escalation always raises the bound. Raises `ValueError` otherwise.
    """
    ladder = (unwind, *unwind_ladder)
    if any(k < 1 for k in ladder) or any(b <= a for a, b in itertools.pairwise(ladder)):
        raise ValueError(
            f"unwind ladder must be increasing positive ints, got {ladder}"
        )
    return ladder


@dataclass(frozen=True)
class LadderAttempt:
    """One rung of the ladder: the bound `k` verified at, and the verdict there."""

    k: int
    result: EsbmcResult


def verify_ladder(
    source: Path, *, verify: VerifyPort, ladder: tuple[int, ...]
) -> tuple[LadderAttempt, ...]:
    """Verify `source` along `ladder`, escalating on `Unknown`.

    Verify at `ladder[0]`; on an `Unknown` verdict re-verify at the next rung,
    repeating until a non-`Unknown` verdict resolves it or the ladder is
    exhausted. Returns every attempt in order (`len >= 1`; the last is terminal).
    When every rung is `Unknown` the last attempt is that terminal `Unknown` —
    the ladder is exhausted, never coerced to a pass. Pure w.r.t. emission: the
    caller inspects the returned attempts to surface each escalation.
    """
    attempts: list[LadderAttempt] = []
    for k in ladder:
        result = verify(source, unwind=k)
        attempts.append(LadderAttempt(k, result))
        if not isinstance(result, Unknown):
            break
    return tuple(attempts)
