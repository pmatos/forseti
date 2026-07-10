"""Behavioural tests for the low-level ``forseti-esbmc`` CLI — no esbmc needed.

`esbmc.cli.main` was previously unexercised: its argparse wiring, the `--`
passthrough, the verdict->exit-code epilogue, and the kwargs it hands `verify`
had no coverage without the real binary. Here the subprocess boundary is
replaced by a `verify` stub that records its call and returns a canned verdict,
so the CLI's own behaviour is pinned deterministically and always runs. The
real-verdict end-to-end path lives in test_verify_integration.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forseti.esbmc import cli
from forseti.esbmc.result import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)


def _meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "f.c"),
        exit_code=0,
        duration_s=0.1,
        stdout="",
        stderr="",
    )


class _RecordingVerify:
    """A `verify` stand-in that records its call and returns a fixed verdict."""

    def __init__(self, result: EsbmcResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, source: Path, **kwargs: Any) -> EsbmcResult:
        self.calls.append({"source": source, **kwargs})
        return self._result


def _patch_verify(
    monkeypatch: pytest.MonkeyPatch, result: EsbmcResult
) -> _RecordingVerify:
    stub = _RecordingVerify(result)
    monkeypatch.setattr(cli, "verify", stub)
    return stub


def test_verified_exits_zero_and_renders_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_verify(monkeypatch, Verified(_meta()))
    code = cli.main(["f.c"])
    assert code == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_violated_exits_one_and_prints_counterexample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_verify(monkeypatch, Violated(_meta(), "TRACE-TEXT"))
    code = cli.main(["f.c"])
    assert code == 1
    assert "TRACE-TEXT" in capsys.readouterr().out


def test_unknown_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    # UNKNOWN is deliberately non-zero: an inconclusive run is never a silent pass.
    _patch_verify(monkeypatch, Unknown(_meta(), UnknownReason.TIMEOUT))
    assert cli.main(["f.c"]) == 2


def test_error_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_verify(monkeypatch, Error(_meta(), "bad binary"))
    assert cli.main(["f.c"]) == 3


def test_defaults_are_passed_through_to_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _patch_verify(monkeypatch, Verified(_meta()))
    cli.main(["f.c"])
    (call,) = stub.calls
    assert call["source"] == Path("f.c")
    assert call["unwind"] == 1
    assert call["timeout_s"] == 30.0
    assert call["function"] is None
    assert call["extra_flags"] == ()
    assert call["esbmc_bin"] == "esbmc"


def test_flags_and_passthrough_reach_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _patch_verify(monkeypatch, Verified(_meta()))
    cli.main(
        [
            "f.c",
            "-k",
            "8",
            "-t",
            "5",
            "--function",
            "drfrom_bytes",
            "--esbmc-bin",
            "/opt/esbmc",
            "--",
            "--overflow-check",
            "--memory-leak-check",
        ]
    )
    (call,) = stub.calls
    assert call["unwind"] == 8
    assert call["timeout_s"] == 5.0
    assert call["function"] == "drfrom_bytes"
    assert call["esbmc_bin"] == "/opt/esbmc"
    assert call["extra_flags"] == ("--overflow-check", "--memory-leak-check")
