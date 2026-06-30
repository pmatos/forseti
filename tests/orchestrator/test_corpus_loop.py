"""The P1 epic exit gate: the loop closes to VERIFIED across the kernel corpus.

For each corpus kernel we drive the real `run_loop` from the staged-bug source
(`*_bug.c`, VIOLATED) through a *deterministic* recorded fix to the clean kernel
(`*.c`, VERIFIED) at its documented bound — no human, no LLM, no network. The
fix provider is `RecordedFixProvider` (a canned bug->clean mapping), so the gate
is reproducible. `abs.c`/`abs_fixed.c` is the inverted legacy pair.

UNKNOWN handling (#4) is intentionally *not* exercised here: at the documented k
the clean corpus goes VIOLATED->VERIFIED with no UNKNOWN, and a real-esbmc
UNKNOWN only arises from timeout/memout (non-deterministic) -- under
`--no-unwinding-assertions` a too-small k yields a (vacuous) VERIFIED, not
UNKNOWN (examples/README.md). The k-escalation policy is covered deterministically
by the fake-driven `test_loop.py::test_unknown_escalates_then_converges` /
`::test_unknown_exhausts_ladder_then_reports` and `test_telemetry.py`. This gate
asserts the complementary property: give-up never fires on the clean corpus.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import EsbmcResult, Verified, Violated, verify
from forseti.orchestrator import (
    ListSink,
    LoopRun,
    LoopState,
    ProviderFixPort,
    RecordedFixProvider,
    run_loop,
    transcript_for,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# (bug source, clean source, documented k, unit id). k + units from
# examples/README.md; the bare name is the clean kernel, `*_bug` the staged
# defect (abs.c/abs_fixed.c is the inverted legacy pair).
CORPUS = [
    ("ring_buffer_bug.c", "ring_buffer.c", 6, "examples/ring_buffer.c::rb_push"),
    ("merge_sort_bug.c", "merge_sort.c", 5, "examples/merge_sort.c::msort"),
    ("utf8_decode_bug.c", "utf8_decode.c", 4, "examples/utf8_decode.c::utf8_decode"),
    ("murmurhash_bug.c", "murmurhash.c", 8, "examples/murmurhash.c::murmur3_32"),
    ("abs.c", "abs_fixed.c", 1, "examples/abs.c::my_abs"),
]


def _verify(source: Path, *, unwind: int) -> EsbmcResult:
    """A VerifyPort with a CI-robust timeout baked in (a plain def, not partial)."""
    return verify(source, unwind=unwind, timeout_s=60)


def _drive(bug: str, clean: str, k: int, *, work_dir: Path, sink: ListSink) -> LoopRun:
    """Drive the loop bug -> recorded fix -> clean at bound k (no human, no LLM)."""
    provider = RecordedFixProvider({EXAMPLES / bug: EXAMPLES / clean})
    fix = ProviderFixPort(provider, work_dir=work_dir)
    return run_loop(
        EXAMPLES / bug, verify=_verify, fix=fix, unwind=k, max_iterations=2, sink=sink
    )


@pytest.mark.parametrize("bug, clean, k, unit", CORPUS, ids=[e[0] for e in CORPUS])
def test_corpus_kernel_closes_to_verified(
    bug: str, clean: str, k: int, unit: str, tmp_path: Path
) -> None:
    sink = ListSink()
    run = _drive(bug, clean, k, work_dir=tmp_path / "work", sink=sink)

    assert run.final_state is LoopState.DONE
    assert run.give_up_reason is None
    assert isinstance(run.iterations[0].result, Violated)
    assert isinstance(run.iterations[-1].result, Verified)
    assert any(e.type == "converged" for e in sink.events)
    assert "Outcome: DONE" in transcript_for(run, unit_id=unit)
