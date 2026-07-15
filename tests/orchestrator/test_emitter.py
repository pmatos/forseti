"""Tests for `sequential_emitter`: the shared emit-closure both drivers use.

`run_loop` and `check_properties` each need an `emit(type, **fields)` closure
that defaults a missing sink to `NullSink`, stamps a monotonic `seq`, and builds
the `Event`. That preamble was copy-pasted in both drivers; `sequential_emitter`
is the single home for it. These tests pin its behaviour at the public seam.
"""

from __future__ import annotations

from forseti.orchestrator import ListSink, NullSink, sequential_emitter


def test_stamps_contiguous_monotonic_seq() -> None:
    sink = ListSink()
    emit = sequential_emitter(sink)

    emit("a")
    emit("b")
    emit("c")

    assert [e.seq for e in sink.events] == [0, 1, 2]
    assert [e.type for e in sink.events] == ["a", "b", "c"]


def test_passes_fields_through_to_event() -> None:
    sink = ListSink()
    emit = sequential_emitter(sink)

    emit("verify.verdict", index=3, k=16, verdict="unknown", detail={"why": "x"})

    (event,) = sink.events
    assert event.index == 3
    assert event.k == 16
    assert event.verdict == "unknown"
    assert event.detail == {"why": "x"}


def test_none_sink_defaults_to_null_and_is_a_noop() -> None:
    # A missing sink must not raise and must observe nothing (parity with the
    # NullSink default the drivers rely on to stay effect-free).
    emit = sequential_emitter(None)
    emit("trigger.fired")  # must not raise


def test_explicit_null_sink_is_a_noop() -> None:
    emit = sequential_emitter(NullSink())
    emit("trigger.fired")  # must not raise


def test_each_emitter_owns_an_independent_counter() -> None:
    # Two calls produce two closures with separate seq counters, so one driver's
    # events never renumber another's.
    first = ListSink()
    second = ListSink()
    emit_first = sequential_emitter(first)
    emit_second = sequential_emitter(second)

    emit_first("a")
    emit_second("b")
    emit_first("c")

    assert [e.seq for e in first.events] == [0, 1]
    assert [e.seq for e in second.events] == [0]
