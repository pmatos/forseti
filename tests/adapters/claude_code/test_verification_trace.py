"""Trace contracts shared by the edit and out-of-band verification hooks."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from forseti.adapters.claude_code import (
    _verification_trace,
    event_log,
    post_bash,
    post_tool_use,
    verdict_report,
)
from forseti.adapters.claude_code import forseti_gate as gate

_FILE = "src/sample.c"
_VERDICTS = [
    gate.UnitVerdict(
        f"{_FILE}::ok",
        _FILE,
        "ok",
        "verified",
        8,
        argv=("esbmc", "sample.c", "--unwind", "8"),
        duration_s=0.125,
    ),
    gate.UnitVerdict(f"{_FILE}::ptr", _FILE, "ptr", gate.NEEDS_CONTRACT, 8),
    gate.UnitVerdict(
        f"{_FILE}::bad", _FILE, "bad", "violated", 9, argv=(), duration_s=0.0
    ),
    gate.UnitVerdict(f"{_FILE}::slow", _FILE, "slow", "unknown", 16),
    gate.UnitVerdict(f"{_FILE}::broken", _FILE, "broken", "error", 4),
]
_VERIFY_EVENTS = [
    {
        "type": "verify",
        "unit": f"{_FILE}::ok",
        "verdict": "verified",
        "k": 8,
        "duration_s": 0.125,
        "argv": ["esbmc", "sample.c", "--unwind", "8"],
    },
    {
        "type": "verify",
        "unit": f"{_FILE}::ptr",
        "verdict": "needs_contract",
        "k": 8,
        "duration_s": None,
        "argv": None,
    },
    {
        "type": "verify",
        "unit": f"{_FILE}::bad",
        "verdict": "violated",
        "k": 9,
        "duration_s": 0.0,
        "argv": None,
    },
    {
        "type": "verify",
        "unit": f"{_FILE}::slow",
        "verdict": "unknown",
        "k": 16,
        "duration_s": None,
        "argv": None,
    },
    {
        "type": "verify",
        "unit": f"{_FILE}::broken",
        "verdict": "error",
        "k": 4,
        "duration_s": None,
        "argv": None,
    },
]
_CASES = [
    ([], [], 0, 0),
    ([0], [], 0, 0),
    ([1], [], 1, 0),
    ([2], [2], 0, 2),
    ([3], [3], 0, 2),
    ([4], [4], 0, 2),
    ([1, 3, 0, 2, 4], [3, 2, 4], 1, 2),
]


@pytest.mark.parametrize("tool", ["Edit", "Bash"])
@pytest.mark.parametrize(
    ("indices", "failures", "n_needs", "exit_code"),
    _CASES,
    ids=[
        "empty",
        "verified",
        "needs-contract",
        "violated",
        "unknown",
        "error",
        "mixed",
    ],
)
def test_hook_trace_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    indices: list[int],
    failures: list[int],
    n_needs: int,
    exit_code: int,
) -> None:
    """Pin both trigger paths before moving their event projection."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    src = tmp_path / _FILE
    src.parent.mkdir()
    src.write_text("int ok(void) { return 0; }\n", encoding="utf-8")
    verdicts = [_VERDICTS[i] for i in indices]
    monkeypatch.setattr(gate, "env_config_errors", lambda: [])
    monkeypatch.setattr(
        gate, "verify_and_record", lambda file_path, *, project_dir: verdicts
    )
    monkeypatch.setattr(
        gate,
        "discover_changed_c_sources",
        lambda project_dir, *, baseline_head: [str(src)],
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "cwd": str(tmp_path),
                    "tool_name": tool,
                    "tool_input": {"file_path": str(src)},
                }
            )
        ),
    )

    hook = post_bash if tool == "Bash" else post_tool_use
    assert hook.main() == exit_code

    expected = [
        {
            "type": "edit",
            "tool": tool,
            "file": _FILE,
            "functions": [_VERDICTS[i].function for i in indices],
        },
        *[_VERIFY_EVENTS[i] for i in indices],
    ]
    decision = "block" if exit_code else "pass"
    if indices or tool == "Bash":
        expected.append(
            {
                "type": "gate",
                "file": _FILE,
                "decision": decision,
                "n_failures": len(failures),
                "n_needs_contract": n_needs,
                "exit_code": exit_code,
            }
        )
    if tool == "Edit":
        expected.append(
            {
                "type": "gate.decision",
                "harness": "claude-code",
                "adapter": "claude-code-post-tool-use",
                "unit_ids": [_VERDICTS[i].unit_id for i in failures or indices],
                "file": _FILE,
                "decision": decision,
            }
        )
    events = event_log.read_events(tmp_path)
    assert all(isinstance(e.pop("ts"), float) for e in events)
    assert events == expected


@pytest.mark.parametrize("indices", [[], [1, 3, 0, 2, 4, 0]])
def test_record_verification_preserves_order_and_duplicate_evidence(
    tmp_path: Path, indices: list[int]
) -> None:
    _verification_trace.record_verification(
        str(tmp_path), _FILE, "MultiEdit", [_VERDICTS[i] for i in indices]
    )

    events = event_log.read_events(tmp_path)
    for event in events:
        event.pop("ts")
    assert events == [
        {
            "type": "edit",
            "tool": "MultiEdit",
            "file": _FILE,
            "functions": [_VERDICTS[i].function for i in indices],
        },
        *[_VERIFY_EVENTS[i] for i in indices],
    ]


@pytest.mark.parametrize(("indices", "failures", "n_needs", "exit_code"), _CASES)
def test_record_gate_uses_the_report_partition(
    tmp_path: Path,
    indices: list[int],
    failures: list[int],
    n_needs: int,
    exit_code: int,
) -> None:
    part = verdict_report.partition([_VERDICTS[i] for i in indices])
    decision = _verification_trace.record_gate(str(tmp_path), _FILE, part)

    assert decision == ("block" if exit_code else "pass")
    events = event_log.read_events(tmp_path)
    for event in events:
        event.pop("ts")
    assert events == [
        {
            "type": "gate",
            "file": _FILE,
            "decision": decision,
            "n_failures": len(failures),
            "n_needs_contract": n_needs,
            "exit_code": exit_code,
        }
    ]


def test_trace_write_failure_does_not_change_the_decision(tmp_path: Path) -> None:
    (tmp_path / ".forseti").write_text("occupied", encoding="utf-8")
    verdicts = [_VERDICTS[2]]

    _verification_trace.record_verification(str(tmp_path), _FILE, "Edit", verdicts)
    decision = _verification_trace.record_gate(
        str(tmp_path), _FILE, verdict_report.partition(verdicts)
    )

    assert decision == "block"
    assert (tmp_path / ".forseti").read_text(encoding="utf-8") == "occupied"
