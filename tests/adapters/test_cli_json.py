"""Behavioral tests for the shared adapter CLI/JSON transport."""

from __future__ import annotations

import json
import subprocess

import pytest

from forseti.adapters import _cli_json


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
