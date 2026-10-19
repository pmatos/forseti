"""The single-file lifecycle shared by harness configuration installers.

Adapters own decoding, ownership checks, and content transforms. This module
owns symlink refusal, UTF-8 reads, unchanged detection, and atomic replacement
or deletion. It does not group files into a transaction or lock out other edits.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

from forseti.adapters._atomic import write_text_atomic


class FileChange(Enum):
    CREATED = auto()
    UPDATED = auto()
    REMOVED = auto()
    UNCHANGED = auto()


def edit_file[T](
    path: Path,
    *,
    decode: Callable[[str], T],
    transform: Callable[[T | None], T | None],
    encode: Callable[[T], str],
    error_type: type[Exception],
    description: str,
) -> FileChange:
    """Read, transform, and commit one managed file, reporting the change.

    ``None`` represents absence or requested deletion; decoded values must not
    be ``None``. Transforms must leave their input unmodified so equality can
    suppress writes (semantic for JSON, literal for text). Encoding is deferred
    until a value changes. Only read/decode failures are translated; transform,
    encode, write, and unlink failures propagate unchanged.
    """
    if path.is_symlink():
        raise error_type(
            f"{path} is a symlink -- refusing to replace it (an atomic "
            "rewrite would swap in a plain file, silently breaking the link)"
        )
    existed = path.exists()
    existing = None
    if existed:
        try:
            existing = decode(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise error_type(
                f"{path}: cannot read existing {description} ({exc})"
            ) from exc

    updated = transform(existing)
    if updated == existing:
        return FileChange.UNCHANGED
    if updated is None:
        path.unlink()
        return FileChange.REMOVED
    write_text_atomic(path, encode(updated))
    return FileChange.UPDATED if existed else FileChange.CREATED
