"""Issue #39: a patched unit's sibling quoted includes must still resolve.

`quoted_include_kernel.c` is the abs/INT64_MIN bug from examples/abs.c, but
non-self-contained: it `#include`s a sibling header by a relative, quoted
path. Two ways `ProviderFixPort` + its caller can keep
`#include "quoted_include_helper.h"` resolving on re-verify:

- `work_dir=unit.parent`: the candidate lands beside the original, so the
  sibling header resolves for free, same as an in-place edit.
- a separate writable `work_dir` (the read-only-source case, PR #207 review):
  the candidate no longer shares a directory with the header, so the caller
  must carry the original directory into verification via `extra_flags=("-I",
  ...)`.

Skipped automatically when esbmc is not on PATH, mirroring the other
integration suites.
"""

from __future__ import annotations

import functools
import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify
from forseti.orchestrator import (
    LoopState,
    ProviderFixPort,
    RecordedFixProvider,
    run_loop,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _stage_unit(base: Path) -> Path:
    unit = base / "quoted_include_kernel.c"
    shutil.copy(FIXTURES / "quoted_include_kernel.c", unit)
    shutil.copy(FIXTURES / "quoted_include_helper.h", base / "quoted_include_helper.h")
    return unit


def test_fix_with_sibling_quoted_header_reverifies_without_parse_error(
    tmp_path: Path,
) -> None:
    unit = _stage_unit(tmp_path)
    provider = RecordedFixProvider({unit: FIXTURES / "quoted_include_kernel_fixed.c"})
    fix = ProviderFixPort(provider, original=unit, work_dir=unit.parent)

    run = run_loop(unit, verify=verify, fix=fix, unwind=1, max_iterations=2)

    # First pass: the original, in-place bug -> VIOLATED (never an Error: its
    # own directory always resolved the sibling header).
    assert isinstance(run.iterations[0].result, Violated)
    # Second pass: the applied candidate, written beside `unit` -> VERIFIED.
    # An Error here would mean the sibling header failed to resolve from the
    # candidate's write location -- the #39 regression.
    assert isinstance(run.iterations[-1].result, Verified)
    assert run.final_state is LoopState.DONE

    # The candidate landed beside the original, where the quoted include
    # resolves, not in some unrelated directory.
    applied = run.iterations[-1].source
    assert applied.parent == unit.parent


def test_fix_with_work_dir_elsewhere_resolves_via_include_flag(
    tmp_path: Path,
) -> None:
    # The read-only-source shape (PR #207 review on fix.py:172): the candidate
    # must land in a writable work_dir distinct from the original's directory,
    # and the sibling header must still resolve -- here via `-I`, not by
    # sharing a directory.
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    unit = _stage_unit(source_dir)
    work_dir = tmp_path / "work"
    provider = RecordedFixProvider({unit: FIXTURES / "quoted_include_kernel_fixed.c"})
    fix = ProviderFixPort(provider, original=unit, work_dir=work_dir)
    verify_with_include = functools.partial(
        verify, extra_flags=("-I", str(unit.parent))
    )

    run = run_loop(
        unit, verify=verify_with_include, fix=fix, unwind=1, max_iterations=2
    )

    assert isinstance(run.iterations[0].result, Violated)
    assert isinstance(run.iterations[-1].result, Verified)
    assert run.final_state is LoopState.DONE

    # The candidate landed in work_dir, not beside the (here, still writable,
    # but conceptually read-only) original.
    applied = run.iterations[-1].source
    assert applied.parent == work_dir
    assert applied.parent != unit.parent
