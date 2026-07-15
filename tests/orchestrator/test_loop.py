"""Behavioural tests for the `run_loop` driver.

The verify/fix ports are faked — no esbmc binary, no disk, no network. A
`FakeVerify` replays a scripted list of verdicts in order; a `FakeFix` leaves
the source untouched and counts calls. This exercises the driver's control
flow in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forseti.esbmc import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.orchestrator import GiveUpReason, ListSink, LoopState, run_loop

if TYPE_CHECKING:
    from forseti.esbmc import verify
    from forseti.orchestrator import VerifyPort

    # The real `verify` must satisfy the narrow port (mypy-only guard; no
    # runtime effect). Fails type-checking if the Protocol variance ever breaks.
    _check: VerifyPort = verify


SRC = Path("kernel.c")


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "kernel.c", "--unwind", "8", "--no-unwinding-assertions"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def violated() -> Violated:
    return Violated(meta(), "[Counterexample]\n")


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts in order."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.calls = 0
        self.unwinds: list[int] = []

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.calls += 1
        self.unwinds.append(unwind)
        return self._results.pop(0)


class FakeFix:
    """A FixPort that leaves the source untouched and counts invocations."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, source: Path, violated: Violated) -> Path:
        self.calls += 1
        return source


def test_verified_first_pass_is_done() -> None:
    fix = FakeFix()
    run = run_loop(SRC, verify=FakeVerify([Verified(meta())]), fix=fix, unwind=8)
    assert run.final_state is LoopState.DONE
    assert run.give_up_reason is None
    assert len(run.iterations) == 1
    assert run.iterations[0].source == SRC
    assert fix.calls == 0


def test_violated_then_verified_converges() -> None:
    fix = FakeFix()
    verify = FakeVerify([violated(), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8)
    assert run.final_state is LoopState.DONE
    assert fix.calls == 1
    assert [it.state for it in run.iterations] == [LoopState.FIX, LoopState.DONE]


def test_never_fixed_gives_up_at_budget() -> None:
    fix = FakeFix()
    verify = FakeVerify([violated(), violated(), violated()])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8, max_iterations=3)
    assert run.final_state is LoopState.GIVE_UP
    assert run.give_up_reason is GiveUpReason.MAX_ITERATIONS_EXCEEDED
    assert len(run.iterations) == 3
    assert fix.calls == 3
    # the last recorded pass is still a FIX; GIVE_UP comes from exhaustion.
    assert run.iterations[-1].state is LoopState.FIX


def test_unknown_halts_without_fixing() -> None:
    # UNKNOWN must stop honestly — no fix, no silent pass (roadmap Risk 1).
    fix = FakeFix()
    verify = FakeVerify([Unknown(meta(), UnknownReason.TIMEOUT)])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8)
    assert run.final_state is LoopState.UNKNOWN
    assert run.give_up_reason is None
    assert fix.calls == 0


def test_error_gives_up_without_fixing() -> None:
    fix = FakeFix()
    verify = FakeVerify([Error(meta(), "esbmc binary not found")])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8)
    assert run.final_state is LoopState.GIVE_UP
    assert run.give_up_reason is GiveUpReason.ESBMC_ERROR
    assert fix.calls == 0


def test_non_positive_budget_is_rejected() -> None:
    # A zero/negative budget can never run a pass — reject it loudly rather
    # than return an empty, misleading give-up.
    fix = FakeFix()
    verify = FakeVerify([Verified(meta())])
    with pytest.raises(ValueError):
        run_loop(SRC, verify=verify, fix=fix, unwind=8, max_iterations=0)


def test_unknown_escalates_then_converges() -> None:
    # UNKNOWN at the base bound, then VERIFIED at the next rung up -> DONE.
    fix = FakeFix()
    verify = FakeVerify([Unknown(meta(), UnknownReason.TIMEOUT), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8, unwind_ladder=(16,))
    assert run.final_state is LoopState.DONE
    assert fix.calls == 0
    assert verify.unwinds == [8, 16]
    assert len(run.iterations) == 2


def test_unknown_exhausts_ladder_then_reports() -> None:
    # UNKNOWN at every rung -> terminal UNKNOWN once the ladder is exhausted;
    # never a silent pass, and no fix attempted.
    fix = FakeFix()
    verify = FakeVerify(
        [
            Unknown(meta(), UnknownReason.TIMEOUT),
            Unknown(meta(), UnknownReason.TIMEOUT),
            Unknown(meta(), UnknownReason.TIMEOUT),
        ]
    )
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8, unwind_ladder=(16, 32))
    assert run.final_state is LoopState.UNKNOWN
    assert run.give_up_reason is None
    assert fix.calls == 0
    assert verify.unwinds == [8, 16, 32]
    assert len(run.iterations) == 3


def test_ladder_restarts_at_base_after_a_fix() -> None:
    # A fresh candidate (post-fix) restarts the ladder at the base bound, then
    # escalates again on its own UNKNOWN.
    fix = FakeFix()
    verify = FakeVerify(
        [violated(), Unknown(meta(), UnknownReason.TIMEOUT), Verified(meta())]
    )
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8, unwind_ladder=(16,))
    assert run.final_state is LoopState.DONE
    assert fix.calls == 1
    assert verify.unwinds == [8, 8, 16]
    assert len(run.iterations) == 3


def test_escalation_through_fix_records_every_rung_and_event() -> None:
    # Contract for the shared k-escalation policy (`ladder.verify_ladder`): a
    # fresh candidate restarts at the base bound and escalates rung-by-rung on
    # each UNKNOWN. This pins the per-rung Iteration<->event correspondence
    # *across* a fix boundary in one scenario — the exact seam the driver routes
    # through `verify_ladder`, so a re-route can never drift the recorded rungs,
    # the escalation events, or their interleaving.
    sink = ListSink()
    verify = FakeVerify(
        [
            violated(),  # round 1 @ k=8  -> FIX
            Unknown(meta(), UnknownReason.TIMEOUT),  # round 2 @ k=8  -> escalate
            Unknown(meta(), UnknownReason.TIMEOUT),  # round 2 @ k=16 -> escalate
            Verified(meta()),  # round 2 @ k=32 -> DONE
        ]
    )
    run = run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16, 32), sink=sink
    )

    assert run.final_state is LoopState.DONE
    # Every rung is recorded: base-restart after the fix, then 8 -> 16 -> 32.
    assert verify.unwinds == [8, 8, 16, 32]
    assert [it.index for it in run.iterations] == [0, 1, 2, 3]
    assert [it.k for it in run.iterations] == [8, 8, 16, 32]
    assert [it.state for it in run.iterations] == [
        LoopState.FIX,
        LoopState.UNKNOWN,
        LoopState.UNKNOWN,
        LoopState.DONE,
    ]
    # The full interleaved event stream: a verify.verdict per rung, an
    # escalation decision *between* consecutive rungs, contiguous seq stamps.
    assert [e.type for e in sink.events] == [
        "trigger.fired",
        "verify.verdict",  # i0 k=8 violated
        "fix.attempt",  # i0
        "verify.verdict",  # i1 k=8 unknown
        "unknown.policy.decision",  # i1 escalate 8 -> 16
        "verify.verdict",  # i2 k=16 unknown
        "unknown.policy.decision",  # i2 escalate 16 -> 32
        "verify.verdict",  # i3 k=32 verified
        "converged",  # i3
    ]
    assert [e.seq for e in sink.events] == list(range(9))
    escalations = [e for e in sink.events if e.type == "unknown.policy.decision"]
    assert [e.detail for e in escalations] == [
        {"decision": "escalate", "from_k": 8, "to_k": 16},
        {"decision": "escalate", "from_k": 16, "to_k": 32},
    ]
    # both the Unknown pass and the escalated re-verify are the same iteration.
    assert [e.index for e in escalations] == [1, 2]


def test_invalid_unwind_ladder_is_rejected() -> None:
    # The effective ladder (unwind, *unwind_ladder) must be strictly increasing
    # positive ints: reject non-increasing, duplicate, and < 1 bounds loudly.
    fix = FakeFix()
    for bad_unwind, bad_ladder in [(8, (4,)), (8, (16, 16)), (0, (1,))]:
        with pytest.raises(ValueError):
            run_loop(
                SRC,
                verify=FakeVerify([Verified(meta())]),
                fix=fix,
                unwind=bad_unwind,
                unwind_ladder=bad_ladder,
            )
