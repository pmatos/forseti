"""The `run_loop` driver: bounded write -> verify -> fix over injected ports.

The driver is deterministic and effect-free in itself — all I/O lives behind
the `VerifyPort`/`FixPort` seams. It records every pass in an in-memory
`LoopRun`, tagging a `GIVE_UP` with the `GiveUpReason` that caused it. The
human-facing report (last counterexample + per-iteration history + reason) is a
pure projection over that record — `report_for` in `report.py` — so the driver
stays effect-free. On `Unknown` the driver escalates the unwind bound `k` along
a bounded ladder, re-verifying the same source until the verdict resolves or the
ladder is exhausted (then a terminal `UNKNOWN` — never a silent pass); an
exhausted iteration budget gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from forseti.esbmc import EsbmcResult, Error, Unknown, Verified, Violated

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
    unwind_ladder: tuple[int, ...] = (),
) -> LoopRun:
    """Drive write -> verify -> fix until a terminal verdict or a budget runs out.

    Two nested bounds. The **outer** loop is the fix budget: up to `max_iterations`
    rounds, each verifying the current source and, on `Violated`, calling `fix`
    (on every round, incl. the last) before the next round; exhausting the budget
    ends in `GIVE_UP`. The **inner** loop is the k-escalation ladder: on `Unknown`
    it re-verifies the *same* source at the next-higher unwind along
    `(unwind, *unwind_ladder)`, settling on the terminal `UNKNOWN` only once the
    ladder is exhausted (never a silent pass; roadmap Risk 1). `k` restarts at the
    base bound for each fresh candidate. `next_state` is the single source of
    truth for the recorded state label.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    ladder = (unwind, *unwind_ladder)
    if any(k < 1 for k in ladder) or any(b <= a for a, b in zip(ladder, ladder[1:])):
        raise ValueError(
            f"unwind ladder must be increasing positive ints, got {ladder}"
        )
    iterations: list[Iteration] = []
    current = source
    index = 0
    rounds = 0
    while rounds < max_iterations:
        rounds += 1
        k_index = 0
        while True:
            result = verify(current, unwind=ladder[k_index])
            state = next_state(result)
            iterations.append(Iteration(index, current, result, state))
            index += 1
            if isinstance(result, Unknown) and k_index + 1 < len(ladder):
                k_index += 1  # escalate: re-verify the same source at higher k
                continue
            break
        match result:
            case Verified():
                return LoopRun(state, tuple(iterations))
            case Violated() as violation:
                current = fix(current, violation)  # fix every round, incl. the last
            case Unknown():
                return LoopRun(state, tuple(iterations))  # ladder exhausted
            case Error():
                return LoopRun(
                    LoopState.GIVE_UP,
                    tuple(iterations),
                    give_up_reason=GiveUpReason.ESBMC_ERROR,
                )
            case _:
                assert_never(result)
    return LoopRun(
        LoopState.GIVE_UP,
        tuple(iterations),
        give_up_reason=GiveUpReason.MAX_ITERATIONS_EXCEEDED,
    )
