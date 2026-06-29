"""Tests for the human-readable loop transcript (the demo summary)."""

from __future__ import annotations

from pathlib import Path

from forseti.esbmc import EsbmcResult, RunMeta, Unknown, UnknownReason, Verified, Violated
from forseti.orchestrator import run_loop, transcript_for

SRC = Path("kernel.c")


def meta(unwind: int = 8) -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "kernel.c", "--unwind", str(unwind), "--no-unwinding-assertions"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def violated() -> Violated:
    return Violated(meta(), "[Counterexample]\n")


class FakeVerify:
    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        return self._results.pop(0)


class FakeFix:
    def __call__(self, source: Path, violated: Violated) -> Path:
        return source


def test_transcript_for_abs_converge() -> None:
    # Acceptance: the abs run (VIOLATED -> fix -> VERIFIED) renders a readable
    # summary keyed by the unit, listing each iteration's verdict.
    verify = FakeVerify([violated(), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8)

    text = transcript_for(run, unit_id="examples/abs.c::my_abs")

    assert "examples/abs.c::my_abs" in text
    assert "Outcome: DONE" in text
    lines = text.splitlines()
    assert any("[0]" in ln and "VIOLATED" in ln for ln in lines)
    assert any("[1]" in ln and "VERIFIED" in ln for ln in lines)


def test_transcript_for_give_up_shows_reason() -> None:
    verify = FakeVerify([violated(), violated()])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, max_iterations=2)

    text = transcript_for(run, unit_id="kernel.c")

    assert "Outcome: GIVE_UP" in text
    assert "max_iterations_exceeded" in text


def test_transcript_for_unknown_shows_reason() -> None:
    verify = FakeVerify(
        [Unknown(meta(8), UnknownReason.TIMEOUT), Unknown(meta(16), UnknownReason.TIMEOUT)]
    )
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,))

    text = transcript_for(run, unit_id="kernel.c")

    assert "Outcome: UNKNOWN" in text
    assert "timeout" in text
