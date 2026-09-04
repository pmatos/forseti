"""Tests for the Oh My Pi `tool_result` verify hook (`oh_my_pi/verify_hook.py`).

Hermetic except the `@needs_esbmc` end-to-end cases at the bottom, which
shell out to the real `forseti` console script (on `PATH`) and real `esbmc`
-- the same eligible-C-function fixture #213's other adapter tests use,
proving a held property passes silently, a violated one blocks with a
counterexample, and (mirroring the Codex adapter's own hermetic coverage)
UNKNOWN is reported, never silently passed.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forseti.adapters.oh_my_pi import verify_hook

needs_esbmc = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)
needs_forseti_cli = pytest.mark.skipif(
    shutil.which("forseti") is None, reason="forseti console script not on PATH"
)

_ABS_SLICE = (
    "#include <stdint.h>\n"
    "int64_t my_abs(int64_t x) {\n"
    "    return (x < 0) ? -x : x;\n"
    "}\n"
)


# --- _edited_sources -------------------------------------------------------


def test_edited_sources_write_tool_returns_path() -> None:
    assert verify_hook._edited_sources("write", {"path": "foo.c"}) == ["foo.c"]


def test_edited_sources_write_tool_filters_by_suffix() -> None:
    assert verify_hook._edited_sources("write", {"path": "README.md"}) == []


def test_edited_sources_write_tool_missing_path_is_empty() -> None:
    assert verify_hook._edited_sources("write", {}) == []


def test_edited_sources_edit_tool_parses_single_hashline_header() -> None:
    raw = "[foo.c#AB12]\nPUT 1.=1: int x;\n"
    assert verify_hook._edited_sources("edit", {"input": raw}) == ["foo.c"]


def test_edited_sources_edit_tool_parses_multiple_sections() -> None:
    raw = "[a.c#AB12]\nPUT 1.=1: int x;\n[b.c#CD34]\nPUT 2.=2: int y;\n"
    assert verify_hook._edited_sources("edit", {"input": raw}) == ["a.c", "b.c"]


def test_edited_sources_edit_tool_mv_supersedes_header_path() -> None:
    raw = "[old.c#AB12]\nMV new.c\n"
    assert verify_hook._edited_sources("edit", {"input": raw}) == ["new.c"]


def test_edited_sources_edit_tool_mv_only_applies_within_its_own_section() -> None:
    raw = "[a.c#AB12]\nMV renamed.c\n[b.c#CD34]\nPUT 1.=1: int z;\n"
    assert verify_hook._edited_sources("edit", {"input": raw}) == ["renamed.c", "b.c"]


def test_edited_sources_edit_tool_dedupes_and_filters_by_suffix() -> None:
    raw = (
        "[a.c#AB12]\nPUT 1.=1: x;\n"
        "[a.c#CD34]\nPUT 2.=2: y;\n"
        "[README.md#EF56]\nPUT 1.=1: z;\n"
    )
    assert verify_hook._edited_sources("edit", {"input": raw}) == ["a.c"]


def test_edited_sources_edit_tool_no_headers_is_empty() -> None:
    assert verify_hook._edited_sources("edit", {"input": "just some text"}) == []


def test_edited_sources_unknown_tool_is_empty() -> None:
    assert verify_hook._edited_sources("bash", {"command": "rm -rf /"}) == []


def test_edited_sources_edit_tool_non_string_input_is_empty() -> None:
    assert verify_hook._edited_sources("edit", {"input": None}) == []


# --- _list_functions / _semantic_check (fake subprocess) -------------------


def test_list_functions_parses_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"units": [{"function": "f"}, {"function": "g"}]}),
            stderr="",
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    assert verify_hook._list_functions("f.c", "/cwd") == ["f", "g"]


def test_list_functions_returns_none_on_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError("forseti not found")

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    assert verify_hook._list_functions("f.c", "/cwd") is None


def test_list_functions_returns_none_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="oops", stderr=""
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    assert verify_hook._list_functions("f.c", "/cwd") is None


def test_list_functions_returns_none_when_units_not_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"units": "not-a-list"}), stderr=""
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    assert verify_hook._list_functions("f.c", "/cwd") is None


def test_semantic_check_returns_error_when_outcome_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps({}), stderr="unexpected failure"
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "error"
    assert "unexpected failure" in evidence


def test_semantic_check_extracts_outcome_and_counterexample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "outcome": "violated",
        "check": {
            "verdicts": [
                {"outcome": "violated", "result": {"raw_counterexample": "x == 0"}}
            ]
        },
    }

    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "violated"
    assert evidence == "x == 0"


def test_semantic_check_held_has_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"outcome": "held", "check": {"verdicts": [{"outcome": "held"}]}}

    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "held"
    assert evidence == ""


def test_semantic_check_falls_back_to_skip_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "outcome": "unknown",
        "check": {
            "verdicts": [
                {"outcome": "skipped", "skip_reason": "reachability, deferred"}
            ]
        },
    }

    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "unknown"
    assert evidence == "reachability, deferred"


def test_semantic_check_returns_error_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="not json", stderr="boom"
        )

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "error"
    assert evidence == "boom"


def test_semantic_check_returns_error_on_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError("boom")

    monkeypatch.setattr(verify_hook.subprocess, "run", fake_run)
    outcome, evidence = verify_hook._semantic_check("f.c", "my_fn", "/cwd")
    assert outcome == "error"
    assert "boom" in evidence


# --- main() ------------------------------------------------------------


def test_main_ignores_non_write_edit_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"toolName": "bash"})))
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_ignores_error_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": True,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_tolerates_unparseable_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert verify_hook.main() == 0


def test_main_ignores_non_dict_top_level_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["not", "a", "dict"])))
    assert verify_hook.main() == 0


def test_main_ignores_event_missing_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = {"toolName": "write", "input": {"path": "f.c"}, "isError": False}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert verify_hook.main() == 0


def test_main_ignores_when_nothing_matches_source_suffixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    event = {
        "toolName": "write",
        "input": {"path": "README.md"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_skips_when_no_store_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    def boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not shell out when no store exists")

    monkeypatch.setattr(verify_hook, "_list_functions", boom)
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def _seed_store(tmp_path: Path) -> None:
    (tmp_path / ".forseti").mkdir()
    (tmp_path / ".forseti" / "forseti.db").write_bytes(b"")


def test_main_path_exists_but_no_functions_found_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_list_functions` returning `[]` (genuinely no functions) is a pass."""
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: [])

    def boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not check a unit that was never discovered")

    monkeypatch.setattr(verify_hook, "_semantic_check", boom)
    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""

    from forseti.core.events import events_path

    assert not events_path(tmp_path / ".forseti").exists()


def test_main_list_units_failure_is_unresolved_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_list_functions` returning `None` (enumeration failed) must be surfaced.

    `None` means `list-units` itself could not run or produce a usable
    payload -- distinct from `[]` (file parsed, no functions). Conflating the
    two would let a tooling failure silently read as a pass.
    """
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: None)

    def boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not check a unit that was never discovered")

    monkeypatch.setattr(verify_hook, "_semantic_check", boom)
    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "decision" not in payload
    assert "f.c" in payload["systemMessage"]
    assert "list-units-failed" in payload["systemMessage"]

    from forseti.core.events import events_path

    events = [
        json.loads(line)
        for line in events_path(tmp_path / ".forseti").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["decision"] == "unresolved"


def test_main_blocks_on_violated_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: ["f"])
    monkeypatch.setattr(
        verify_hook, "_semantic_check", lambda path, fn, cwd: ("violated", "x == 0")
    )

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "f.c::f" in payload["reason"]
    assert "x == 0" in payload["reason"]


def test_main_reports_violated_plus_inconclusive_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(
        verify_hook, "_list_functions", lambda path, cwd: ["bad", "iffy"]
    )

    def fake_check(path: str, fn: str, cwd: str) -> tuple[str, str]:
        return ("violated", "x == 0") if fn == "bad" else ("unknown", "")

    monkeypatch.setattr(verify_hook, "_semantic_check", fake_check)

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "f.c::bad" in payload["reason"]
    assert "Also inconclusive" in payload["reason"]
    assert "f.c::iffy" in payload["reason"]


def test_main_surfaces_unknown_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: ["f"])
    monkeypatch.setattr(
        verify_hook, "_semantic_check", lambda path, fn, cwd: ("unknown", "")
    )

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "decision" not in payload
    assert "f.c::f" in payload["systemMessage"]
    assert "unknown" in payload["systemMessage"]


def test_main_allows_held_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: ["f"])
    monkeypatch.setattr(
        verify_hook, "_semantic_check", lambda path, fn, cwd: ("held", "")
    )

    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_empty_outcome_is_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`empty` (nothing stored for this unit) must not read as `unresolved`."""
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: ["f"])
    monkeypatch.setattr(
        verify_hook, "_semantic_check", lambda path, fn, cwd: ("empty", "")
    )

    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""

    from forseti.core.events import events_path

    assert not events_path(tmp_path / ".forseti").exists()


def test_main_no_edited_path_remains_is_unresolved_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "does-not-exist.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert "decision" not in payload
    assert "does-not-exist.c" in payload["systemMessage"]

    from forseti.core.events import events_path

    events = [
        json.loads(line)
        for line in events_path(tmp_path / ".forseti").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["decision"] == "unresolved"


def test_main_records_canonical_gate_decision_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.c").write_text("int f(void) { return 0; }\n")
    _seed_store(tmp_path)
    event = {
        "toolName": "write",
        "input": {"path": "f.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(verify_hook, "_list_functions", lambda path, cwd: ["f"])
    monkeypatch.setattr(
        verify_hook, "_semantic_check", lambda path, fn, cwd: ("held", "")
    )

    assert verify_hook.main() == 0

    from forseti.core.events import events_path

    events = [
        json.loads(line)
        for line in events_path(tmp_path / ".forseti").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["type"] == "gate.decision"
    assert events[0]["harness"] == "oh-my-pi"
    assert events[0]["adapter"] == "oh-my-pi-tool-result"
    assert events[0]["decision"] == "pass"
    assert events[0]["unit_ids"] == ["f.c::f"]


# --- end-to-end, real forseti + real esbmc ---------------------------------


@needs_esbmc
@needs_forseti_cli
def test_main_end_to_end_held_property_passes_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    # `source` relative to `cwd`, not the absolute `str(source)`: the property
    # store keys a candidate by whatever path string it's given verbatim, and
    # the hook below looks it up under the same relative "abs_unit.c" the
    # `write` event names -- an absolute path here would store the candidate
    # under a different unit_id, so the hook would silently find nothing to
    # check and this test would pass vacuously (see the `.forseti/events.jsonl`
    # assertion below, which fails loudly if that regresses).
    subprocess.run(
        [
            "forseti",
            "submit-property",
            "abs_unit.c",
            "--function",
            "my_abs",
            "--expression",
            "result >= 0",
            "--domain",
            "x > INT64_MIN",
            "--provider",
            "test",
            "--model",
            "test-1",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    event = {
        "toolName": "write",
        "input": {"path": "abs_unit.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.chdir(tmp_path)

    assert verify_hook.main() == 0
    assert capsys.readouterr().out == ""

    from forseti.core.events import events_path

    events = [
        json.loads(line)
        for line in events_path(tmp_path / ".forseti").read_text().splitlines()
    ]
    gate_events = [e for e in events if e["type"] == "gate.decision"]
    assert len(gate_events) == 1
    assert gate_events[0]["decision"] == "pass"
    assert gate_events[0]["unit_ids"] == ["abs_unit.c::my_abs"]


@needs_esbmc
@needs_forseti_cli
def test_main_end_to_end_violated_property_blocks_with_counterexample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    subprocess.run(
        [
            "forseti",
            "submit-property",
            "abs_unit.c",
            "--function",
            "my_abs",
            "--expression",
            "result < 0",
            "--provider",
            "test",
            "--model",
            "test-1",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    event = {
        "toolName": "write",
        "input": {"path": "abs_unit.c"},
        "isError": False,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.chdir(tmp_path)

    assert verify_hook.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "abs_unit.c::my_abs" in payload["reason"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
