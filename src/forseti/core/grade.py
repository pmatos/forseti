"""Forseti Core's `grade` operation -- the harness-neutral kill-rate scorer face (#392).

`grade_source` is to `grading.grade_unit` what `check_source` is to
`check_properties`: it opens the `.forseti` `PropertyStore` (the one `propose` and
`check` use), runs the scorer with the real ESBMC bindings and writes each
`Grading` through `PropertyStore.grade`. The returned `GradeRun.to_dict()` is the
wire shape the CLI's `--json` and the MCP tool share. Per-(property, mutant)
results are appended to `<store_root>/events.jsonl` after the run.

Harnesses are written under a fresh `store_root/grade-work/<uuid>` directory,
never beside `source` (same reasoning as `check_source`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from forseti.core.check import DEFAULT_TIMEOUT_S, DEFAULT_UNWIND
from forseti.core.persistence import open_store
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.grading.equivalence import EquivalenceChecker
from forseti.grading.grade import GradeRun, MutantSource, grade_unit, record_trace
from forseti.orchestrator import HarnessWriterPort, VerifyPort
from forseti.precond import DEFAULT_MAX_LEN

_WORK_SUBDIR = "grade-work"


def grade_source(
    source: Path,
    *,
    function: str,
    store_root: Path = DEFAULT_STORE_ROOT,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    max_len: int | None = DEFAULT_MAX_LEN,
    max_mutants: int | None = None,
    seed: int = 0,
    mutants: MutantSource | None = None,
    equivalence: EquivalenceChecker | None = None,
    verify_for: Callable[[Path], VerifyPort] | None = None,
    render: HarnessWriterPort | None = None,
) -> GradeRun:
    """Grade `source`::`function`'s held, candidate properties against mutants.

    See `grading.grade_unit` for the scoring rules; `unwind`, `unwind_ladder`,
    `timeout_s`, `extra_flags`, `esbmc_bin` and `max_len` mean what they mean for
    `check_source` (the check runs on the real code and on every mutant with the
    same settings). A raw `sqlite3.Error` surfaces as `PropertyStoreError`. The
    last four arguments replace the real mutation, equivalence, ESBMC and harness
    bindings (hermetic tests).
    """
    with open_store(store_root) as store:
        run = grade_unit(
            source,
            function,
            store=store,
            work_dir=store_root / _WORK_SUBDIR / uuid.uuid4().hex,
            unwind=unwind,
            unwind_ladder=unwind_ladder,
            max_len=max_len,
            max_mutants=max_mutants,
            seed=seed,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            extra_flags=extra_flags,
            mutants=mutants,
            equivalence=equivalence,
            verify_for=verify_for,
            render=render,
        )
    record_trace(store_root, run)
    return run
