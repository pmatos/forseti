"""Characterization of `run_loop`'s k-escalation contract.

The escalation rule (re-verify the *same* source at the next-higher unwind on
`Unknown`, settle on terminal `UNKNOWN` once the ladder is exhausted) lives in
`ladder.verify_ladder`. These tests pin the exact behaviour `run_loop` must show
at its two seams — the recorded `iterations` and the emitted event stream —
independent of *how* the escalation is driven internally, so the driver can
route through the shared ladder helper without any observable change.

The verify/fix ports are faked (no esbmc, no disk). `meta(unwind)` writes the
real bound into `argv`; a `ListSink` captures the ordered event stream.
"""

from __future__ import annotations

from pathlib import Path

from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.orchestrator import ListSink, LoopState, run_loop

SRC = Path("kernel.c")


def meta(unwind: int = 8) -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=(
            "esbmc",
            "kernel.c",
            "--unwind",
            str(unwind),
            "--no-unwinding-assertions",
        ),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def unknown(unwind: int = 8) -> Unknown:
    return Unknown(meta(unwind), UnknownReason.TIMEOUT)


def violated(unwind: int = 8) -> Violated:
    return Violated(meta(unwind), "[Counterexample]\n")


class FakeVerify:
    """A VerifyPort replaying scripted verdicts, recording the bounds it saw."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.unwinds: list[int] = []

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        self.unwinds.append(unwind)
        return self._results.pop(0)


class FakeFix:
    """A FixPort that leaves the source untouched and counts invocations."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, source: Path, violated: Violated) -> Path:
        self.calls += 1
        return source


def test_escalate_then_converge_records_bound_per_rung() -> None:
    # Each intermediate Unknown rung is a recorded iteration carrying the exact
    # bound it ran at — the escalated re-verify is not folded into one pass.
    verify = FakeVerify([unknown(8), Verified(meta(16))])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,))

    assert run.final_state is LoopState.DONE
    assert [it.k for it in run.iterations] == [8, 16]
    assert [it.state for it in run.iterations] == [LoopState.UNKNOWN, LoopState.DONE]
    # every rung re-verifies the *same* source (escalation, not a fresh candidate)
    assert [it.source for it in run.iterations] == [SRC, SRC]


def test_escalate_then_converge_emits_interleaved_stream() -> None:
    # The full ordered event stream: one verify.verdict per rung, an escalate
    # decision *between* the Unknown rung and its re-verify, then converged.
    sink = ListSink()
    verify = FakeVerify([unknown(8), Verified(meta(16))])
    run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,), sink=sink
    )

    assert [(e.seq, e.type) for e in sink.events] == [
        (0, "trigger.fired"),
        (1, "verify.verdict"),
        (2, "unknown.policy.decision"),
        (3, "verify.verdict"),
        (4, "converged"),
    ]
    v0, decision, v1 = sink.events[1], sink.events[2], sink.events[3]
    assert (v0.index, v0.k, v0.verdict) == (0, 8, "unknown")
    assert (v1.index, v1.k, v1.verdict) == (1, 16, "verified")
    # the escalate decision names the Unknown rung it acted on and both bounds
    assert decision.index == 0
    assert decision.detail == {"decision": "escalate", "from_k": 8, "to_k": 16}


def test_exhausted_ladder_emits_a_decision_per_rung_then_exhausted() -> None:
    sink = ListSink()
    verify = FakeVerify([unknown(8), unknown(16), unknown(32)])
    run = run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16, 32), sink=sink
    )

    assert run.final_state is LoopState.UNKNOWN
    assert [it.k for it in run.iterations] == [8, 16, 32]
    decisions = [
        (e.index, e.detail) for e in sink.events if e.type == "unknown.policy.decision"
    ]
    assert decisions == [
        (0, {"decision": "escalate", "from_k": 8, "to_k": 16}),
        (1, {"decision": "escalate", "from_k": 16, "to_k": 32}),
        (2, {"decision": "exhausted"}),
    ]


def test_ladder_restarts_at_base_after_a_fix() -> None:
    # A post-fix candidate restarts the ladder at the base bound; the global
    # iteration index keeps counting across the fix boundary.
    fix = FakeFix()
    verify = FakeVerify([violated(8), unknown(8), Verified(meta(16))])
    run = run_loop(SRC, verify=verify, fix=fix, unwind=8, unwind_ladder=(16,))

    assert run.final_state is LoopState.DONE
    assert fix.calls == 1
    assert verify.unwinds == [8, 8, 16]
    assert [it.index for it in run.iterations] == [0, 1, 2]
    assert [it.k for it in run.iterations] == [8, 8, 16]
    assert [it.state for it in run.iterations] == [
        LoopState.FIX,
        LoopState.UNKNOWN,
        LoopState.DONE,
    ]
