"""Hermetic tests for Core's `grade` face: `grade_source`, the CLI and the MCP tool.

The scorer itself is covered by `tests/grading/test_grade.py`; these pin the wiring
-- the store it is handed, the work directory, the trace, the CLI flags and exit
codes, the MCP payload -- with `grade_unit`/`grade_source` replaced by a recorder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from forseti.core import cli, mcp_server
from forseti.core import grade as core_grade
from forseti.core.mcp_server import build_server, grade_tool
from forseti.grading.grade import (
    GradeOutcome,
    GradeRun,
    MutantOutcome,
    MutantReading,
    PropertyGrade,
)
from forseti.orchestrator import PropertyOutcome
from forseti.properties import (
    Grading,
    GradingVerdict,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStoreError,
    Provenance,
)


def make_run(*, graded: bool = True) -> GradeRun:
    prop = Property(
        "p1",
        "u.c::f",
        PropertyKind.SEMANTIC,
        "result >= 0",
        PropertyStatus.CANDIDATE,
        Provenance("t", "1"),
    )
    readings = (
        MutantReading("op@1:1#0", "op", "`<` -> `<=` at line 1", MutantOutcome.KILLED),
        MutantReading(
            "op@2:1#0", "op", "`>` -> `>=` at line 2", MutantOutcome.SURVIVED
        ),
    )
    grade = PropertyGrade(
        prop,
        GradeOutcome.GRADED if graded else GradeOutcome.UNDECIDED,
        "killed 1/2; survived: `>` -> `>=` at line 2 [op@2:1#0]",
        PropertyOutcome.HELD,
        4,
        readings,
        Grading(GradingVerdict.HELD, 0.5, "r") if graded else None,
    )
    return GradeRun("u.c::f", (grade,), 4, (8, 16), 8, generated=2)


def test_grade_source_runs_the_scorer_on_the_store_and_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_grade_unit(source: Path, symbol: str, **kwargs: Any) -> GradeRun:
        seen.update(source=source, symbol=symbol, **kwargs)
        return make_run()

    monkeypatch.setattr(core_grade, "grade_unit", fake_grade_unit)
    root = tmp_path / ".forseti"

    run = core_grade.grade_source(
        tmp_path / "u.c",
        function="f",
        store_root=root,
        unwind=3,
        unwind_ladder=(5,),
        max_len=4,
        max_mutants=7,
        seed=2,
        extra_flags=("-DN",),
    )

    assert run.outcome == "graded"
    assert seen["symbol"] == "f"
    assert (seen["unwind"], seen["unwind_ladder"], seen["max_len"]) == (3, (5,), 4)
    assert (seen["max_mutants"], seen["seed"]) == (7, 2)
    assert seen["extra_flags"] == ("-DN",)
    assert seen["work_dir"].parent == root / "grade-work"
    assert hasattr(seen["store"], "grade")
    types = [
        json.loads(line)["type"]
        for line in (root / "events.jsonl").read_text().splitlines()
    ]
    assert types == [
        "grading.run",
        "grading.property",
        "grading.mutant",
        "grading.mutant",
    ]


def test_two_grade_calls_get_their_own_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs: list[Path] = []

    def fake_grade_unit(source: Path, symbol: str, **kwargs: Any) -> GradeRun:
        dirs.append(kwargs["work_dir"])
        return make_run()

    monkeypatch.setattr(core_grade, "grade_unit", fake_grade_unit)
    for _ in range(2):
        core_grade.grade_source(tmp_path / "u.c", function="f", store_root=tmp_path)

    assert dirs[0] != dirs[1]


def run_cli(
    monkeypatch: pytest.MonkeyPatch, run: GradeRun | Exception, *argv: str
) -> tuple[int, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def fake_grade_source(source: Path, **kwargs: Any) -> GradeRun:
        seen.update(source=source, **kwargs)
        if isinstance(run, Exception):
            raise run
        return run

    monkeypatch.setattr(cli, "grade_source", fake_grade_source)
    return cli.main(["grade", *argv]), seen


def test_cli_maps_flags_onto_grade_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, seen = run_cli(
        monkeypatch,
        make_run(),
        "u.c",
        "--function",
        "f",
        "--max-mutants",
        "5",
        "--seed",
        "3",
        "-k",
        "2",
        "--max-len",
        "6",
        "--store-root",
        "S",
        "--",
        "-DN",
    )

    assert code == 0
    assert seen["source"] == Path("u.c")
    assert seen["function"] == "f"
    assert seen["store_root"] == Path("S")
    assert (seen["max_mutants"], seen["seed"]) == (5, 3)
    assert (seen["unwind"], seen["max_len"]) == (2, 6)
    assert seen["extra_flags"] == ("-DN",)
    out = capsys.readouterr().out
    assert "kill rate 0.50" in out
    assert "survived: `>` -> `>=` at line 2" in out


def test_cli_json_is_the_grade_run_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_cli(monkeypatch, make_run(), "u.c", "--function", "f", "--json")

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == make_run().to_dict()
    counts = payload["properties"][0]["counts"]
    assert {"unknown", "error", "skipped"} <= counts.keys()


def test_cli_exit_code_is_nonzero_when_nothing_was_graded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_cli(monkeypatch, make_run(graded=False), "u.c", "--function", "f")

    assert code == 2
    assert "undecided" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error", [PropertyStoreError("db"), ValueError("bad"), OSError("io")]
)
def test_cli_reports_a_failed_run_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    code, _ = run_cli(monkeypatch, error, "u.c", "--function", "f")

    assert code == 1
    assert capsys.readouterr().err.startswith("forseti grade: ")


def test_cli_reports_a_mutation_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forseti.grading import MutationError

    code, _ = run_cli(
        monkeypatch, MutationError("esbmc binary not found"), "u.c", "--function", "f"
    )

    assert code == 1
    assert "esbmc binary not found" in capsys.readouterr().err


def test_mcp_registers_the_grade_tool() -> None:
    tools = asyncio.run(build_server().list_tools())
    grade = next(t for t in tools if t.name == "grade")
    props = grade.input_schema.get("properties", {})
    assert {"source", "function", "max_mutants", "seed", "max_len"} <= props.keys()


def test_mcp_grade_tool_returns_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_grade_source(source: Path, **kwargs: Any) -> GradeRun:
        seen.update(source=source, **kwargs)
        return make_run()

    monkeypatch.setattr(mcp_server, "grade_source", fake_grade_source)

    payload = grade_tool("u.c", "f", unwind_ladder=[8], max_mutants=3)

    assert payload == make_run().to_dict()
    assert seen["unwind_ladder"] == (8,)
    assert seen["max_mutants"] == 3
    assert seen["function"] == "f"
