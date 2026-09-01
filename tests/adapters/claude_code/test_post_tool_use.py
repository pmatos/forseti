"""Tests for the PostToolUse verify hook: direct Write/Edit/MultiEdit gating.

Companion to `test_out_of_band.py` (which covers `post_bash`/`stop_gate`'s
out-of-band-edit path); this drives `post_tool_use.main()` itself the same
way -- a stdin payload, real `gate.verify_and_record`, with `verify_function`
(and, where a real call would shell to ESBMC, `extract_function_defs`)
monkeypatched so the verdict is deterministic and the test stays fast.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from forseti.adapters.claude_code import event_log, post_tool_use
from forseti.adapters.claude_code import forseti_gate as gate


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from any FORSETI_GATE_*/CLAUDE_PROJECT_DIR set in the outer env."""
    for var in ("FORSETI_GATE_INCLUDE", "FORSETI_GATE_EXCLUDE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)


def _run(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch, **tool_input: object
) -> int:
    """Drive `post_tool_use.main()` with a stdin payload pointing at `project_dir`."""
    body = {"cwd": str(project_dir), "tool_name": "Edit", "tool_input": tool_input}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    return post_tool_use.main()


def _enumerate_one_unit(monkeypatch: pytest.MonkeyPatch, name: str = "f") -> None:
    """Enumerate one non-pointer unit without shelling out to `forseti list-units`."""
    monkeypatch.setattr(
        gate,
        "extract_function_defs",
        lambda file_path, *, project_dir, content=None: [
            gate.FuncDef(name, takes_pointer=False)
        ],
    )


def test_missing_file_path_is_a_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(tmp_path, monkeypatch) == 0


def test_non_c_file_is_a_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("not C\n")
    assert _run(tmp_path, monkeypatch, file_path=str(readme)) == 0


def test_nonexistent_c_file_is_a_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file reported as edited but already gone (e.g. deleted by a later step in
    # the same turn) has nothing on disk to verify -- pass, don't error.
    assert _run(tmp_path, monkeypatch, file_path="ghost.c") == 0


def test_malformed_env_var_blocks_before_verifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #95 review: `verify_and_record` reads `gate.DEFAULT_K`/
    # `VERIFY_TIMEOUT_S` (env_int/env_float-backed) -- a malformed value must
    # block here, before this hook could silently verify and durably record a
    # verdict under the wrong value (the same fail-closed check
    # `stop_gate.main()` opens with).
    src = tmp_path / "f.c"
    src.write_text("int f(void){return 0;}\n")
    monkeypatch.setenv("FORSETI_UNWIND", "not-a-number")
    gate.env_int("FORSETI_UNWIND", "1")  # exercise the real parse path

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not verify with a malformed env config")

    monkeypatch.setattr(gate, "verify_and_record", _boom)

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 2
    err = capsys.readouterr().err
    assert "FORSETI_UNWIND" in err
    assert "not-a-number" in err


def test_verified_c_file_prints_the_kernel_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "clean.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict("clean.c::f", "clean.c", "f", "verified", k)
        ),
    )

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 0
    out = capsys.readouterr().out
    assert "VERIFIED up to k" in out
    assert "clean.c::f" in out

    events = event_log.read_events(str(tmp_path))
    # A trailing canonical `gate.decision` event (core/events.py, #213) shares
    # the same events.jsonl as the adapter-local trace -- see this module's
    # own docstring.
    assert [e["type"] for e in events] == [
        event_log.EDIT,
        event_log.VERIFY,
        event_log.GATE,
        "gate.decision",
    ]
    assert events[-2]["decision"] == "pass"
    assert events[-1]["decision"] == "pass"


def test_violated_c_file_prints_counterexample_to_stderr_and_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "buggy.c"
    src.write_text("int f(void){return 1 / 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict(
                "buggy.c::f",
                "buggy.c",
                "f",
                "violated",
                k,
                counterexample="division by zero",
            )
        ),
    )

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 2
    err = capsys.readouterr().err
    assert "buggy.c::f" in err
    assert "VIOLATED" in err
    assert "division by zero" in err

    events = event_log.read_events(str(tmp_path))
    # A trailing canonical `gate.decision` event (core/events.py, #213) shares
    # the same events.jsonl as the adapter-local trace.
    assert events[-1]["type"] == "gate.decision"
    assert events[-1]["decision"] == "block"
    assert events[-2]["type"] == event_log.GATE
    assert events[-2]["decision"] == "block"
    assert events[-2]["exit_code"] == 2


def test_pointer_unit_is_reported_loudly_and_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pointer/array parameter unit is NEEDS_CONTRACT (issue #122): recorded
    # without ever shelling to ESBMC (mirrors
    # test_verify_and_record_stamps_scanned_and_dedups), reported, not blocked.
    src = tmp_path / "ptr.c"
    src.write_text("int f(int *p){return *p;}\n")

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 0
    out = capsys.readouterr().out
    assert "NOT gated" in out
    assert "ptr.c::f" in out


def test_c_file_with_no_functions_is_a_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "empty.c"
    src.write_text("// no functions here\n")
    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 0

    events = event_log.read_events(str(tmp_path))
    # An empty verification batch is still a decision this gate made -- record
    # the canonical `gate.decision` event so the cross-harness trace isn't
    # missing this allowing outcome (issue #252 review).
    assert events[-1]["type"] == "gate.decision"
    assert events[-1]["decision"] == "pass"
    assert events[-1]["unit_ids"] == []


def test_failure_without_a_counterexample_prints_its_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An UNKNOWN verdict (e.g. an ESBMC timeout) has `detail`, not a
    # counterexample -- the `elif` branch of the failure report.
    src = tmp_path / "slow.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict(
                "slow.c::f", "slow.c", "f", "unknown", k, detail="timeout after 110s"
            )
        ),
    )

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 2
    err = capsys.readouterr().err
    assert "timeout after 110s" in err
    assert "Counterexample" not in err


def test_failure_with_neither_counterexample_nor_detail_still_names_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A bare ERROR verdict (e.g. an ESBMC invocation failure with no parseable
    # detail) still gets a line naming the unit, even with nothing more to add.
    src = tmp_path / "broken.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict("broken.c::f", "broken.c", "f", "error", k)
        ),
    )

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 2
    err = capsys.readouterr().err
    assert "broken.c::f" in err
    assert "Counterexample" not in err


def test_mixed_failure_and_needs_contract_reports_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A file with one failing scalar unit and one pointer unit: the block still
    # names the pointer unit's NEEDS_CONTRACT note alongside the real failure.
    src = tmp_path / "mixed.c"
    src.write_text("int f(void){return 1 / 0;}\nint g(int *p){return *p;}\n")
    monkeypatch.setattr(
        gate,
        "extract_function_defs",
        lambda file_path, *, project_dir, content=None: [
            gate.FuncDef("f", takes_pointer=False),
            gate.FuncDef("g", takes_pointer=True),
        ],
    )
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict(
                "mixed.c::f",
                "mixed.c",
                "f",
                "violated",
                k,
                counterexample="division by zero",
            )
        ),
    )

    assert _run(tmp_path, monkeypatch, file_path=str(src)) == 2
    err = capsys.readouterr().err
    assert "mixed.c::f" in err
    assert "NOT gated" in err
    assert "mixed.c::g" in err
