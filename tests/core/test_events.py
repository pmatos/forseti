"""Tests for Core's canonical lifecycle-event primitive (`core/events.py`, #213)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from forseti.adapters.claude_code import event_log
from forseti.core.events import (
    GATE_DECISION,
    PROPERTY_CHECK_START,
    PROPERTY_PROPOSED,
    PROPERTY_VERDICT,
    events_path,
    record_event,
)


def test_record_event_appends_one_json_line(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    record_event(root, PROPERTY_PROPOSED, unit_id="a.c::f", property_id="p1")
    record_event(
        root, PROPERTY_VERDICT, unit_id="a.c::f", property_id="p1", outcome="held"
    )

    lines = events_path(root).read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == [PROPERTY_PROPOSED, PROPERTY_VERDICT]
    assert all("ts" in e for e in events)
    assert events[0]["unit_id"] == "a.c::f"
    assert events[1]["outcome"] == "held"


def test_record_event_creates_store_root(tmp_path: Path) -> None:
    root = tmp_path / "nested" / ".forseti"
    assert not root.exists()
    record_event(root, GATE_DECISION, decision="pass")
    assert root.exists()
    assert events_path(root).exists()


def test_record_event_swallows_write_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A trace failure must never raise into the caller (module docstring): a
    # store_root that is actually a *file* makes `mkdir(parents=True, ...)`
    # raise `NotADirectoryError` (an OSError subclass) when it tries to create
    # a child under it.
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("")
    # no raise:
    record_event(blocked / ".forseti", PROPERTY_CHECK_START, unit_id="a.c::f")


def test_record_event_swallows_non_serializable_fields(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    record_event(root, PROPERTY_PROPOSED, unit_id="a.c::f", bad=object())  # no raise
    # Nothing usable was written -- the whole line is best-effort, not partial.
    assert not events_path(root).exists() or events_path(root).read_text() == ""


def test_core_and_adapter_append_to_one_trace(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    record_event(root, PROPERTY_CHECK_START, unit_id="a.c::f", ts=1)
    event_log.log_event(tmp_path, event_log.VERIFY, verdict="verified", ts=2)
    record_event(root, PROPERTY_VERDICT, outcome="held", ts=3)

    assert events_path(root) == event_log.events_path(tmp_path)
    assert event_log.read_events(tmp_path) == [
        {"ts": 1, "type": PROPERTY_CHECK_START, "unit_id": "a.c::f"},
        {"ts": 2, "type": event_log.VERIFY, "verdict": "verified"},
        {"ts": 3, "type": PROPERTY_VERDICT, "outcome": "held"},
    ]


def test_core_event_preserves_field_overrides_and_json_format(tmp_path: Path) -> None:
    record_event(tmp_path, "original", ts=7, type="custom", note="café\nnext")

    assert events_path(tmp_path).read_text(encoding="utf-8") == (
        '{"note": "caf\\u00e9\\nnext", "ts": 7, "type": "custom"}\n'
    )


def test_core_event_swallows_circular_fields_after_opening(tmp_path: Path) -> None:
    circular: list[object] = []
    circular.append(circular)

    record_event(tmp_path, PROPERTY_PROPOSED, bad=circular)

    assert events_path(tmp_path).read_text(encoding="utf-8") == ""


def test_core_event_does_not_suppress_clock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("clock unavailable")

    def broken_clock() -> float:
        raise failure

    monkeypatch.setattr(time, "time", broken_clock)
    with pytest.raises(OSError) as caught:
        record_event(tmp_path, PROPERTY_CHECK_START)
    assert caught.value is failure
    assert not events_path(tmp_path).exists()
