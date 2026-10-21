"""Bounded observations pairing source content with its physical mtime.

A same-descriptor read alone can still race an in-place rewrite. Bracketing it
with ``fstat`` and rejecting a changed mtime keeps the accepted bytes paired
with the clock that ``__TIMESTAMP__`` will read from a staged snapshot. Content
hashes, rather than metadata, remain the gate's authority for content identity.
"""

from __future__ import annotations

import os
from collections.abc import Callable

# A continuously rewritten file must exhaust the observation rather than spin.
MTIME_STABLE_ATTEMPTS = 5


def observe_stable_source[T](
    path: str | os.PathLike[str], capture: Callable[[bytes], T]
) -> tuple[T, int] | None:
    """Return captured content and mtime, or ``None`` after unstable attempts.

    Each attempt opens the path afresh and brackets the read and ``capture``
    with ``fstat`` through that same descriptor. ``capture`` runs once per
    attempt; its value is discarded if the timestamps disagree. Retaining bytes
    and hashing them are the gate's two captures, preserving where each site's
    hash occurs relative to the bracket. The descriptor closes on every path.

    ``OSError`` and capture errors propagate unchanged; exhaustion is distinct
    from unreadability. Locking, supersession, verdicts, and retry charges belong
    to the caller. This observation does not freeze the live file afterwards.
    """
    for _ in range(MTIME_STABLE_ATTEMPTS):
        with open(path, "rb") as handle:
            before_ns = os.fstat(handle.fileno()).st_mtime_ns
            value = capture(handle.read())
            mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
        if mtime_ns == before_ns:
            return value, mtime_ns
    return None
