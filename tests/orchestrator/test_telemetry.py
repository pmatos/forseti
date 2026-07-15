"""Behavioural tests for loop telemetry: the injected `EventSink` seam.

The verify/fix ports are faked (no esbmc, no disk). A `ListSink` captures the
events `run_loop` emits at each transition so we can assert the sequence. A
local `meta(unwind)` builder writes the real bound into `argv` so the
report-derived `k` matches the event `k`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from forseti.esbmc import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.orchestrator import (
    EventEmitter,
    JsonlSink,
    ListSink,
    NullSink,
    run_loop,
)


def unknown(unwind: int = 8) -> Unknown:
    return Unknown(meta(unwind), UnknownReason.TIMEOUT)


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


def violated(unwind: int = 8) -> Violated:
    return Violated(meta(unwind), "[Counterexample]\n")


class FakeVerify:
    """A VerifyPort replaying scripted verdicts in order."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        return self._results.pop(0)


class FakeFix:
    """A FixPort that leaves the source untouched."""

    def __call__(self, source: Path, violated: Violated) -> Path:
        return source


def test_run_loop_emits_trigger_fired_first() -> None:
    sink = ListSink()
    run_loop(
        SRC, verify=FakeVerify([Verified(meta())]), fix=FakeFix(), unwind=8, sink=sink
    )

    assert sink.events, "expected at least one emitted event"
    first = sink.events[0]
    assert first.type == "trigger.fired"
    assert first.seq == 0


def test_converge_emits_expected_event_sequence() -> None:
    sink = ListSink()
    verify = FakeVerify([violated(), Verified(meta())])
    run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, sink=sink)

    assert [e.type for e in sink.events] == [
        "trigger.fired",
        "verify.verdict",
        "fix.attempt",
        "verify.verdict",
        "converged",
    ]
    # seq is a contiguous 0..n stamp.
    assert [e.seq for e in sink.events] == [0, 1, 2, 3, 4]
    # the two verify.verdict events carry the recorded iteration index/k/verdict.
    v0, v1 = sink.events[1], sink.events[3]
    assert (v0.index, v0.k, v0.verdict) == (0, 8, "violated")
    assert (v1.index, v1.k, v1.verdict) == (1, 8, "verified")
    # fix.attempt and converged name the iteration they acted on.
    assert sink.events[2].index == 0
    assert sink.events[4].index == 1


def test_unknown_escalation_emits_policy_decision() -> None:
    sink = ListSink()
    verify = FakeVerify([unknown(8), Verified(meta(16))])
    run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,), sink=sink
    )

    decisions = [e for e in sink.events if e.type == "unknown.policy.decision"]
    assert len(decisions) == 1
    d = decisions[0]
    assert d.detail == {"decision": "escalate", "from_k": 8, "to_k": 16}
    # both the Unknown pass and the escalated re-verify are recorded iteration 0.
    assert d.index == 0


def test_escalation_after_fix_emits_full_event_stream() -> None:
    # A fix, then an escalate-to-converge on the fresh candidate: pins the whole
    # cross-round event stream (types, seqs, indices, ks) so the ladder-unifying
    # refactor cannot drift the emitted telemetry. Verdicts: violated@8 -> fix ->
    # unknown@8 -> escalate -> verified@16.
    sink = ListSink()
    verify = FakeVerify([violated(8), unknown(8), Verified(meta(16))])
    run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16,), sink=sink
    )

    assert [e.type for e in sink.events] == [
        "trigger.fired",
        "verify.verdict",
        "fix.attempt",
        "verify.verdict",
        "unknown.policy.decision",
        "verify.verdict",
        "converged",
    ]
    assert [e.seq for e in sink.events] == [0, 1, 2, 3, 4, 5, 6]

    verdicts = [e for e in sink.events if e.type == "verify.verdict"]
    assert [(e.index, e.k, e.verdict) for e in verdicts] == [
        (0, 8, "violated"),
        (1, 8, "unknown"),
        (2, 16, "verified"),
    ]
    fix_attempt = sink.events[2]
    assert fix_attempt.index == 0
    escalation = sink.events[4]
    assert escalation.index == 1
    assert escalation.detail == {"decision": "escalate", "from_k": 8, "to_k": 16}
    assert sink.events[6].index == 2  # converged names the settling iteration


def test_terminal_unknown_emits_exhausted() -> None:
    sink = ListSink()
    verify = FakeVerify([unknown(8), unknown(16), unknown(32)])
    run_loop(
        SRC, verify=verify, fix=FakeFix(), unwind=8, unwind_ladder=(16, 32), sink=sink
    )

    decisions = [
        e.detail.get("decision")
        for e in sink.events
        if e.type == "unknown.policy.decision"
    ]
    assert decisions == ["escalate", "escalate", "exhausted"]


def test_give_up_emits_reason_for_error() -> None:
    sink = ListSink()
    run_loop(
        SRC,
        verify=FakeVerify([Error(meta(), "boom")]),
        fix=FakeFix(),
        unwind=8,
        sink=sink,
    )

    give_ups = [e for e in sink.events if e.type == "give_up"]
    assert len(give_ups) == 1
    assert give_ups[0].detail == {"reason": "esbmc_error"}


def test_give_up_emits_reason_for_max_iterations() -> None:
    sink = ListSink()
    verify = FakeVerify([violated(), violated()])
    run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, max_iterations=2, sink=sink)

    give_ups = [e for e in sink.events if e.type == "give_up"]
    assert len(give_ups) == 1
    assert give_ups[0].detail == {"reason": "max_iterations_exceeded"}
    # the budget-exhaustion give-up is not tied to a single iteration.
    assert give_ups[0].index is None


def test_null_sink_is_default_and_behavior_unchanged() -> None:
    # A sink only observes — the LoopRun must be identical with no sink, an
    # explicit NullSink, and a ListSink.
    def fresh_verify() -> FakeVerify:
        return FakeVerify([violated(), Verified(meta())])

    run_default = run_loop(SRC, verify=fresh_verify(), fix=FakeFix(), unwind=8)
    run_null = run_loop(
        SRC, verify=fresh_verify(), fix=FakeFix(), unwind=8, sink=NullSink()
    )
    run_list = run_loop(
        SRC, verify=fresh_verify(), fix=FakeFix(), unwind=8, sink=ListSink()
    )

    for other in (run_null, run_list):
        assert other.final_state is run_default.final_state
        assert other.give_up_reason is run_default.give_up_reason
        assert len(other.iterations) == len(run_default.iterations)


def test_event_emitter_stamps_monotonic_seq_from_zero() -> None:
    # Each emit stamps the next seq, starting at 0 — the monotonic order guarantee
    # both drivers rely on, owned in one place instead of a per-driver closure.
    sink = ListSink()
    emitter = EventEmitter(sink)
    emitter.emit("a")
    emitter.emit("b")
    emitter.emit("c")

    assert [e.seq for e in sink.events] == [0, 1, 2]
    assert [e.type for e in sink.events] == ["a", "b", "c"]


def test_event_emitter_forwards_type_and_fields() -> None:
    # The type tag and every keyword field land on the Event handed to the sink.
    sink = ListSink()
    EventEmitter(sink).emit("verify.verdict", index=2, k=16, verdict="verified")

    (event,) = sink.events
    assert event.type == "verify.verdict"
    assert (event.index, event.k, event.verdict) == (2, 16, "verified")


def test_event_emitter_defaults_to_null_sink() -> None:
    # A None sink defaults to NullSink, so an emitter with no sink is effect-free
    # (mirrors the driver default) and never raises.
    emitter = EventEmitter()
    emitter.emit("trigger.fired", detail={"source": "kernel.c"})
    emitter.emit("converged", index=0)  # does not raise; nothing observed


def test_jsonl_sink_writes_parseable_lines() -> None:
    buf = io.StringIO()
    verify = FakeVerify([violated(), Verified(meta())])
    run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, sink=JsonlSink(buf))

    lines = buf.getvalue().splitlines()
    assert lines, "expected JSONL output"
    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == [
        "trigger.fired",
        "verify.verdict",
        "fix.attempt",
        "verify.verdict",
        "converged",
    ]
    assert [r["seq"] for r in records] == [0, 1, 2, 3, 4]
