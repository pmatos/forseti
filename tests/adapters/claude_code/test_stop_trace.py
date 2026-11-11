"""Final Stop trace schemas, exercised through the hook and its recorder."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from forseti.adapters.claude_code import (
    _stop_trace,
    event_log,
    property_gate,
    stop_gate,
)
from forseti.adapters.claude_code import forseti_gate as gate


def _summary(*, violations: bool = False) -> property_gate.SemanticCheckSummary:
    verdict = {"unit_id": "f.c::f", "property_id": "p", "k": 4}
    return property_gate.SemanticCheckSummary(
        violations=(verdict,) if violations else (),
        checked=12,
        deferred=13,
        unresolved=(verdict,) * 2,
        failed=3,
        skipped=(verdict,) * 4,
        vacuous=(verdict,) * 5,
    )


def _events(project_dir: Path) -> list[dict[str, Any]]:
    events = event_log.read_events(project_dir)
    for event in events:
        assert isinstance(event.pop("ts"), float)
    return events


_SEMANTIC_COUNTS = {
    "n_semantic_violations": 0,
    "n_semantic_unresolved": 2,
    "n_semantic_failed": 3,
    "n_semantic_skipped": 4,
    "n_semantic_vacuous": 5,
}
_EMPTY = property_gate.SemanticCheckSummary((), checked=0, deferred=0)
_CASES = [
    ("quiet", _EMPTY, 0, 0, 0, 0, 0, {"n_unverified": 0, "attempt": 0}),
    ("semantic-note", _summary(), 0, 0, 0, 0, 0, _SEMANTIC_COUNTS),
    ("needs-note", _summary(), 2, 0, 0, 0, 0, _SEMANTIC_COUNTS),
    ("block", _summary(violations=True), 2, 3, 4, 5, 0, _SEMANTIC_COUNTS),
    ("residual", _summary(violations=True), 2, 3, 4, 5, 99, _SEMANTIC_COUNTS),
]


@pytest.mark.parametrize(
    ("case", "semantic", "needs", "blocking", "oob", "blob", "attempts", "counts"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_hook_final_stop_trace_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    semantic: property_gate.SemanticCheckSummary,
    needs: int,
    blocking: int,
    oob: int,
    blob: int,
    attempts: int,
    counts: dict[str, int],
) -> None:
    """Pin complete final payloads before moving their projection."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(gate, "env_config_errors", lambda: [])
    monkeypatch.setattr(gate, "MAX_STOP_ATTEMPTS", 3)
    monkeypatch.setattr(
        gate, "load_state", lambda project_dir: {"stop_attempts": attempts}
    )
    monkeypatch.setattr(gate, "save_state", lambda project_dir, state: None)
    monkeypatch.setattr(gate, "prune_deleted_units", lambda state, project_dir: [])
    monkeypatch.setattr(gate, "blocking_units", lambda state: [{}] * blocking)
    monkeypatch.setattr(gate, "needs_contract_units", lambda state: [{}] * needs)
    monkeypatch.setattr(
        gate, "discover_changed_c_sources", lambda project_dir, **kwargs: []
    )
    monkeypatch.setattr(
        gate, "divergent_blob_sources", lambda project_dir, state, **kwargs: [{}] * blob
    )
    monkeypatch.setattr(
        gate,
        "sources_needing_verify",
        lambda project_dir, state, discovered: ["f.c"] * oob,
    )
    monkeypatch.setattr(
        property_gate, "semantic_check_summary", lambda project_dir, state: semantic
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    assert stop_gate.main() == 0

    if case == "quiet":
        expected: dict[str, Any] = {"type": "stop", "decision": "allow", **counts}
        assert capsys.readouterr().out == ""
    elif case in ("semantic-note", "needs-note"):
        expected = {
            "type": "stop",
            "decision": "allow_needs_contract" if needs else "allow_semantic_check",
            "n_needs_contract": needs,
            **counts,
            "attempt": 0,
        }
        assert "systemMessage" in json.loads(capsys.readouterr().out)
    else:
        expected = {
            "type": "stop",
            "decision": case,
            "n_unverified": blocking,
            "n_oob": oob,
            "n_blob": blob,
            "attempt": attempts + 1,
            "n_needs_contract": needs,
            **counts,
            "n_semantic_violations": 1,
        }
        output = json.loads(capsys.readouterr().out)
        assert (output.get("decision") == "block") == (case == "block")

    events = _events(tmp_path)
    assert events[0] == expected
    if semantic.violations:
        assert events[1:] == [
            {
                "type": "gate.decision",
                "harness": "claude-code",
                "adapter": "claude-code-stop-gate",
                "unit_ids": ["f.c::f"],
                "decision": case,
            }
        ]
    else:
        assert len(events) == 1


@pytest.mark.parametrize(
    ("semantic", "needs", "decision", "counts"),
    [
        (_EMPTY, 0, "allow", {"n_unverified": 0, "attempt": 0}),
        (
            property_gate.SemanticCheckSummary((), checked=10, deferred=0),
            0,
            "allow",
            {"n_unverified": 0, "attempt": 0},
        ),
        (_summary(), 0, "allow_semantic_check", _SEMANTIC_COUNTS),
        (_summary(), 2, "allow_needs_contract", _SEMANTIC_COUNTS),
        (_EMPTY, 1, "allow_needs_contract", dict.fromkeys(_SEMANTIC_COUNTS, 0)),
        (
            property_gate.SemanticCheckSummary((), checked=0, deferred=13),
            0,
            "allow_semantic_check",
            dict.fromkeys(_SEMANTIC_COUNTS, 0),
        ),
    ],
    ids=["quiet", "checked-only", "semantic", "mixed", "needs-only", "deferred-only"],
)
def test_record_allow_projects_the_existing_sparse_schemas(
    tmp_path: Path,
    semantic: property_gate.SemanticCheckSummary,
    needs: int,
    decision: str,
    counts: dict[str, int],
) -> None:
    _stop_trace.record_allow(str(tmp_path), semantic, n_needs_contract=needs)

    expected: dict[str, Any] = {"type": "stop", "decision": decision, **counts}
    if decision != "allow":
        expected.update(n_needs_contract=needs, attempt=0)
    assert _events(tmp_path) == [expected]


@pytest.mark.parametrize("decision", ["block", "residual"])
@pytest.mark.parametrize("semantic", [_EMPTY, _summary(violations=True)])
def test_record_outstanding_preserves_each_count_and_actual_attempt(
    tmp_path: Path,
    decision: Literal["block", "residual"],
    semantic: property_gate.SemanticCheckSummary,
) -> None:
    _stop_trace.record_outstanding(
        str(tmp_path),
        semantic,
        decision=decision,
        attempt=7,
        n_unverified=8,
        n_oob=9,
        n_blob=10,
        n_needs_contract=11,
    )

    counts = (
        {**_SEMANTIC_COUNTS, "n_semantic_violations": 1}
        if semantic.violations
        else dict.fromkeys(_SEMANTIC_COUNTS, 0)
    )
    assert _events(tmp_path) == [
        {
            "type": "stop",
            "decision": decision,
            "n_unverified": 8,
            "n_oob": 9,
            "n_blob": 10,
            "attempt": 7,
            "n_needs_contract": 11,
            **counts,
        }
    ]


def test_recording_is_best_effort_and_does_not_mutate_the_summary(
    tmp_path: Path,
) -> None:
    (tmp_path / ".forseti").write_text("occupied", encoding="utf-8")
    semantic = _summary(violations=True)

    _stop_trace.record_allow(str(tmp_path), semantic, n_needs_contract=0)
    _stop_trace.record_outstanding(
        str(tmp_path),
        semantic,
        decision="block",
        attempt=1,
        n_unverified=0,
        n_oob=0,
        n_blob=0,
        n_needs_contract=0,
    )

    assert (tmp_path / ".forseti").read_text(encoding="utf-8") == "occupied"
    assert semantic == _summary(violations=True)
