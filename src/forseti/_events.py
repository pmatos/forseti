"""Shared JSONL trace writing for Core and harness-local events.

The two entry points retain their existing destination and failure contracts:
Core takes a store root and suppresses I/O and serialization errors; local
logging takes a project directory and suppresses only I/O errors. Event
vocabularies and reading stay with their callers. This module depends on
neither Core nor a harness adapter.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_EVENTS_FILE = "events.jsonl"


def events_path(store_root: Path) -> Path:
    """The trace file for a store root: ``<store_root>/events.jsonl``."""
    return store_root / _EVENTS_FILE


def project_events_path(project_dir: str | os.PathLike[str]) -> Path:
    """The trace file for a project: ``<project_dir>/.forseti/events.jsonl``."""
    return events_path(Path(project_dir) / ".forseti")


def _append(path: Path, event: dict[str, Any]) -> None:
    """Append one sorted JSON line, leaving failure policy to the entry point."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def record_event(store_root: Path, event_type: str, **fields: Any) -> None:
    """Append a canonical event; suppress I/O and JSON serialization errors.

    Stamp the wall-clock ``ts`` and ``type`` before destination resolution.
    Caller fields retain precedence over generated fields. Serialization takes
    place after opening the file, so a failed first event can leave it empty.
    """
    event = {"ts": time.time(), "type": event_type, **fields}
    try:
        path = events_path(store_root)
        _append(path, event)
    except (OSError, TypeError, ValueError):
        pass


def log_event(project_dir: str | os.PathLike[str], type: str, **fields: Any) -> None:
    """Append a local event; suppress I/O errors, propagate serialization errors.

    Stamp before destination resolution, as in ``record_event``. Keeping the
    existing ``type`` parameter also preserves which field overrides a caller
    can supply. No locking, retries, or flush policy is added to either writer.
    """
    event = {"ts": time.time(), "type": type, **fields}
    try:
        path = project_events_path(project_dir)
        _append(path, event)
    except OSError:
        pass
