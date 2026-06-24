"""The `run_loop` driver: bounded write -> verify -> fix over injected ports.

The driver is deterministic and effect-free in itself — all I/O lives behind
the `VerifyPort`/`FixPort` seams. It records every pass in an in-memory
`LoopRun`. Termination *policy* (a rich give-up report, convergence over
multiple properties) is #26; UNKNOWN escalation is #27; here UNKNOWN simply
halts and an exhausted iteration budget gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forseti.esbmc import EsbmcResult, Violated

from .ports import FixPort, VerifyPort
from .state import LoopState, next_state

DEFAULT_MAX_ITERATIONS = 10


@dataclass(frozen=True)
class Iteration:
    """One verify pass: the verdict and the loop state it mapped to."""

    index: int
    result: EsbmcResult
    state: LoopState


@dataclass(frozen=True)
class LoopRun:
    """The outcome of a `run_loop` call: where it ended and how it got there."""

    final_state: LoopState
    iterations: tuple[Iteration, ...]


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
        iterations.append(Iteration(index, result, state))
        match result:
            case Violated() as violation:
                current = fix(current, violation)
            case _:
                return LoopRun(state, tuple(iterations))
    return LoopRun(LoopState.GIVE_UP, tuple(iterations))
