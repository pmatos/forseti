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
    EsbmcResult,
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.orchestrator import LoopState, run_loop

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

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.calls += 1
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
    assert len(run.iterations) == 1
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
    assert fix.calls == 0


def test_error_gives_up_without_fixing() -> None:
    fix = FakeFix()
    verify = FakeVerify([Error(meta(), "esbmc binary not found")])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8)
    assert run.final_state is LoopState.GIVE_UP
    assert fix.calls == 0


def test_non_positive_budget_is_rejected() -> None:
    # A zero/negative budget can never run a pass — reject it loudly rather
    # than return an empty, misleading give-up.
    fix = FakeFix()
    verify = FakeVerify([Verified(meta())])
    with pytest.raises(ValueError):
        run_loop(SRC, verify=verify, fix=fix, unwind=8, max_iterations=0)
