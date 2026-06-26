"""Behavioural tests for the report-to-human projection `report_for`.

A stopped loop must hand a human enough context: the last counterexample, the
per-iteration history, and why it gave up. These tests drive runs with faked
ports (no esbmc binary, no disk, no network) and assert the `Report` projection
over the result.
"""

from __future__ import annotations

import json
from pathlib import Path

from forseti.esbmc import (
    Assignment,
    Counterexample,
    EsbmcResult,
    RunMeta,
    SourceLoc,
    Step,
    Unknown,
    UnknownReason,
    Verified,
    ViolatedProperty,
    Violated,
)
from forseti.orchestrator import (
    GiveUpReason,
    Iteration,
    LoopRun,
    LoopState,
    report_for,
    run_loop,
)

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
    return Violated(meta(), "[Counterexample]\nState 1 ...\n")


def counterexample() -> Counterexample:
    loc = SourceLoc(file="kernel.c", line=10, column=5, function="f")
    prop = ViolatedProperty(
        loc=loc,
        description="arithmetic overflow on add",
        expression="x + 1",
        cwe=("CWE-190",),
    )
    step = Step(
        number=1,
        loc=loc,
        assignments=(Assignment(lhs="x", value="2147483647", binary="0111"),),
    )
    return Counterexample(steps=(step,), violated_property=prop)


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts in order."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        return self._results.pop(0)


class FakeFix:
    """A FixPort that leaves the source untouched."""

    def __call__(self, source: Path, violated: Violated) -> Path:
        return source


def test_never_fixed_report_is_populated_at_cap() -> None:
    # Acceptance: a fix that never fixes terminates at the cap with a populated
    # report (no infinite loop).
    verify = FakeVerify([violated(), violated(), violated()])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, max_iterations=3)
    report = report_for(run)
    assert report.final_state is LoopState.GIVE_UP
    assert report.give_up_reason is GiveUpReason.MAX_ITERATIONS_EXCEEDED
    assert len(report.iterations) == 3
    assert report.last_counterexample_raw is not None


def test_typed_counterexample_flows_in_and_serializes() -> None:
    cex = counterexample()
    verify = FakeVerify([Violated(meta(), "[Counterexample]\n", cex), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8)
    report = report_for(run)
    assert report.last_counterexample is cex
    payload = report.to_dict()
    json.dumps(payload)  # must not raise — the serializability guard
    assert payload["final_state"] == "done"
    assert payload["last_counterexample"] is not None
    assert payload["iterations"][0]["verdict"] == "violated"


def test_converged_report_has_no_cex_or_reason() -> None:
    run = run_loop(SRC, verify=FakeVerify([Verified(meta())]), fix=FakeFix(), unwind=8)
    report = report_for(run)
    assert report.final_state is LoopState.DONE
    assert report.give_up_reason is None
    assert report.last_counterexample is None
    assert report.last_counterexample_raw is None


def test_k_extracted_from_argv() -> None:
    verify = FakeVerify([violated(), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8)
    report = report_for(run)
    assert [it.k for it in report.iterations] == [8, 8]


def _run_with_argv(argv: tuple[str, ...]) -> LoopRun:
    m = RunMeta(
        esbmc_version="8.3.0",
        argv=argv,
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )
    it = Iteration(index=0, source=SRC, result=Verified(m), state=LoopState.DONE)
    return LoopRun(final_state=LoopState.DONE, iterations=(it,))


def test_k_is_none_for_malformed_argv() -> None:
    # Missing --unwind, a trailing --unwind with no value, and a non-int value
    # must all yield k=None — never raise.
    for argv in (
        ("esbmc", "kernel.c", "--no-unwinding-assertions"),
        ("esbmc", "kernel.c", "--unwind"),
        ("esbmc", "kernel.c", "--unwind", "deep"),
    ):
        report = report_for(_run_with_argv(argv))
        assert report.iterations[0].k is None


def test_report_surfaces_unknown_reason() -> None:
    # The per-iteration history distinguishes UNKNOWN reasons (e.g. timeout) so a
    # human can see why the loop escalated; non-unknown passes carry None.
    verify = FakeVerify([Unknown(meta(), UnknownReason.TIMEOUT), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,))
    report = report_for(run)
    assert report.iterations[0].verdict == "unknown"
    assert report.iterations[0].unknown_reason == "timeout"
    assert report.iterations[1].unknown_reason is None
    json.dumps(report.to_dict())  # still serializable with the new field
    assert report.to_dict()["iterations"][0]["unknown_reason"] == "timeout"
