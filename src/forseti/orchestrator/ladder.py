"""The UNKNOWN k-escalation ladder, shared by the loop and property-check drivers.

`UNKNOWN` is a distinct, honest halt — never a silent pass (CLAUDE.md, roadmap
Risk 1). On an inconclusive verdict a driver re-verifies the *same* source at the
next-higher unwind bound along a bounded ladder, settling on a terminal `UNKNOWN`
only once the ladder is exhausted. That policy is owned here — pure and
independently tested — so both drivers route through it (`run_loop` per fix
round, `check_properties` (#66) per property) instead of each keeping its own
copy of the escalation rule.

`verify_ladder` *yields* every attempt in order, one at a time as it is computed.
`climb_to_terminal` consumes it, observing each completed rung synchronously
before the next (possibly slow) verify runs. Callers own their telemetry and
history; the ladder owns observation timing and terminal selection.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
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
    """One rung of the ladder: the bound `k`, its verdict, and the escalation.

    `escalate_to` is the next bound the ladder will re-verify at *because* this
    rung's verdict was a non-terminal `Unknown`, or `None` when this attempt
    settles the ladder — a resolved (non-`Unknown`) verdict, or an `Unknown` at
    the final rung (exhausted, never a silent pass). It is the "will we escalate,
    and to what k" decision made data, so each driver reads it instead of
    re-deriving `ladder[position + 1]` for itself (the two can't drift).
    """

    k: int
    result: EsbmcResult
    escalate_to: int | None


def verify_ladder(
    source: Path, *, verify: VerifyPort, ladder: tuple[int, ...]
) -> Iterator[LadderAttempt]:
    """Verify `source` along `ladder`, escalating on `Unknown`.

    Verify at `ladder[0]`; on an `Unknown` verdict re-verify at the next rung,
    repeating until a non-`Unknown` verdict resolves it or the ladder is
    exhausted. Yields every attempt in order, one at a time *as it is computed*
    (at least one; the last is terminal), each carrying its own `escalate_to`
    decision: a caller emitting per attempt therefore flushes each completed rung
    — and its escalation — before the next verify is invoked, so a slow or
    interrupted later rung can't swallow an earlier verdict (issue #100). The
    escalation target is read from `ladder`, not by peeking at the next verify, so
    yielding stays lazy. When every rung is `Unknown` the final attempt is that
    terminal `Unknown` — the ladder is exhausted, never coerced to a pass.
    """
    for position, k in enumerate(ladder):
        result = verify(source, unwind=k)
        will_escalate = isinstance(result, Unknown) and position + 1 < len(ladder)
        escalate_to = ladder[position + 1] if will_escalate else None
        yield LadderAttempt(k, result, escalate_to)
        if not isinstance(result, Unknown):
            return


def climb_to_terminal(
    source: Path,
    *,
    verify: VerifyPort,
    ladder: tuple[int, ...],
    on_attempt: Callable[[LadderAttempt], None] | None = None,
) -> LadderAttempt:
    """Climb a validated `ladder`, observe each attempt, and return the terminal one.

    `on_attempt`, when supplied, runs synchronously for every completed rung,
    including the terminal one, before any next verification starts (issue #100).
    The caller owns history and telemetry; only the latest attempt is retained
    here. With no observer, intermediate attempts are discarded.

    Verification and observation exceptions propagate immediately, without
    running a later rung. An exhausted ladder returns its terminal `Unknown`,
    never a pass. Like `verify_ladder`, expects a nonempty validated ladder.
    """
    settled = None
    for attempt in verify_ladder(source, verify=verify, ladder=ladder):
        settled = attempt
        if on_attempt is not None:
            on_attempt(attempt)
    assert settled is not None  # verify_ladder yields at least one attempt
    return settled
