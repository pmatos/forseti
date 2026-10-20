"""Stable source reads and their distinct gate failure policies."""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest

from forseti.adapters.claude_code import forseti_gate as gate
from forseti.adapters.claude_code._source_observation import (
    MTIME_STABLE_ATTEMPTS,
    observe_stable_source,
)


@pytest.mark.parametrize("raw", [b"", b"\x00\xff\r\n", b"source\n" * 100_000])
def test_observation_preserves_bytes_and_nanosecond_mtime(
    tmp_path: Path, raw: bytes
) -> None:
    src = tmp_path / "source.c"
    src.write_bytes(raw)
    mtime_ns = 1_700_000_000_123_456_789
    os.utime(src, ns=(mtime_ns, mtime_ns))

    assert observe_stable_source(src, bytes) == (raw, src.stat().st_mtime_ns)


def test_projection_is_inside_bracket_and_retry_discards_previous_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite during capture must retry both the read and its projection."""
    src = tmp_path / "source.c"
    src.write_bytes(b"first")
    first_mtime = src.stat().st_mtime_ns
    new_mtime = first_mtime + 5_000_000_000
    target_ino = src.stat().st_ino
    real_fstat = os.fstat
    events: list[str] = []

    def observed_stat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if result.st_ino == target_ino:
            events.append("stat")
        return result

    monkeypatch.setattr(os, "fstat", observed_stat)

    def capture(raw: bytes) -> str:
        events.append("capture")
        if raw == b"first":
            src.write_bytes(b"second")
            os.utime(src, ns=(new_mtime, new_mtime))
        return hashlib.sha256(raw).hexdigest()

    assert observe_stable_source(src, capture) == (
        hashlib.sha256(b"second").hexdigest(),
        src.stat().st_mtime_ns,
    )
    assert events == ["stat", "capture", "stat"] * 2


def test_each_retry_reopens_the_source_path(tmp_path: Path) -> None:
    """A retry must observe a replacement inode rather than reuse the old fd."""
    src = tmp_path / "source.c"
    src.write_bytes(b"first")
    original_mtime = src.stat().st_mtime_ns

    def replace_during_capture(raw: bytes) -> bytes:
        if raw == b"first":
            # Move the first inode's mtime so this bracket cannot be accepted,
            # then replace the path while the reader still holds that inode.
            moved_mtime = original_mtime + 5_000_000_000
            os.utime(src, ns=(moved_mtime, moved_mtime))
            replacement = tmp_path / "replacement.c"
            replacement.write_bytes(b"second")
            replacement.replace(src)
        return raw

    assert observe_stable_source(src, replace_during_capture) == (
        b"second",
        src.stat().st_mtime_ns,
    )


def test_exhaustion_returns_no_observation(tmp_path: Path) -> None:
    src = tmp_path / "source.c"
    src.write_bytes(b"source")
    mtime_ns = src.stat().st_mtime_ns
    captured: list[bytes] = []

    def move_mtime(raw: bytes) -> bytes:
        nonlocal mtime_ns
        captured.append(raw)
        mtime_ns += 5_000_000_000
        os.utime(src, ns=(mtime_ns, mtime_ns))
        return raw

    assert observe_stable_source(src, move_mtime) is None
    assert captured == [b"source"] * MTIME_STABLE_ATTEMPTS


def test_unreadable_source_propagates_oserror_without_capturing(tmp_path: Path) -> None:
    captured: list[bytes] = []

    with pytest.raises(FileNotFoundError):
        observe_stable_source(tmp_path / "missing.c", captured.append)
    assert captured == []


def test_none_capture_is_distinct_from_exhaustion(tmp_path: Path) -> None:
    src = tmp_path / "source.c"
    src.write_bytes(b"source")

    assert observe_stable_source(src, lambda raw: None) == (
        None,
        src.stat().st_mtime_ns,
    )


def test_capture_errors_propagate_without_retry(tmp_path: Path) -> None:
    src = tmp_path / "source.c"
    src.write_bytes(b"source")
    failure = ValueError("capture failed")
    calls = 0

    def fail_capture(raw: bytes) -> bytes:
        nonlocal calls
        calls += 1
        raise failure

    with pytest.raises(ValueError) as caught:
        observe_stable_source(src, fail_capture)
    assert caught.value is failure
    assert calls == 1


@pytest.mark.parametrize("superseded", [False, True])
def test_unstable_claim_read_preserves_prior_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, superseded: bool
) -> None:
    """Exhaustion under the claim lock reports after releasing it, without charging."""
    src = tmp_path / "x.c"
    src.write_bytes(b"source\n")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    prior_claim = {"hash": digest, "attempts": 2, "pid": 123}
    state = gate.load_state(str(tmp_path))
    state["pending"] = {"x.c": prior_claim}
    if superseded:
        state["scanned"] = {"x.c": digest}
    gate.save_state(str(tmp_path), state)
    monkeypatch.setattr(gate, "extract_function_defs", lambda *a, **kw: [])

    lock_held = False
    real_lock = gate.gate_lock

    @contextlib.contextmanager
    def tracked_lock(project_dir: str) -> Generator[None]:
        nonlocal lock_held
        assert not lock_held, "a blocking error must wait until the claim lock releases"
        with real_lock(project_dir):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    monkeypatch.setattr(gate, "gate_lock", tracked_lock)
    real_fstat = os.fstat
    target_ino = src.stat().st_ino
    calls = 0

    def moving_claim_mtime(fd: int) -> object:
        nonlocal calls
        stat = real_fstat(fd)
        if stat.st_ino != target_ino:
            return stat
        calls += 1
        if calls <= 2:
            assert not lock_held
            return stat
        assert lock_held, "the fresh observation must stay inside the claim lock"
        return SimpleNamespace(st_mtime_ns=stat.st_mtime_ns + calls)

    monkeypatch.setattr(os, "fstat", moving_claim_mtime)

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert calls == 2 + 2 * gate._MTIME_STABLE_ATTEMPTS
    after = gate.load_state(str(tmp_path))
    assert after["pending"]["x.c"] == prior_claim
    if superseded:
        assert verdicts == []
        assert "x.c::?" not in after["units"]
        assert after["scanned"]["x.c"] == digest
    else:
        assert [v.verdict for v in verdicts] == ["error"]
        assert verdicts[0].detail == (
            f"{src} kept changing while being read; could not pair "
            "its bytes with a stable mtime"
        )
        assert "x.c" not in after["scanned"]


def test_unreadable_claim_read_uses_content_change_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable second observation differs from an unreadable initial read."""
    src = tmp_path / "x.c"
    src.write_bytes(b"source\n")

    def remove_during_enumeration(
        *args: object, **kwargs: object
    ) -> list[gate.FuncDef]:
        src.unlink()
        return []

    monkeypatch.setattr(gate, "extract_function_defs", remove_during_enumeration)

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    assert verdicts[0].detail == (
        "source changed while its units were being enumerated; not recording "
        "a scan of content that was not enumerated — re-edit to re-verify"
    )
    state = gate.load_state(str(tmp_path))
    assert "x.c" not in state["scanned"]
    assert "x.c" not in state["pending"]
