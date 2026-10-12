"""Behavioral tests for the shared adapter CLI/JSON transport."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from forseti.adapters import _cli_json
from forseti.adapters._cli_json import capture_json_command


def test_captured_command_retains_status_output_and_diagnostic_choices(
    tmp_path: Path,
) -> None:
    command = capture_json_command(
        [
            sys.executable,
            "-c",
            "import json, os, sys; "
            "print(json.dumps({'cwd': os.getcwd()})); "
            "sys.stderr.write('  ' + 'x' * 900 + '  '); sys.exit(2)",
        ],
        timeout=10,
        cwd=tmp_path,
    )

    assert command.returncode == 2
    assert command.decode() == {"cwd": str(tmp_path)}
    assert json.loads(command.stdout) == {"cwd": str(tmp_path)}
    assert command.stderr == "  " + "x" * 900 + "  "
    assert command.diagnostic() == "x" * 400
    assert command.diagnostic(limit=800) == "x" * 800


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        PermissionError("cannot execute"),
        subprocess.TimeoutExpired("forseti", 10),
        subprocess.SubprocessError("other process failure"),
    ],
)
def test_capture_propagates_original_process_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(_cli_json.subprocess, "run", fake_run)
    with pytest.raises(type(error)) as caught:
        capture_json_command(["forseti", "verify"], timeout=10)
    assert caught.value is error


def test_capture_defers_decoding_until_the_caller_requests_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="not json", stderr=""
        )

    monkeypatch.setattr(_cli_json.subprocess, "run", fake_run)
    command = capture_json_command(["forseti", "list-units"], timeout=10)
    assert command.returncode == 2
    assert command.diagnostic(limit=800) == "not json"
    with pytest.raises(json.JSONDecodeError) as caught:
        command.decode()
    assert caught.value.doc == "not json"


def test_nonzero_command_with_json_is_a_decoded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps({"verdict": "violated"}),
            stderr="diagnostic",
        )

    monkeypatch.setattr(_cli_json.subprocess, "run", fake_run)

    result = _cli_json.run_json(["forseti", "verify"], timeout=120)

    assert result.payload == {"verdict": "violated"}
    assert result.failure is None
    assert result.diagnostic == "diagnostic"


def test_launch_failure_is_distinct_from_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("forseti not found")

    monkeypatch.setattr(_cli_json.subprocess, "run", fake_run)

    result = _cli_json.run_json(["forseti", "verify"], timeout=120)

    assert result.payload is None
    assert result.failure == "launch"
    assert result.diagnostic == "forseti not found"


def test_invalid_output_prefers_and_bounds_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="not json",
            stderr=f"  {'x' * 500}  ",
        )

    monkeypatch.setattr(_cli_json.subprocess, "run", fake_run)

    result = _cli_json.run_json(["forseti", "verify"], timeout=120)

    assert result.payload is None
    assert result.failure == "decode"
    assert result.diagnostic == "x" * 400
