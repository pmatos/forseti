"""Source snapshot intent and ownership through the interface used by the gate."""

from __future__ import annotations

import errno
import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal

import pytest

from forseti.adapters.claude_code import _source_snapshot as snapshots
from forseti.adapters.claude_code import forseti_gate as gate

Kind = Literal["enumeration", "verification"]
MTIME_NS = 1_700_000_000_123_456_789


def source_context(kind: Kind, root: Path) -> AbstractContextManager[str]:
    if kind == "enumeration":
        return snapshots.enumeration_source(
            "source.C", b"source", project_dir=str(root)
        )
    return snapshots.verification_source(
        "source.C", b"source", project_dir=str(root), mtime_ns=MTIME_NS
    )


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf\x00\xff\r\n", b"x" * 100_000])
def test_enumeration_uses_only_supplied_bytes(tmp_path: Path, content: bytes) -> None:
    source = tmp_path / "source.C"
    source.write_bytes(b"different live content")
    with snapshots.enumeration_source(
        source, content, project_dir=str(tmp_path)
    ) as path:
        staged = Path(path)
        assert staged.parent == source.parent
        assert staged.suffix == ".C"
        assert staged.name.startswith(gate._ENUM_SNAPSHOT_PREFIX)
        assert staged.read_bytes() == content
    assert not staged.exists()
    assert source.read_bytes() == b"different live content"


def test_no_content_bypasses_staging(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with snapshots.enumeration_source(
        "./not-present.c", None, project_dir=str(missing)
    ) as path:
        assert path == "./not-present.c"
    assert not missing.exists()


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_verification_prepares_bytes_and_mtime(tmp_path: Path, bom: bytes) -> None:
    spelling = 'quote"back\\slash.C'
    with snapshots.verification_source(
        spelling,
        bom + b"int f(void) {}\r\n",
        project_dir=str(tmp_path),
        mtime_ns=MTIME_NS,
    ) as path:
        staged = Path(path)
        assert staged.parent == tmp_path
        assert staged.suffix == ".C"
        assert staged.name.startswith(gate._VERIFY_SNAPSHOT_PREFIX)
        assert staged.read_bytes() == (
            bom + b'#line 1 "quote\\"back\\\\slash.C"\nint f(void) {}\r\n'
        )
        assert staged.stat().st_mtime_ns == MTIME_NS
    assert not staged.exists()


@pytest.mark.parametrize("kind", ["enumeration", "verification"])
def test_consumer_exception_survives_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    original = OSError("consumer failed")
    real_unlink = os.unlink
    with pytest.raises(OSError) as caught, source_context(kind, tmp_path) as path:

        def cannot_unlink(path: str) -> None:
            raise OSError("cleanup failed")

        monkeypatch.setattr(os, "unlink", cannot_unlink)
        raise original
    assert caught.value is original
    real_unlink(path)


@pytest.mark.parametrize("kind", ["enumeration", "verification"])
def test_successful_cleanup_has_intent_specific_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    real_unlink = os.unlink

    def cannot_unlink(path: str) -> None:
        raise OSError("cleanup failed")

    context = source_context(kind, tmp_path)
    if kind == "enumeration":
        with (
            pytest.raises(gate.UnitsUnavailable, match=r"enumerated.*could not remove"),
            context as path,
        ):
            monkeypatch.setattr(os, "unlink", cannot_unlink)
    else:
        with context as path:
            monkeypatch.setattr(os, "unlink", cannot_unlink)
    real_unlink(path)


@pytest.mark.parametrize("kind", ["enumeration", "verification"])
def test_fdopen_failure_closes_descriptor_and_removes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    real_mkstemp = tempfile.mkstemp
    created: list[tuple[int, str]] = []

    def capture(*, prefix: str, suffix: str, dir: str) -> tuple[int, str]:
        result = real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        created.append(result)
        return result

    def fail_open(fd: int, mode: str) -> None:
        raise OSError("fdopen failed")

    monkeypatch.setattr(tempfile, "mkstemp", capture)
    monkeypatch.setattr(os, "fdopen", fail_open)
    with (
        pytest.raises(gate.UnitsUnavailable, match="fdopen failed"),
        source_context(kind, tmp_path),
    ):
        pass
    [(fd, path)] = created
    assert not Path(path).exists()
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ["enumeration", "verification"])
def test_interrupted_write_removes_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    class InterruptingHandle:
        def __enter__(self) -> InterruptingHandle:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def write(self, data: bytes) -> int:
            raise KeyboardInterrupt

    def interrupt(fd: int, mode: str) -> InterruptingHandle:
        os.close(fd)
        return InterruptingHandle()

    monkeypatch.setattr(os, "fdopen", interrupt)
    with pytest.raises(KeyboardInterrupt), source_context(kind, tmp_path):
        pass
    assert list(tmp_path.iterdir()) == []
