"""End-to-end tracer bullet: drive run_loop on the real abs/INT_MIN kernel.

Closes the write -> verify -> fix loop on examples/abs.c with the real
forseti.esbmc.verify as the VerifyPort and a *recorded* fix (the repaired kernel
already in the repo, examples/abs_fixed.c) as the FixPort -- proving the #24
skeleton closes a real loop before we scale to the corpus.

Skipped automatically when esbmc is not on PATH, mirroring
tests/esbmc/test_verify_integration.py. The agent-driven fix is #5/#14; the
richer FixRequest/FixProvider contract is #28.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify
from forseti.orchestrator import LoopState, run_loop

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# INT64_MIN as the two's-complement bit string esbmc prints (top bit set, 63
# zeros) -- pinned by bit pattern, not the decimal literal, since esbmc 8.3.0
# renders it as "-9223372036854775807 - 1" (same pin as the esbmc suite).
_INT64_MIN_BITS = "1" + "0" * 63


class RecordedFix:
    """A FixPort returning the known-good fixed source, counting invocations."""

    def __init__(self, mapping: dict[Path, Path]) -> None:
        self._mapping = mapping
        self.calls = 0

    def __call__(self, source: Path, violated: Violated) -> Path:
        self.calls += 1
        return self._mapping[source]


def test_abs_int_min_loop_converges_to_verified() -> None:
    fix = RecordedFix({EXAMPLES / "abs.c": EXAMPLES / "abs_fixed.c"})

    run = run_loop(
        EXAMPLES / "abs.c", verify=verify, fix=fix, unwind=1, max_iterations=2
    )

    # Acceptance 1: converges to DONE/VERIFIED within a small bound.
    assert run.final_state is LoopState.DONE
    assert len(run.iterations) == 2
    assert isinstance(run.iterations[-1].result, Verified)

    # Acceptance 2a: the VIOLATED -> FIX -> VERIFIED transition is exercised.
    assert [it.state for it in run.iterations] == [LoopState.FIX, LoopState.DONE]
    assert fix.calls == 1

    # Acceptance 2b: the first-pass counterexample carries x = INT64_MIN.
    first = run.iterations[0].result
    assert isinstance(first, Violated)
    cex = first.counterexample
    assert cex is not None
    assert cex.violated_property.loc.file.endswith("abs.c")
    assert any(
        a.binary is not None and a.binary.replace(" ", "") == _INT64_MIN_BITS
        for a in cex.inputs
    )
