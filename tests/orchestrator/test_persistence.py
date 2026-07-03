"""Tests for run persistence: one JSONL record per run, keyed `path::symbol`."""

from __future__ import annotations

import json
from pathlib import Path

from forseti.esbmc import EsbmcResult, RunMeta, Verified, Violated
from forseti.orchestrator import ListSink, LoopRun, persist_run, run_loop

SRC = Path("kernel.c")


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "kernel.c", "--unwind", "8", "--no-unwinding-assertions"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


class FakeVerify:
    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        return self._results.pop(0)


class FakeFix:
    def __call__(self, source: Path, violated: Violated) -> Path:
        return source


def _abs_run() -> tuple[LoopRun, ListSink]:
    sink = ListSink()
    verify = FakeVerify([Violated(meta(), "[Counterexample]\n"), Verified(meta())])
    run = run_loop(SRC, verify=verify, fix=FakeFix(), unwind=8, sink=sink)
    return run, sink


def test_persist_writes_keyed_jsonl_record(tmp_path: Path) -> None:
    run, sink = _abs_run()

    dest = persist_run(
        run, unit_id="examples/abs.c::my_abs", events=sink.events, root=tmp_path
    )

    # A readable slug prefix, plus a stable hash suffix so distinct units never
    # collide (the bare slug is lossy: `a/b.c::f` and `a_b.c::f` slug alike).
    assert dest.parent == tmp_path / "runs"
    assert dest.name.startswith("examples_abs.c__my_abs-")
    assert dest.suffix == ".jsonl"
    assert dest.is_file()
    lines = dest.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["unit"] == "examples/abs.c::my_abs"
    assert isinstance(record["report"], dict)
    assert record["report"]["final_state"] == "done"
    assert isinstance(record["events"], list)
    assert record["events"][0]["type"] == "trigger.fired"


def test_persist_appends_second_run(tmp_path: Path) -> None:
    run, sink = _abs_run()
    persist_run(
        run, unit_id="examples/abs.c::my_abs", events=sink.events, root=tmp_path
    )
    dest = persist_run(
        run, unit_id="examples/abs.c::my_abs", events=sink.events, root=tmp_path
    )

    assert len(dest.read_text().splitlines()) == 2


def test_distinct_units_that_slug_alike_get_distinct_files(tmp_path: Path) -> None:
    # `a/b.c::f` and `a_b.c::f` have the same lossy slug; the hash suffix must
    # keep them in separate JSONL files so their histories never interleave.
    run, sink = _abs_run()
    d1 = persist_run(run, unit_id="a/b.c::f", events=sink.events, root=tmp_path)
    d2 = persist_run(run, unit_id="a_b.c::f", events=sink.events, root=tmp_path)

    assert d1 != d2
    # the same unit id is stable (idempotent path), so a re-persist appends.
    d1_again = persist_run(run, unit_id="a/b.c::f", events=sink.events, root=tmp_path)
    assert d1_again == d1
    assert len(d1.read_text().splitlines()) == 2
    assert len(d2.read_text().splitlines()) == 1
