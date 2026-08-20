"""Hermetic tests for `property_gate.py` (issue #95's Stop-gate surfacing).

No esbmc: `_check_unit`'s `subprocess.run` is monkeypatched, so these pin the
gate-level wiring (fast no-op, candidate filtering, the per-turn unit budget,
best-effort failure handling) without needing a real `forseti check` round
trip -- that is `tests/core/test_check_integration.py`'s job one layer down.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forseti.adapters.claude_code import property_gate
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    make_property_id,
)


def _unit(rel: str, function: str, verdict: str = "verified") -> dict[str, Any]:
    return {"file": rel, "function": function, "verdict": verdict, "k": 4}


def _state(*units: dict[str, Any]) -> dict[str, Any]:
    return {"units": {f"{u['file']}::{u['function']}": u for u in units}}


def _add_candidate(root: Path, unit_id: str, expression: str = "result >= 0") -> None:
    store = PropertyStore.open(root / ".forseti")
    store.add(
        Property(
            property_id=make_property_id(unit_id, PropertyKind.SEMANTIC, expression),
            unit_id=unit_id,
            kind=PropertyKind.SEMANTIC,
            expression=expression,
            status=PropertyStatus.CANDIDATE,
            provenance=Provenance("test", "v1"),
        )
    )
    store.close()


def _payload(
    unit_id: str, outcome: str, *, property_id: str = "p1", k: int = 4
) -> dict[str, Any]:
    counts = {"held": 0, "violated": 0, "unknown": 0, "error": 0, "skipped": 0}
    counts[outcome] = 1
    return {
        "unit_id": unit_id,
        "counts": counts,
        "verdicts": [
            {
                "property_id": property_id,
                "unit_id": unit_id,
                "kind": "semantic",
                "outcome": outcome,
                "k": k,
            }
        ],
    }


def _fake_run(
    payload: dict[str, Any],
    exit_code: int = 0,
    captured: dict[str, Any] | None = None,
) -> Any:
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if captured is not None:
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, exit_code, stdout=json.dumps(payload))

    return fake_run


def test_no_store_file_is_a_fast_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("subprocess.run must not be called with no store")

    monkeypatch.setattr(subprocess, "run", _boom)
    state = _state(_unit("f.c", "my_abs"))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.empty
    assert summary.violations == ()


def test_store_exists_but_no_candidates_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    PropertyStore.open(tmp_path / ".forseti").close()  # creates forseti.db, no rows

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("subprocess.run must not be called with no candidates")

    monkeypatch.setattr(subprocess, "run", _boom)
    state = _state(_unit("f.c", "my_abs"))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.empty


def test_non_verified_units_are_never_candidates_for_checking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A unit the safety gate itself has NOT verified (violated/unknown/error/
    # needs_contract) must not be semantically checked here -- its safety
    # verdict already blocks the turn on its own.
    _add_candidate(tmp_path, f"{tmp_path / 'f.c'}::my_abs")

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("subprocess.run must not run for a non-verified unit")

    monkeypatch.setattr(subprocess, "run", _boom)
    state = _state(_unit("f.c", "my_abs", verdict="violated"))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.empty


def test_candidate_for_a_verified_unit_shells_to_forseti_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess, "run", _fake_run(_payload(unit_id, "violated"), 1, captured)
    )

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 1
    assert summary.deferred == 0
    assert len(summary.violations) == 1
    assert summary.violations[0]["property_id"] == "p1"

    argv = captured["argv"]
    assert "check" in argv
    assert str(source) in argv
    assert "--function" in argv and "my_abs" in argv
    assert "--unwind-ladder" in argv
    assert argv[argv.index("--unwind-ladder") + 1] == ""  # no escalation budget
    assert "--json" in argv
    assert captured["cwd"] == str(tmp_path)


def test_relative_unit_file_is_matched_by_the_check_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production `state["units"]` stores `file` project-relative (`gate.unit_id`),
    not absolute -- unlike every other fixture here. `_check_unit` must pass that
    same relative spelling through to `forseti check --json` (relying on the
    subprocess's own `cwd=project_dir`), or the check's store lookup builds a
    different `unit_id` than the one `_units_with_candidates` just proved has a
    candidate, and it silently finds nothing (issue #95 review).
    """
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "f.c"
    source.write_text("int my_abs(int x) { return x; }\n")
    rel = "src/f.c"
    unit_id = f"{rel}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(rel, "my_abs"))

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess, "run", _fake_run(_payload(unit_id, "violated"), 1, captured)
    )

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 1
    assert len(summary.violations) == 1
    argv = captured["argv"]
    # The positional `source` arg (right after "check") must be the exact
    # relative spelling `_units_with_candidates` matched -- not project_dir-
    # joined into an absolute path (the store-root arg legitimately is one).
    assert argv[argv.index("check") + 1] == rel


def test_unresolved_outcomes_are_reported_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md: "never silently pass" UNKNOWN -- ERROR gets the same treatment
    here, since both mean `forseti check` could not settle the property either
    way, distinct from HELD/VIOLATED.
    """
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    monkeypatch.setattr(subprocess, "run", _fake_run(_payload(unit_id, "unknown")))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.violations == ()
    assert len(summary.unresolved) == 1
    assert summary.unresolved[0]["outcome"] == "unknown"
    assert not summary.empty  # never a silent pass


def test_held_property_reports_no_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    monkeypatch.setattr(subprocess, "run", _fake_run(_payload(unit_id, "held")))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 1
    assert summary.violations == ()
    assert summary.empty  # quiet pass -- nothing worth reporting


def test_tooling_failure_is_best_effort_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def raising_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="forseti", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", raising_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.violations == ()


def test_corrupt_store_is_best_effort_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".forseti").mkdir()
    (tmp_path / ".forseti" / "forseti.db").write_text("not a database")

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must never reach a check call from a corrupt store")

    monkeypatch.setattr(subprocess, "run", _boom)
    state = _state(_unit("f.c", "my_abs"))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.empty


def test_per_turn_budget_defers_excess_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(property_gate, "MAX_UNITS_PER_TURN", 1)
    units = []
    sources = []
    for i in range(3):
        source = tmp_path / f"f{i}.c"
        unit_id = f"{source}::fn{i}"
        _add_candidate(tmp_path, unit_id)
        units.append(_unit(str(source), f"fn{i}"))
        sources.append(source)
    state = _state(*units)

    calls = {"n": 0}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        payload = {
            "unit_id": "x",
            "counts": {
                "held": 1,
                "violated": 0,
                "unknown": 0,
                "error": 0,
                "skipped": 0,
            },
            "verdicts": [],
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert calls["n"] == 1  # bounded, not one call per candidate unit
    assert summary.checked == 1
    assert summary.deferred == 2
    assert not summary.empty  # deferred alone is still worth reporting
