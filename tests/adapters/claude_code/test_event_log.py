"""Tests for the adapter's loop trace (`event_log`).

See `adapters/claude-code/tests/test_trace_to_mermaid.py` for the renderer
tests -- `trace_to_mermaid.py` is a standalone dev tool outside the packaged
adapter (RFC-0004), not moved here.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from forseti.adapters.claude_code import event_log


def test_log_event_appends_ordered_lines(tmp_path: Path) -> None:
    event_log.log_event(
        tmp_path, event_log.EDIT, tool="Write", file="a.c", functions=["f"]
    )
    event_log.log_event(
        tmp_path, event_log.VERIFY, unit="a.c::f", verdict="violated", k=1
    )

    path = event_log.events_path(tmp_path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["type"] == event_log.EDIT and first["file"] == "a.c"
    assert second["type"] == event_log.VERIFY and second["verdict"] == "violated"
    # every event carries a wall-clock ts, monotonic across the two appends
    assert first["ts"] <= second["ts"]


def test_read_events_skips_malformed_lines(tmp_path: Path) -> None:
    path = event_log.events_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"type": "edit", "ts": 1}\nnot json\n{"type": "stop", "ts": 2}\n')

    events = event_log.read_events(tmp_path)
    assert [e["type"] for e in events] == ["edit", "stop"]


def test_read_events_absent_is_empty(tmp_path: Path) -> None:
    assert event_log.read_events(tmp_path) == []


def test_write_text_atomic_round_trips_a_large_payload(tmp_path: Path) -> None:
    # Not a synthetic short-write simulation (os.fdopen's BufferedWriter goes
    # through io.FileIO, which retries at the C level below what Python's own
    # os.write() can be monkeypatched to intercept) -- this instead confirms
    # a payload well past a single small write lands byte-for-byte complete.
    target = tmp_path / "nested" / "state.json"
    payload = json.dumps({"data": "x" * 200_000})

    event_log.write_text_atomic(target, payload)

    assert target.read_text(encoding="utf-8") == payload
    assert not list(target.parent.glob("*.tmp"))  # no leftover temp file


def test_write_text_atomic_creates_a_fresh_file_at_0600(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    event_log.write_text_atomic(target, "{}")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_text_atomic_preserves_an_existing_files_mode(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")
    target.chmod(0o644)

    event_log.write_text_atomic(target, '{"updated": true}')

    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_text() == '{"updated": true}'


def test_log_event_never_raises_on_bad_target(tmp_path: Path) -> None:
    # a path whose parent is a file, not a dir — mkdir would fail; must be swallowed
    clash = tmp_path / "file"
    clash.write_text("x")
    event_log.log_event(clash / "nested", event_log.STOP, decision="allow")
