"""Shared, pure Python-to-Python helpers for `demo/render.py` and
`demo/canvas/server.py` -- the two things both independently needed and, after
two separate bug-fix rounds to each copy, are worth keeping in one place.

Deliberately NOT shared here: each file's own I/O mode (render.py reads text,
server.py reads binary -- a counterexample can carry arbitrary bytes a text
offset would desync on), its own read/print/lock plumbing, and its
JS-side mirror in `canvas/index.html` (a browser can't import this module).
Those differences are real and stay caller-side; only the pure logic that
had to be patched twice, identically, lives here.

Stdlib-only, matching every other file under `demo/`.
"""

from __future__ import annotations

import os
from pathlib import Path

_PREFIX_PROBE_BYTES = 64


class TailState:
    """Tracks one growing/appendable file's identity across polls.

    `observe(path)` decides whether a caller's existing read cursor into
    `path` is now stale: the file was rotated (a new inode), truncated (size
    shrank), or truncated and immediately rewritten to a same-or-larger size
    -- which defeats a pure inode/size check, since neither signal changes.
    The third case is caught by comparing the file's first
    `_PREFIX_PROBE_BYTES` bytes against what was seen last time; a changed
    prefix means the bytes at the start of the file are no longer the ones
    already served, regardless of the current size. Confirmed empirically
    (PR #300): a truncate followed by a rewrite to >= the old size reproduces
    exactly this miss with inode/size checks alone.

    Raises whatever `Path.stat()`/`open()` raise on a missing/unreadable
    file; the caller's own polling loop already handles that (both existing
    callers poll on a fixed interval and treat "can't stat it" as "waiting
    for the file to appear").
    """

    def __init__(self) -> None:
        self._inode: int | None = None
        self._size = 0
        self._prefix = b""

    def observe(self, path: Path) -> tuple[bool, os.stat_result]:
        """Return `(reset, stat_result)` for the file's current state.

        `reset` is `True` the first time this instance sees a size smaller
        than before, a new inode, or changed leading bytes -- never on the
        very first call (there is nothing yet to compare against).
        """
        st = path.stat()
        try:
            with open(path, "rb") as fh:
                prefix = fh.read(_PREFIX_PROBE_BYTES)
        except OSError:
            prefix = b""

        rotated = self._inode is not None and st.st_ino != self._inode
        truncated = st.st_size < self._size
        content_changed = bool(self._prefix) and prefix != self._prefix
        reset = rotated or truncated or content_changed

        self._inode = st.st_ino
        self._size = st.st_size
        self._prefix = prefix
        return reset, st


def compute_replay_delays(
    events: list[dict], speed: float | None, max_delay_s: float = 3.0
) -> list[float]:
    """The wait-before-this-event delay (seconds) for each event in order.

    Each delay is the event's recorded `ts` gap from the previous event,
    divided by `speed` and capped at `max_delay_s` per gap -- so a long real
    pause in the original session doesn't stall a replay for minutes. Every
    delay is `0.0` when `speed` is `None`/falsy (instant replay) or an
    event's `ts` isn't a number.

    Pure and caller-agnostic: `render.py::run_replay` sleeps each delay in
    turn as it prints; `canvas/server.py::ReplaySource` sums them into a
    fixed cumulative-offset table it compares wall-clock elapsed time
    against. Both need exactly this list; how they use it differs.
    """
    delays: list[float] = []
    prev_ts: float | None = None
    for ev in events:
        ts = ev.get("ts")
        delay = 0.0
        if speed and speed > 0 and prev_ts is not None and isinstance(ts, (int, float)):
            gap = ts - prev_ts
            if gap > 0:
                delay = min(max_delay_s, gap / speed)
        delays.append(delay)
        if isinstance(ts, (int, float)):
            prev_ts = ts
    return delays
