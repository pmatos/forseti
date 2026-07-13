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

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never

from forseti.esbmc import Error, EsbmcResult, Unknown, Verified, Violated

from .ladder import validated_ladder, verify_ladder
from .ports import FixPort, VerifyPort
from .state import GiveUpReason, LoopState, next_state
from .telemetry import Event, EventSink, NullSink

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

    Two nested bounds. The **outer** loop is the fix budget: up to `max_iterations`
    rounds, each verifying the current source and, on `Violated`, calling `fix`
    (on every round, incl. the last) before the next round; exhausting the budget
    ends in `GIVE_UP`. The **inner** bound is the k-escalation ladder, delegated to
    the shared `verify_ladder`: on `Unknown` it re-verifies the *same* source at
    the next-higher unwind along `(unwind, *unwind_ladder)`, settling on the
    terminal `UNKNOWN` only once the ladder is exhausted (never a silent pass;
    roadmap Risk 1). `k` restarts at the base bound for each fresh candidate — a
    fresh `verify_ladder` call per round. `next_state` is the single source of
    truth for the recorded state label.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    ladder = validated_ladder(unwind, unwind_ladder)
    out = sink or NullSink()
    seq = itertools.count()

    def emit(type: str, **kw: Any) -> None:
        out.emit(Event(next(seq), type, **kw))

    iterations: list[Iteration] = []
    current = source
    index = 0
    rounds = 0
    emit("trigger.fired", detail={"source": str(source), "base_k": unwind})
    while rounds < max_iterations:
        rounds += 1
        # The k-escalation ladder is the shared policy in `verify_ladder`: it
        # verifies the same source rung-by-rung, escalating past each Unknown
        # until the verdict resolves or the ladder is exhausted. Record one
        # Iteration per rung and surface the escalation between consecutive rungs
        # (a non-terminal attempt is always an escalation — the last is terminal).
        attempts = verify_ladder(current, verify=verify, ladder=ladder)
        for at_index, attempt in enumerate(attempts):
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
            index += 1
            if at_index + 1 < len(attempts):
                emit(
                    "unknown.policy.decision",
                    index=index - 1,
                    detail={
                        "decision": "escalate",
                        "from_k": attempt.k,
                        "to_k": attempts[at_index + 1].k,
                    },
                )
        final = iterations[-1]  # verify_ladder returns >= 1 attempt
        match final.result:
            case Verified():
                emit("converged", index=final.index)
                return LoopRun(final.state, tuple(iterations))
            case Violated() as violation:
                emit("fix.attempt", index=final.index)
                current = fix(current, violation)  # fix every round, incl. the last
            case Unknown():
                emit(
                    "unknown.policy.decision",
                    index=final.index,
                    detail={"decision": "exhausted"},
                )
                return LoopRun(final.state, tuple(iterations))  # ladder exhausted
            case Error():
                emit("give_up", index=final.index, detail={"reason": "esbmc_error"})
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
