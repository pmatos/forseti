"""The UNKNOWN k-escalation ladder, shared by the loop and property-check drivers.

`UNKNOWN` is a distinct, honest halt — never a silent pass (CLAUDE.md, roadmap
Risk 1). On an inconclusive verdict a driver re-verifies the *same* source at the
next-higher unwind bound along a bounded ladder, settling on a terminal `UNKNOWN`
only once the ladder is exhausted. That policy is owned here — pure and
independently tested — so both drivers route through it (`run_loop` per fix
round, `check_properties` (#66) per property) instead of each keeping its own
copy of the escalation rule.

Pure with respect to emission: `verify_ladder` *yields* every attempt in order,
one at a time as it is computed, so the caller owns telemetry (each driver emits
its own event vocabulary) and can surface each completed rung — and its
escalation decision — before the next (possibly slow) verify runs.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
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
) -> Iterator[LadderAttempt]:
    """Verify `source` along `ladder`, escalating on `Unknown`.

    Verify at `ladder[0]`; on an `Unknown` verdict re-verify at the next rung,
    repeating until a non-`Unknown` verdict resolves it or the ladder is
    exhausted. Yields every attempt in order, one at a time *as it is computed*
    (at least one; the last is terminal): a caller emitting per attempt therefore
    flushes each completed rung — and, since it holds the same `ladder`, that
    rung's escalation decision — before the next verify is invoked, so a slow or
    interrupted later rung can't swallow an earlier verdict (issue #100). When
    every rung is `Unknown` the final attempt is that terminal `Unknown` — the
    ladder is exhausted, never coerced to a pass.
    """
    for k in ladder:
        result = verify(source, unwind=k)
        yield LadderAttempt(k, result)
        if not isinstance(result, Unknown):
            return
