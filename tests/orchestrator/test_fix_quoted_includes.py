"""Issue #39: a patched unit's sibling quoted includes must still resolve.

`quoted_include_kernel.c` is the abs/INT64_MIN bug from examples/abs.c, but
non-self-contained: it `#include`s a sibling header by a relative, quoted
path. Before this fix, `ProviderFixPort` wrote the candidate under an
unrelated `work_dir`, where `#include "quoted_include_helper.h"` can't
resolve — the real-esbmc re-verify parse-errors even though the original
source verifies. Skipped automatically when esbmc is not on PATH, mirroring
the other integration suites.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Error, Verified, Violated, verify
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


def test_fix_with_sibling_quoted_header_reverifies_without_parse_error(
    tmp_path: Path,
) -> None:
    unit = tmp_path / "quoted_include_kernel.c"
    shutil.copy(FIXTURES / "quoted_include_kernel.c", unit)
    shutil.copy(
        FIXTURES / "quoted_include_helper.h", tmp_path / "quoted_include_helper.h"
    )
    provider = RecordedFixProvider({unit: FIXTURES / "quoted_include_kernel_fixed.c"})
    fix = ProviderFixPort(provider)

    run = run_loop(unit, verify=verify, fix=fix, unwind=1, max_iterations=2)

    # First pass: the original, in-place bug -> VIOLATED (never an Error: its
    # own directory always resolved the sibling header).
    assert isinstance(run.iterations[0].result, Violated)
    # Second pass: the applied candidate, written beside `unit` -> VERIFIED,
    # not Error. An Error here would mean the sibling header failed to
    # resolve from the candidate's write location -- the #39 regression.
    assert not isinstance(run.iterations[-1].result, Error)
    assert isinstance(run.iterations[-1].result, Verified)
    assert run.final_state is LoopState.DONE

    # The candidate landed beside the original, where the quoted include
    # resolves, not in some unrelated directory.
    applied = run.iterations[-1].source
    assert applied.parent == unit.parent
