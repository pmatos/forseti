"""Atomic single-file text writes, shared by every adapter installer/logger.

Stdlib only -- no `forseti` import -- so an adapter package that must stay
importable without the rest of `forseti` (e.g. a hook script) can still use it.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path


def write_text_atomic(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` to `path` atomically: pid-qualified temp file + rename.

    Never leaves a torn/partial file behind -- a process killed mid-write
    leaves the temp file, not a truncated `path`.

    The temp file is created at mode 0600, then widened to `path`'s own mode
    just before the swap if `path` already exists (left at 0600 for a fresh
    file). `Path.replace` installs the *temp* file's inode, not `path`'s old
    one, so without this a settings file a user `chmod 600`'d (it can hold a
    Claude Code `env` block with API keys) would silently widen to the
    process umask default on every rerun -- and never sit world-readable even
    for the instant between the write and the swap.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # A raw os.write() may legally write fewer bytes than given (a "short
    # write"); fdopen's buffered file object loops internally to guarantee
    # the whole payload lands, while still owning (and closing) `fd` itself.
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))
    with contextlib.suppress(FileNotFoundError):
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
    tmp.replace(path)
