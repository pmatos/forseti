"""The shared trace-writing interface, exercised with real temporary files."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forseti._events import events_path, log_event, project_events_path, record_event


def test_trace_destinations_keep_both_root_conventions(tmp_path: Path) -> None:
    assert events_path(tmp_path) == tmp_path / "events.jsonl"
    assert project_events_path(str(tmp_path)) == events_path(tmp_path / ".forseti")


@pytest.mark.parametrize(
    ("writer", "relative_path"),
    [
        (record_event, Path("events.jsonl")),
        (log_event, Path(".forseti/events.jsonl")),
    ],
)
def test_writers_append_complete_records_without_replacing_existing_content(
    tmp_path: Path, writer: Callable[..., None], relative_path: Path
) -> None:
    root = tmp_path / "nested" / "project"
    writer(root, "first", ts=1, note="café\nline")
    payload = "x" * 200_000
    writer(root, "second", ts=2, payload=payload)

    lines = (root / relative_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"ts": 1, "type": "first", "note": "café\nline"}
    assert json.loads(lines[1]) == {"ts": 2, "type": "second", "payload": payload}


@pytest.mark.parametrize("circular", [False, True])
def test_core_serialization_failure_leaves_previous_and_future_events_intact(
    tmp_path: Path, circular: bool
) -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    record_event(tmp_path, "first", ts=1)

    record_event(tmp_path, "invalid", bad=cycle if circular else object())
    record_event(tmp_path, "last", ts=2)

    assert [
        json.loads(line) for line in events_path(tmp_path).read_text().splitlines()
    ] == [
        {"ts": 1, "type": "first"},
        {"ts": 2, "type": "last"},
    ]


@pytest.mark.parametrize("circular", [False, True])
def test_adapter_serialization_failure_leaves_previous_and_future_events_intact(
    tmp_path: Path, circular: bool
) -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    log_event(tmp_path, "first", ts=1)

    with pytest.raises(ValueError if circular else TypeError):
        log_event(tmp_path, "invalid", bad=cycle if circular else object())
    log_event(tmp_path, "last", ts=2)

    path = project_events_path(tmp_path)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"ts": 1, "type": "first"},
        {"ts": 2, "type": "last"},
    ]
