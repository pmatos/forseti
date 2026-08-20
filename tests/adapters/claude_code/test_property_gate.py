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
    _add_property(root, unit_id, expression, status=PropertyStatus.CANDIDATE)


def _add_property(
    root: Path,
    unit_id: str,
    expression: str,
    *,
    status: PropertyStatus,
) -> None:
    store = PropertyStore.open(root / ".forseti")
    store.add(
        Property(
            property_id=make_property_id(unit_id, PropertyKind.SEMANTIC, expression),
            unit_id=unit_id,
            kind=PropertyKind.SEMANTIC,
            expression=expression,
            status=status,
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


def test_malformed_verified_unit_missing_file_or_function_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `state["units"]` entry missing `file`/`function` (a hand-corrupted or
    partially-written gate_state.json) must not crash the candidate scan --
    it just can't be looked up in the property store, so it's skipped."""
    PropertyStore.open(tmp_path / ".forseti").close()  # creates forseti.db, no rows

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("subprocess.run must not run for a malformed unit")

    monkeypatch.setattr(subprocess, "run", _boom)
    state = {
        "units": {
            "bad": {"verdict": "verified", "k": 4}  # no file/function
        }
    }

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


def test_forseti_build_flags_reach_the_check_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety gate forwards `FORSETI_BUILD_FLAGS` to its own verify/
    list-units calls; this subprocess must get the same flags after `--`, or
    the semantic harness can compile a different preprocessor branch than the
    safety gate verified (issue #95 review)."""
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Iinclude -DNDEBUG")

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess, "run", _fake_run(_payload(unit_id, "held"), 0, captured)
    )

    property_gate.semantic_check_summary(str(tmp_path), state)

    argv = captured["argv"]
    assert argv[-3:] == ["--", "-Iinclude", "-DNDEBUG"]


def test_no_build_flags_adds_no_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess, "run", _fake_run(_payload(unit_id, "held"), 0, captured)
    )

    property_gate.semantic_check_summary(str(tmp_path), state)

    assert "--" not in captured["argv"]


def test_malformed_build_flags_is_best_effort_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "'-I/opt/my sdk")  # unbalanced quote

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must never reach a check call with bad build flags")

    monkeypatch.setattr(subprocess, "run", _boom)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0  # best-effort: dropped, not raised


def test_non_json_stdout_is_best_effort_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="not json")

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.failed == 1


def test_malformed_payload_shape_is_best_effort_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        # Valid JSON, but not the {"verdicts": [...]} shape `forseti check
        # --json` promises.
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"oops": True}))

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.failed == 1


def test_non_dict_verdict_entries_are_skipped_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        payload = {"unit_id": unit_id, "counts": {}, "verdicts": ["not-a-dict"]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 1  # the unit itself was checked...
    assert summary.violations == ()  # ...but the malformed entry is ignored
    assert summary.unresolved == ()


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


def test_skipped_outcomes_are_reported_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachability-kind property `forseti check` defers (SKIPPED, ADR-0009
    D2) is not a failure, but it also isn't an actual check -- a unit whose
    only stored candidate is reachability-kind must not read as a quiet,
    fully-covered pass (issue #95 review).
    """
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    monkeypatch.setattr(subprocess, "run", _fake_run(_payload(unit_id, "skipped")))

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.violations == ()
    assert summary.unresolved == ()
    assert len(summary.skipped) == 1
    assert summary.skipped[0]["outcome"] == "skipped"
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
    # A unit that never produced any verdict at all is never silently
    # indistinguishable from "nothing to report" (issue #95 review).
    assert summary.failed == 1
    assert not summary.empty


def test_subprocess_timeout_scales_with_stored_property_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`forseti check` checks every stored property for the unit in one
    process, but `--timeout` is esbmc's *per-attempt* budget -- a unit with
    2+ properties needs more than one attempt's worth of wall clock or a
    slow-but-legitimate check is dropped as a false timeout (issue #95
    review). Each attempt also gets `VERIFY_GRACE_S` -- the same wall-clock
    slack `verify` itself permits per esbmc attempt (`esbmc/runner.py`) --
    or a legitimately-running child could be killed by this parent before it
    could time out on its own. Tests the unclamped formula specifically --
    `MAX_TOTAL_CHECK_S` is raised out of the way; its own clamping is a
    separate concern covered by
    `test_per_unit_timeout_is_clamped_to_the_remaining_aggregate_budget`."""
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 1000.0)
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id, expression="result >= 0")
    _add_candidate(tmp_path, unit_id, expression="result >= -1")
    _add_candidate(tmp_path, unit_id, expression="result >= -2")
    state = _state(_unit(str(source), "my_abs"))

    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_payload(unit_id, "held"))
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    property_gate.semantic_check_summary(str(tmp_path), state)

    # 3 stored properties -> 3x (per-attempt CHECK_TIMEOUT_S + VERIFY_GRACE_S),
    # plus margin.
    expected = (
        3 * (property_gate.CHECK_TIMEOUT_S + property_gate.VERIFY_GRACE_S)
        + property_gate._SUBPROCESS_MARGIN_S
    )
    assert captured["timeout"] == expected


def test_subprocess_timeout_counts_graded_properties_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`forseti check` checks every non-terminal property, not just CANDIDATE
    (`properties.CHECKABLE_STATUSES`) -- a unit with 1 CANDIDATE + 2
    already-GRADED properties runs 3 esbmc attempts, so sizing the subprocess
    timeout off the CANDIDATE count alone would under-budget it (issue #95
    review). `MAX_TOTAL_CHECK_S` is raised out of the way, same reasoning as
    `test_subprocess_timeout_scales_with_stored_property_count`."""
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 1000.0)
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id, expression="result >= 0")
    _add_property(tmp_path, unit_id, "result >= -1", status=PropertyStatus.GRADED)
    _add_property(tmp_path, unit_id, "result >= -2", status=PropertyStatus.GRADED)
    state = _state(_unit(str(source), "my_abs"))

    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_payload(unit_id, "held"))
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    property_gate.semantic_check_summary(str(tmp_path), state)

    expected = (
        3 * (property_gate.CHECK_TIMEOUT_S + property_gate.VERIFY_GRACE_S)
        + property_gate._SUBPROCESS_MARGIN_S
    )
    assert captured["timeout"] == expected


def test_unit_with_only_graded_properties_is_not_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit with GRADED (but no fresh CANDIDATE) properties has nothing new
    for this opt-in surface to report -- `_units_with_candidates` gates
    presence on CANDIDATE specifically, even though the timeout sizing counts
    every non-terminal status (issue #95 review)."""
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_property(tmp_path, unit_id, "result >= 0", status=PropertyStatus.GRADED)
    state = _state(_unit(str(source), "my_abs"))

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not check a unit with only GRADED properties")

    monkeypatch.setattr(subprocess, "run", _boom)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.empty


def test_aggregate_deadline_defers_remaining_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-turn unit budget alone does not bound wall clock -- three units
    each near their own per-unit timeout can still blow past the Stop hook's
    total budget (issue #95 review). An aggregate cap must defer whatever is
    left rather than let the loop run unbounded."""
    monkeypatch.setattr(property_gate, "MAX_UNITS_PER_TURN", 3)
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 0.0)
    units = []
    for i in range(3):
        source = tmp_path / f"f{i}.c"
        unit_id = f"{source}::fn{i}"
        _add_candidate(tmp_path, unit_id)
        units.append(_unit(str(source), f"fn{i}"))
    state = _state(*units)

    calls = {"n": 0}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_payload("x", "held"))
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert calls["n"] == 0  # the deadline was already elapsed before the first call
    assert summary.checked == 0
    assert summary.deferred == 3
    assert not summary.empty


def test_per_unit_timeout_is_clamped_to_the_remaining_aggregate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single unit with enough stored properties can need more wall clock
    than the *whole* aggregate ceiling by itself -- the between-units deadline
    check alone cannot bound that, since it only runs before a unit starts,
    not while sizing that unit's own subprocess timeout (issue #95 review,
    thread a7NSB). The per-unit formula must be clamped to whatever budget
    remains."""
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 40.0)
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    for i in range(6):  # 6 properties -> 6*(20+5)+10=160s unclamped, way over 40s
        _add_candidate(tmp_path, unit_id, expression=f"result >= -{i}")
    state = _state(_unit(str(source), "my_abs"))

    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_payload(unit_id, "held"))
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    property_gate.semantic_check_summary(str(tmp_path), state)

    unclamped = (
        6 * (property_gate.CHECK_TIMEOUT_S + property_gate.VERIFY_GRACE_S)
        + property_gate._SUBPROCESS_MARGIN_S
    )
    max_total = property_gate.MAX_TOTAL_CHECK_S
    assert unclamped > max_total  # the case actually exercises clamping
    assert captured["timeout"] <= max_total
    assert captured["timeout"] == pytest.approx(max_total, abs=1.0)


def test_clamped_timeout_is_deferred_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit whose subprocess timeout is clamped below what `n_props` needs
    (`test_per_unit_timeout_is_clamped_to_the_remaining_aggregate_budget`) can
    legitimately run out of that clamped time -- `deferred` ("ran out of turn
    budget") is the honest bucket, not `failed` ("this check itself broke"),
    the same distinction `_MIN_ATTEMPT_BUDGET_S` already draws for a unit
    clamped so hard it never starts at all (issue #95 review)."""
    monkeypatch.setattr(property_gate, "MAX_TOTAL_CHECK_S", 40.0)
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    for i in range(6):  # 6 properties -> clamped well below their natural timeout
        _add_candidate(tmp_path, unit_id, expression=f"result >= -{i}")
    state = _state(_unit(str(source), "my_abs"))

    def timing_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timing_out)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.failed == 0
    assert summary.deferred == 1
    assert not summary.empty


def test_unclamped_timeout_is_still_failed_not_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-property unit's subprocess timeout is not clamped by the
    aggregate budget (`test_tooling_failure_is_best_effort_not_raised`'s own
    scenario) -- a `TimeoutExpired` there is a genuine tooling problem, not a
    budget one, so it must stay `failed`."""
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def timing_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timing_out)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.deferred == 0
    assert summary.failed == 1


def test_negative_max_units_per_turn_is_rejected_not_slice_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #95 review: `MAX_UNITS_PER_TURN` used to feed a bare `env_int` with
    # no lower bound straight into `candidates[:MAX_UNITS_PER_TURN]` -- a
    # negative value silently triggers Python's negative-slice semantics
    # (drops the *last* N, not "process none") instead of erroring. The
    # `minimum=0` bound on `env_int` now catches it at the source.
    monkeypatch.setenv("FORSETI_PROPERTY_MAX_UNITS", "-1")
    from forseti.adapters.claude_code import forseti_gate as gate

    assert gate.env_int("FORSETI_PROPERTY_MAX_UNITS", "3", minimum=0) == 3
    assert "must be >= 0" in gate.env_config_errors()[-1]


def test_min_attempt_budget_exceeding_total_records_an_env_config_error() -> None:
    # Issue #95 review: raising FORSETI_PROPERTY_CHECK_TIMEOUT_S without a
    # matching raise to FORSETI_PROPERTY_MAX_TOTAL_CHECK_S would otherwise
    # silently defer every unit, every turn, forever -- neither env var alone
    # is out of range, so `env_int`/`env_float`'s own `minimum=` bound can't
    # catch it; the cross-check must.
    from forseti.adapters.claude_code import forseti_gate as gate

    property_gate._check_min_attempt_budget_fits(65.0, 60.0)
    assert "FORSETI_PROPERTY_CHECK_TIMEOUT_S" in gate.env_config_errors()[-1]
    assert "65" in gate.env_config_errors()[-1]


def test_min_attempt_budget_within_total_records_no_error() -> None:
    from forseti.adapters.claude_code import forseti_gate as gate

    property_gate._check_min_attempt_budget_fits(35.0, 60.0)
    assert gate.env_config_errors() == ()


def test_unit_is_deferred_not_started_when_budget_cannot_cover_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting a unit with a remaining budget too small for even one honest
    esbmc attempt would clamp its subprocess timeout down to something that
    always looks like a tooling failure -- `deferred` ("ran out of turn
    budget") is the honest bucket, not `failed` ("this check itself broke")
    (issue #95 review)."""
    monkeypatch.setattr(
        property_gate, "MAX_TOTAL_CHECK_S", property_gate._MIN_ATTEMPT_BUDGET_S / 2
    )
    source = tmp_path / "f.c"
    unit_id = f"{source}::my_abs"
    _add_candidate(tmp_path, unit_id)
    state = _state(_unit(str(source), "my_abs"))

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not start a unit the remaining budget can't cover")

    monkeypatch.setattr(subprocess, "run", _boom)

    summary = property_gate.semantic_check_summary(str(tmp_path), state)

    assert summary.checked == 0
    assert summary.failed == 0
    assert summary.deferred == 1
    assert not summary.empty


def test_failed_units_are_surfaced_in_the_stop_message() -> None:
    from forseti.adapters.claude_code import stop_gate

    summary = property_gate.SemanticCheckSummary((), checked=0, deferred=0, failed=2)

    message = stop_gate._semantic_message(summary)

    assert "2 unit(s)" in message
    assert "could not be checked" in message


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


def test_per_turn_selection_is_deterministic_not_insertion_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking never mutates a property's stored status, so an insertion-
    order-dependent prefix would select the exact same units forever on a
    project with more candidates than the budget -- worse, it would depend on
    an incidental `dict` ordering nobody documents (issue #95 review). Insert
    `state["units"]` in reverse order and confirm the selection doesn't
    follow it."""
    monkeypatch.setattr(property_gate, "MAX_UNITS_PER_TURN", 1)
    units = []
    for i in reversed(range(3)):  # inserted z, y, x -- NOT sorted order
        source = tmp_path / f"f{i}.c"
        unit_id = f"{source}::fn{i}"
        _add_candidate(tmp_path, unit_id)
        units.append(_unit(str(source), f"fn{i}"))
    state = _state(*units)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_run(_payload("x", "held"), 0, captured),
    )

    property_gate.semantic_check_summary(str(tmp_path), state)

    # f0.c sorts first regardless of state["units"]' insertion order.
    assert captured["argv"][captured["argv"].index("check") + 1] == str(
        tmp_path / "f0.c"
    )
