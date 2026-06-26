"""The `run_loop` driver: bounded write -> verify -> fix over injected ports.

The driver is deterministic and effect-free in itself — all I/O lives behind
the `VerifyPort`/`FixPort` seams. It records every pass in an in-memory
`LoopRun`, tagging a `GIVE_UP` with the `GiveUpReason` that caused it. The
human-facing report (last counterexample + per-iteration history + reason) is a
pure projection over that record — `report_for` in `report.py` — so the driver
stays effect-free. UNKNOWN escalation is #27; here UNKNOWN simply halts and an
exhausted iteration budget gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forseti.esbmc import EsbmcResult, Violated

from .ports import FixPort, VerifyPort
from .state import GiveUpReason, LoopState, next_state

DEFAULT_MAX_ITERATIONS = 10


@dataclass(frozen=True)
class Iteration:
    """One verify pass: the source verified, the verdict, and the state it mapped to.

    `source` is the path verified this pass — i.e. the output of the *previous*
    iteration's fix. With the minimal `FixPort` it is the only fix handle the
    loop has; a structured diff handle arrives with #28.
    """

    index: int
    source: Path
    result: EsbmcResult
    state: LoopState


@dataclass(frozen=True)
class LoopRun:
    """The outcome of a `run_loop` call: where it ended and how it got there.

    `give_up_reason` is set only when `final_state` is `GIVE_UP` (which path
    led there); `None` for `DONE`/`UNKNOWN`.
    """

    final_state: LoopState
    iterations: tuple[Iteration, ...]
    give_up_reason: GiveUpReason | None = None


def run_loop(
    source: Path,
    *,
    verify: VerifyPort,
    fix: FixPort,
    unwind: int,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopRun:
    """Drive write -> verify -> fix until a terminal verdict or the budget runs out.

    Control flow keys on the result *type* (so `Violated` narrows for the `fix`
    call); `next_state` is the single source of truth for the recorded state
    label. A `Violated` verdict triggers a fix and another pass; any other
    verdict (DONE/UNKNOWN/GIVE_UP) is terminal. Exhausting `max_iterations`
    while still `Violated` ends in GIVE_UP.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    iterations: list[Iteration] = []
    current = source
    for index in range(max_iterations):
        result = verify(current, unwind=unwind)
        state = next_state(result)
        iterations.append(Iteration(index, current, result, state))
        match result:
            case Violated() as violation:
                current = fix(current, violation)
            case _:
                # DONE/UNKNOWN have no give-up reason; the only way `state` is
                # GIVE_UP here is an `Error` verdict (Verified->DONE,
                # Unknown->UNKNOWN, Violated handled above).
                reason = (
                    GiveUpReason.ESBMC_ERROR
                    if state is LoopState.GIVE_UP
                    else None
                )
                return LoopRun(state, tuple(iterations), give_up_reason=reason)
    return LoopRun(
        LoopState.GIVE_UP,
        tuple(iterations),
        give_up_reason=GiveUpReason.MAX_ITERATIONS_EXCEEDED,
    )
