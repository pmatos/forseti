"""Claude Code's command-specific failure policy at the transport seam."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forseti.adapters.claude_code import forseti_gate as gate


@pytest.mark.parametrize("exit_code", [0, 1])
def test_verify_accepts_json_verdict_regardless_of_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=exit_code,
            stdout=json.dumps({"verdict": "violated", "counterexample": "trace"}),
            stderr="diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = gate.verify_function("x.c", "f", project_dir=str(tmp_path))
    assert result.verdict == "violated"
    assert result.counterexample == "trace"


def test_listing_rejects_nonzero_status_even_with_valid_units_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout='{"units": []}', stderr="  " + "x" * 900
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gate.UnitsUnavailable) as caught:
        gate.extract_function_defs("x.c", project_dir=str(tmp_path))
    assert str(caught.value) == "x" * 800
    assert caught.value.__cause__ is None


def test_listing_invalid_shape_reports_stdout_instead_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = json.dumps({"unexpected": "x" * 900})

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr="unrelated stderr"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gate.UnitsUnavailable) as caught:
        gate.extract_function_defs("x.c", project_dir=str(tmp_path))
    assert str(caught.value).endswith(output[:800])
    assert "unrelated stderr" not in str(caught.value)


@pytest.mark.parametrize("exit_code", [0, 2])
def test_listing_decode_failure_preserves_status_precedence_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=exit_code, stdout="not json", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gate.UnitsUnavailable) as caught:
        gate.extract_function_defs("x.c", project_dir=str(tmp_path))
    assert str(caught.value) == "not json"
    if exit_code == 0:
        assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    else:
        assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "error, expected",
    [
        (FileNotFoundError("missing"), "error"),
        (subprocess.TimeoutExpired("forseti", 10), "unknown"),
    ],
)
def test_verify_launch_failures_keep_their_distinct_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = gate.verify_function("x.c", "f", project_dir=str(tmp_path))
    assert result.verdict == expected


def test_verify_propagates_other_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = PermissionError("cannot execute")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PermissionError) as caught:
        gate.verify_function("x.c", "f", project_dir=str(tmp_path))
    assert caught.value is error


@pytest.mark.parametrize(
    "error",
    [OSError("cannot execute"), subprocess.TimeoutExpired("forseti", 10)],
)
def test_listing_launch_failure_keeps_its_original_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gate.UnitsUnavailable) as caught:
        gate.extract_function_defs("x.c", project_dir=str(tmp_path))
    assert caught.value.__cause__ is error
