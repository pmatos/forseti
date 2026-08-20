"""Stop-gate wiring for semantic-property surfacing (issue #95).

`property_gate.semantic_check_summary` is monkeypatched to a scripted result --
its own real behaviour (candidate filtering, the per-turn budget, best-effort
failure handling) is covered by `test_property_gate.py`. What's under test
here is stop_gate.main()'s *folding*: a VIOLATED semantic property is always
reported, but never turns an otherwise-clean turn into a `block` decision, and
is folded alongside a real safety-blocking residual when one is also present.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from forseti.adapters.claude_code import forseti_gate as gate
from forseti.adapters.claude_code import property_gate, stop_gate


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("FORSETI_GATE_INCLUDE", "FORSETI_GATE_EXCLUDE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _run(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"cwd": str(project_dir)}))
    )
    stop_gate.main()


def _run_and_capture(
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    _run(project_dir, monkeypatch)
    out = capsys.readouterr().out
    result: dict[str, Any] = json.loads(out)
    return result


_VIOLATION = {
    "unit_id": "f.c::my_abs",
    "property_id": "p1",
    "kind": "semantic",
    "outcome": "violated",
    "k": 4,
}


def _patch_summary(
    monkeypatch: pytest.MonkeyPatch, summary: property_gate.SemanticCheckSummary
) -> None:
    monkeypatch.setattr(
        property_gate, "semantic_check_summary", lambda project_dir, state: summary
    )


def test_semantic_violation_reports_but_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    _patch_summary(
        monkeypatch,
        property_gate.SemanticCheckSummary((_VIOLATION,), checked=1, deferred=0),
    )

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert "decision" not in result  # never blocks on its own
    message = result["systemMessage"]
    assert "did NOT hold" in message
    assert "f.c::my_abs" in message
    assert "p1" in message


_UNRESOLVED = {
    "unit_id": "f.c::my_abs",
    "property_id": "p2",
    "kind": "semantic",
    "outcome": "unknown",
    "k": 4,
}


def test_semantic_unresolved_is_reported_but_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    _patch_summary(
        monkeypatch,
        property_gate.SemanticCheckSummary(
            (), checked=1, deferred=0, unresolved=(_UNRESOLVED,)
        ),
    )

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert "decision" not in result  # never blocks on its own
    message = result["systemMessage"]
    assert "could not be resolved" in message
    assert "f.c::my_abs" in message
    assert "p2" in message


def test_needs_contract_unit_note_is_folded_into_the_block_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NEEDS_CONTRACT unit is honestly-unverified (module docstring), so it
    blocks like any other outstanding unit -- but gets its own "not gated,
    needs no source fix" note (`_needs_message`) rather than reading like a
    counterexample the agent should try to fix."""
    _git_init(tmp_path)
    (tmp_path / "ptr.c").write_text("int f(int *p) { return *p; }\n")
    with gate.gate_lock(str(tmp_path)):
        state = gate.load_state(str(tmp_path))
        state["units"]["ptr.c::f"] = {
            "unit_id": "ptr.c::f",
            "file": "ptr.c",
            "function": "f",
            "verdict": "needs_contract",
            "k": 4,
        }
        gate.save_state(str(tmp_path), state)
    _patch_summary(monkeypatch, property_gate.SemanticCheckSummary((), 0, 0))

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert result["decision"] == "block"
    assert "NOT gated" in result["reason"]
    assert "need no source fix" in result["reason"]
    assert "ptr.c::f" in result["reason"]


def test_no_semantic_violation_is_a_quiet_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    _patch_summary(monkeypatch, property_gate.SemanticCheckSummary((), 0, 0))

    _run(tmp_path, monkeypatch)

    assert capsys.readouterr().out == ""  # exit 0, no message -- unchanged v0 behaviour


def test_semantic_violation_folds_into_a_real_blocking_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / "bad.c").write_text("int f(void) { return 1; }\n")
    # A real safety-blocking unit, recorded directly (no esbmc needed). The
    # backing file must exist on disk, or `prune_deleted_units` drops this
    # entry before `blocking_units` ever sees it (issue #99 review).
    with gate.gate_lock(str(tmp_path)):
        state = gate.load_state(str(tmp_path))
        state["units"]["bad.c::f"] = {
            "unit_id": "bad.c::f",
            "file": "bad.c",
            "function": "f",
            "verdict": "violated",
            "k": 4,
        }
        gate.save_state(str(tmp_path), state)

    _patch_summary(
        monkeypatch,
        property_gate.SemanticCheckSummary((_VIOLATION,), checked=1, deferred=0),
    )

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert result["decision"] == "block"
    reason = result["reason"]
    assert "bad.c::f" in reason  # the real safety residual
    assert "did NOT hold" in reason  # folded in alongside it
    assert "f.c::my_abs" in reason


def test_semantic_only_note_is_not_mislabeled_needs_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A turn with zero NEEDS_CONTRACT units but a semantic-property note must
    not log `decision="allow_needs_contract"` -- that label is a proxy other
    tooling reads as "pointer/array units present", and a purely semantic note
    would give it a false positive (issue #95 review).
    """
    _git_init(tmp_path)
    _patch_summary(
        monkeypatch,
        property_gate.SemanticCheckSummary((_VIOLATION,), checked=1, deferred=0),
    )

    _run_and_capture(tmp_path, monkeypatch, capsys)

    events = (tmp_path / ".forseti" / "events.jsonl").read_text().strip().splitlines()
    last = json.loads(events[-1])
    assert last["decision"] == "allow_semantic_check"


def test_malformed_numeric_env_var_blocks_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed FORSETI_* numeric env var must not silently fall back to
    its default and let the turn proceed as if nothing were wrong (this
    repo's fail-closed convention) -- it must BLOCK, the same "decision":
    "block" shape every other blocking path in this hook uses, not merely
    avoid a traceback (issue #95 review)."""
    _git_init(tmp_path)
    # Exercise the real parse path (not a simulated error) so this proves the
    # actual failure mode: a bad env var reaching `env_int` records into the
    # same list `stop_gate.main()`'s guard reads.
    monkeypatch.setenv("FORSETI_UNWIND", "not-a-number")
    gate.env_int("FORSETI_UNWIND", "1")

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert result["decision"] == "block"
    assert "FORSETI_UNWIND" in result["reason"]
    assert "not-a-number" in result["reason"]


def test_deferred_only_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    _patch_summary(monkeypatch, property_gate.SemanticCheckSummary((), 0, 2))

    result = _run_and_capture(tmp_path, monkeypatch, capsys)

    assert "decision" not in result
    assert "not checked this turn" in result["systemMessage"]
