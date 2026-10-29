"""End-to-end (real ESBMC): the representation-invariant pair on the ring buffer (#387).

`examples/ring_buffer.c` and its staged-defect twin `ring_buffer_bug.c` share one
invariant. Under it, `rb_push` is VERIFIED up to k on the clean kernel and VIOLATED at
the exit assertion on the twin -- a bug the built-in safety checks cannot see, because
the buggy write stays in bounds (`tail` is reduced `% CAP`) and only `count` escapes.
Skipped when esbmc is not on PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.core import check_invariant_source, submit_invariant_source
from forseti.esbmc import Violated
from forseti.orchestrator import (
    PropertyCheckRun,
    PropertyOutcome,
    property_check_transcript,
)
from forseti.precond.reachability import ProbeReachability
from forseti.properties import (
    INVARIANT_ASSUMPTION_NOTE,
    REP_INVARIANT_LABEL,
    PropertyStore,
    Provenance,
    make_invariant,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CLEAN = EXAMPLES / "ring_buffer.c"
BUG = EXAMPLES / "ring_buffer_bug.c"
INVARIANT = (
    "self->count <= CAP && self->head < CAP && self->tail < CAP "
    "&& (self->head + self->count) % CAP == self->tail"
)


def _check(source: Path, function: str, root: Path) -> PropertyCheckRun:
    return check_invariant_source(
        source, function=function, type_name="ring_t", store_root=root
    )


def _store_invariant(source: Path, root: Path) -> None:
    result = submit_invariant_source(
        source,
        type_name="ring_t",
        expression=INVARIANT,
        provider="hand",
        model="human",
        store_root=root,
    )
    assert result.accepted


@pytest.mark.parametrize("function", ["rb_push", "rb_pop", "rb_init"])
def test_clean_ring_buffer_holds_for_every_function(
    tmp_path: Path, function: str
) -> None:
    root = tmp_path / ".forseti"
    _store_invariant(CLEAN, root)
    run = _check(CLEAN, function, root)
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.probe is ProbeReachability.REACHED
    assert verdict.to_dict()["assumption"] == INVARIANT_ASSUMPTION_NOTE
    assert verdict.harness_source is not None
    if function == "rb_init":
        assert "__ESBMC_assume" not in verdict.harness_source
    else:
        assert "__ESBMC_assume(__forseti_inv(&r))" in verdict.harness_source


def test_bug_twin_violates_the_exit_assertion_of_rb_push_only(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    _store_invariant(BUG, root)

    push = _check(BUG, "rb_push", root)
    (verdict,) = push.verdicts
    assert verdict.outcome is PropertyOutcome.VIOLATED
    assert isinstance(verdict.result, Violated)
    # the invariant's own exit assertion fired, not a bounds check
    assert REP_INVARIANT_LABEL in verdict.result.raw_counterexample
    assert push.outcome == "violated"
    transcript = property_check_transcript(push)
    assert "VIOLATED" in transcript and "Assumed:" in transcript

    for function in ("rb_pop", "rb_init"):
        (other,) = _check(BUG, function, root).verdicts
        assert other.outcome is PropertyOutcome.HELD


def test_a_vacuous_invariant_is_rejected_by_the_probe_not_held(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    with PropertyStore.open(root) as store:
        store.add_type_invariant(
            make_invariant(
                type_key=f"{CLEAN}::ring_t",
                expression="0",
                provenance=Provenance("host-submitted", "1", "hand", "human"),
            )
        )
    run = _check(CLEAN, "rb_push", root)
    (verdict,) = run.verdicts
    assert verdict.outcome is PropertyOutcome.VACUOUS
    assert verdict.probe is ProbeReachability.UNREACHABLE
    assert run.outcome == "unknown"


def test_a_satisfiable_but_unreachable_post_state_is_also_vacuous(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".forseti"
    with PropertyStore.open(root) as store:
        store.add_type_invariant(
            make_invariant(
                type_key=f"{CLEAN}::ring_t",
                expression="self->count > self->count",
                provenance=Provenance("host-submitted", "1", "hand", "human"),
            )
        )
    (verdict,) = _check(CLEAN, "rb_pop", root).verdicts
    assert verdict.outcome is PropertyOutcome.VACUOUS


def test_uncompilable_invariant_is_an_error_never_a_pass(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    result = submit_invariant_source(
        CLEAN,
        type_name="ring_t",
        expression="self->no_such_field <= CAP",
        provider="hand",
        model="human",
        store_root=root,
    )
    assert result.accepted  # identifiers are resolved by the compiler, not the gate
    (verdict,) = _check(CLEAN, "rb_push", root).verdicts
    assert verdict.outcome is PropertyOutcome.ERROR
