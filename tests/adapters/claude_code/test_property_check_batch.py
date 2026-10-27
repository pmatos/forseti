"""The selected-unit check interface owns timing and unit accounting together."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from forseti.adapters.claude_code import property_gate
from forseti.adapters.claude_code.property_gate import _check_selected_units


@pytest.fixture(autouse=True)
def check_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(property_gate, "CHECK_TIMEOUT_S", 20.0)
    monkeypatch.setattr(property_gate, "VERIFY_GRACE_S", 5.0)
    monkeypatch.setattr(property_gate, "_SUBPROCESS_MARGIN_S", 10.0)
    monkeypatch.setattr(property_gate, "_MIN_ATTEMPT_BUDGET_S", 35.0)
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 100.0)
    monkeypatch.setattr(property_gate.gate, "resolve_forseti_cmd", lambda: ["forseti"])
    monkeypatch.setattr(property_gate.gate, "build_flags_from_env", lambda: [])


def test_empty_selection_starts_no_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("an empty selection must not execute a command")

    monkeypatch.setattr(subprocess, "run", unexpected)
    batch = _check_selected_units("project", [])

    assert batch.verdicts == ()
    assert (batch.checked, batch.deferred, batch.failed) == (0, 0, 0)


@pytest.mark.parametrize(
    ("elapsed", "count", "times_out", "timeout", "expected"),
    [
        (65.5, 1, False, None, (0, 1, 0)),
        (65.0, 1, True, 35.0, (0, 1, 0)),
        (40.0, 1, True, 60.0, (0, 0, 1)),
        (65.0, 2, True, 35.0, (0, 1, 0)),
        (65.0, 2, False, 35.0, (1, 0, 0)),
        (40.0, 2, True, 60.0, (0, 1, 0)),
        (0.0, 0, False, 60.0, (1, 0, 0)),
        (0.0, 6, False, 100.0, (1, 0, 0)),
    ],
)
def test_admission_and_timeout_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    count: int,
    times_out: bool,
    timeout: float | None,
    expected: tuple[int, int, int],
) -> None:
    ticks = iter((10.0, 10.0 + elapsed))
    monkeypatch.setattr(property_gate.time, "monotonic", lambda: next(ticks))
    observed: list[float] = []
    verdicts = [{"outcome": "unknown"}, "ignored by summary", {"outcome": "skipped"}]

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(kwargs["timeout"])
        if times_out:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(
            argv, 3, stdout=json.dumps({"verdicts": verdicts})
        )

    monkeypatch.setattr(subprocess, "run", run)
    batch = _check_selected_units(
        "project", [({"file": "f.c", "function": "f"}, count)]
    )

    assert observed == ([] if timeout is None else [timeout])
    assert (batch.checked, batch.deferred, batch.failed) == expected
    assert batch.verdicts == (tuple(verdicts) if expected[0] else ())


@pytest.mark.parametrize("failure", ["oserror", "json", "shape", "flags"])
def test_clamping_does_not_reclassify_tooling_failures(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    ticks = iter((10.0, 75.0))
    monkeypatch.setattr(property_gate.time, "monotonic", lambda: next(ticks))

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 35.0  # natural timeout for two properties is 110
        if failure == "oserror":
            raise OSError("missing executable")
        return subprocess.CompletedProcess(
            argv, 3, stdout="invalid JSON" if failure == "json" else '{"verdicts": {}}'
        )

    def bad_flags() -> list[str]:
        raise property_gate.gate.UnitsUnavailable("bad quoting")

    monkeypatch.setattr(subprocess, "run", run)
    if failure == "flags":
        monkeypatch.setattr(property_gate.gate, "build_flags_from_env", bad_flags)
    batch = _check_selected_units("project", [({"file": "f.c", "function": "f"}, 2)])

    assert (batch.checked, batch.deferred, batch.failed) == (0, 0, 1)
    assert batch.verdicts == ()


def test_later_failures_and_exhaustion_preserve_completed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 10.0, 40.0, 70.0, 100.0))
    monkeypatch.setattr(property_gate.time, "monotonic", lambda: next(ticks))
    calls: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv[2])
        if argv[2] == "b.c":
            raise OSError("missing executable")
        if argv[2] == "c.c":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(
            argv, 2, stdout='{"verdicts": [{"outcome": "violated"}]}'
        )

    monkeypatch.setattr(subprocess, "run", run)
    batch = _check_selected_units(
        "project",
        [({"file": f"{name}.c", "function": name}, 2) for name in "abcde"],
    )

    assert calls == ["a.c", "b.c", "c.c"]
    assert (batch.checked, batch.deferred, batch.failed) == (1, 3, 1)
    assert batch.verdicts == ({"outcome": "violated"},)


def test_unexpected_exceptions_propagate_without_starting_another_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv[2])
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        _check_selected_units(
            "project",
            [({"file": f"{name}.c", "function": name}, 1) for name in "ab"],
        )
    assert calls == ["a.c"]
