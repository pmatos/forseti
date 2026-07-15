"""The `run_loop` driver: bounded write -> verify -> fix over injected ports.

The driver is deterministic and effect-free in itself — all I/O lives behind
the `VerifyPort`/`FixPort`/`EventSink` seams. It records every pass in an
in-memory `LoopRun`, tagging a `GIVE_UP` with the `GiveUpReason` that caused it,
and emits a structured `Event` at each transition through the injected sink (the
default `NullSink` makes emission a no-op, so the driver stays effect-free unless
a sink is supplied). The human-facing report (last counterexample +
per-iteration history + reason) is a pure projection over that record —
`report_for` in `report.py`; the readable transcript is `transcript_for`. On
`Unknown` the driver escalates the unwind bound `k` along a bounded ladder,
re-verifying the same source until the verdict resolves or the ladder is
exhausted (then a terminal `UNKNOWN` — never a silent pass); an exhausted
iteration budget gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from forseti.esbmc import Error, EsbmcResult, Unknown, Verified, Violated

from .ladder import validated_ladder, verify_ladder
from .ports import FixPort, VerifyPort
from .state import GiveUpReason, LoopState, next_state
from .telemetry import EventEmitter, EventSink

DEFAULT_MAX_ITERATIONS = 10


@dataclass(frozen=True)
class Iteration:
    """One verify pass: the source verified, the verdict, and the state it mapped to.

    `source` is the path verified this pass — i.e. the output of the *previous*
    iteration's fix. With the minimal `FixPort` it is the only fix handle the
    loop has; a structured diff handle arrives with #28. `k` is the unwind bound
    this pass ran at — the loop's own escalation decision along the k-ladder,
    carried as data so the report never has to read it back from the esbmc argv.
    """

    index: int
    source: Path
    result: EsbmcResult
    state: LoopState
    k: int


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
    sink: EventSink | None = None,
) -> LoopRun:
    """Drive write -> verify -> fix until a terminal verdict or a budget runs out.

    Two bounds. The **outer** loop is the fix budget: up to `max_iterations`
    rounds, each verifying the current source and, on `Violated`, calling `fix`
    (on every round, incl. the last) before the next round; exhausting the budget
    ends in `GIVE_UP`. Each round delegates the k-escalation to the shared
    `verify_ladder`: on `Unknown` it re-verifies the *same* source at the
    next-higher unwind along `(unwind, *unwind_ladder)`, settling on the terminal
    `UNKNOWN` only once the ladder is exhausted (never a silent pass; roadmap
    Risk 1) — the exact rule `check_properties` runs, so the two drivers can't
    drift. Every rung becomes an `Iteration`; `k` restarts at the base bound for
    each fresh candidate. `next_state` is the single source of truth for the
    recorded state label.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    ladder = validated_ladder(unwind, unwind_ladder)
    emit = EventEmitter(sink).emit

    iterations: list[Iteration] = []
    current = source
    index = 0
    rounds = 0
    emit("trigger.fired", detail={"source": str(source), "base_k": unwind})
    while rounds < max_iterations:
        rounds += 1
        # The shared k-ladder owns the escalation walk (never a silent pass on
        # UNKNOWN; roadmap Risk 1) — the same rule `check_properties` runs. It
        # returns every rung's attempt in order; the driver turns each into an
        # `Iteration` and surfaces the escalations in its own event vocabulary.
        attempts = verify_ladder(current, verify=verify, ladder=ladder)
        for position, attempt in enumerate(attempts):
            state = next_state(attempt.result)
            iterations.append(
                Iteration(index, current, attempt.result, state, attempt.k)
            )
            emit(
                "verify.verdict",
                index=index,
                k=attempt.k,
                verdict=attempt.result.verdict.value,
            )
            if position + 1 < len(attempts):
                # every attempt before the last is a non-terminal Unknown that the
                # ladder escalated past — surface that decision (mirrors check.py).
                emit(
                    "unknown.policy.decision",
                    index=index,
                    detail={
                        "decision": "escalate",
                        "from_k": attempt.k,
                        "to_k": attempts[position + 1].k,
                    },
                )
            index += 1
        final = attempts[-1]
        state = next_state(final.result)
        match final.result:
            case Verified():
                emit("converged", index=index - 1)
                return LoopRun(state, tuple(iterations))
            case Violated() as violation:
                emit("fix.attempt", index=index - 1)
                current = fix(current, violation)  # fix every round, incl. the last
            case Unknown():
                emit(
                    "unknown.policy.decision",
                    index=index - 1,
                    detail={"decision": "exhausted"},
                )
                return LoopRun(state, tuple(iterations))  # ladder exhausted
            case Error():
                emit("give_up", index=index - 1, detail={"reason": "esbmc_error"})
                return LoopRun(
                    LoopState.GIVE_UP,
                    tuple(iterations),
                    give_up_reason=GiveUpReason.ESBMC_ERROR,
                )
            case _:
                assert_never(final.result)
    emit("give_up", detail={"reason": "max_iterations_exceeded"})
    return LoopRun(
        LoopState.GIVE_UP,
        tuple(iterations),
        give_up_reason=GiveUpReason.MAX_ITERATIONS_EXCEEDED,
    )
