"""Unit tests for the shared UNKNOWN k-ladder — no esbmc, no disk.

`validated_ladder` owns the ladder-shape rule (parity with `test_loop.py`'s
`test_invalid_unwind_ladder_is_rejected`); `verify_ladder` owns the escalation,
exercised with a scripted `FakeVerify`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
)
from forseti.orchestrator import LadderAttempt, validated_ladder, verify_ladder

SRC = Path("harness.c")


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "harness.c"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def unknown() -> Unknown:
    return Unknown(meta(), UnknownReason.TIMEOUT)


class FakeVerify:
    """A VerifyPort that replays a scripted list of verdicts, recording bounds."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.unwinds: list[int] = []

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.unwinds.append(unwind)
        return self._results.pop(0)


def test_validated_ladder_accepts_increasing() -> None:
    assert validated_ladder(8, ()) == (8,)
    assert validated_ladder(8, (16, 32)) == (8, 16, 32)


def test_validated_ladder_rejects_bad_shapes() -> None:
    # Non-increasing, duplicate, and < 1 bounds are all rejected loudly.
    for bad_unwind, bad_ladder in [(8, (4,)), (8, (16, 16)), (0, (1,))]:
        with pytest.raises(ValueError):
            validated_ladder(bad_unwind, bad_ladder)


def test_verify_ladder_single_terminal_verdict() -> None:
    # A non-Unknown at the base bound settles in one attempt (no escalation).
    verify = FakeVerify([Verified(meta())])
    attempts = list(verify_ladder(SRC, verify=verify, ladder=(8,)))
    assert len(attempts) == 1
    assert isinstance(attempts[-1].result, Verified)
    assert verify.unwinds == [8]


def test_verify_ladder_escalates_then_resolves() -> None:
    # UNKNOWN at 8, VERIFIED at 16 -> two attempts, ks [8, 16].
    verify = FakeVerify([unknown(), Verified(meta())])
    attempts = list(verify_ladder(SRC, verify=verify, ladder=(8, 16)))
    assert [a.k for a in attempts] == [8, 16]
    assert isinstance(attempts[0].result, Unknown)
    assert isinstance(attempts[-1].result, Verified)
    assert verify.unwinds == [8, 16]


def test_verify_ladder_exhausts_ladder_all_unknown() -> None:
    # UNKNOWN at every rung -> the last attempt is the terminal UNKNOWN, never a
    # silent pass; ks exhaust the whole ladder.
    verify = FakeVerify([unknown(), unknown(), unknown()])
    attempts = list(verify_ladder(SRC, verify=verify, ladder=(8, 16, 32)))
    assert [a.k for a in attempts] == [8, 16, 32]
    assert all(isinstance(a.result, Unknown) for a in attempts)
    assert isinstance(attempts[-1], LadderAttempt)


def test_verify_ladder_is_lazy_one_rung_at_a_time() -> None:
    # The ladder yields incrementally: pulling the first attempt must not have
    # run any later rung yet (the incremental contract behind issue #100).
    verify = FakeVerify([unknown(), unknown(), Verified(meta())])
    gen = verify_ladder(SRC, verify=verify, ladder=(8, 16, 32))

    first = next(gen)
    assert first.k == 8
    assert verify.unwinds == [8]  # rung 16 has NOT run yet

    second = next(gen)
    assert second.k == 16
    assert verify.unwinds == [8, 16]  # rung 32 still pending

    assert [a.k for a in gen] == [32]
    assert verify.unwinds == [8, 16, 32]
